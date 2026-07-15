"""
Azure DevOps Boards push — the Post-Workshop "Sync to Azure DevOps".

ONE-WAY push (explicit product decision — see the Post-Workshop plan):
this app is the source of truth for the generated backlog; work items
are created/updated on the client's board, never read back or
conflict-resolved. `backlog_sync_links` rows (see
postgres/models/backlog.py) make re-pushes idempotent: each pushed item
records its ADO work-item id and a sha256 of the fields last sent, so
"Push N items" counts and sends only what's new or changed instead of
duplicating the board on every click.

Auth: a Personal Access Token (simplest viable v1 — the OAuth entry in
source_oauth.py is scoped for git-repo cloning, a different API
surface). Configure in backend/.env:

    ADO_ORG_URL=https://dev.azure.com/your-org
    ADO_PROJECT=Your Project
    ADO_PAT=...            # PAT with "Work Items: Read & Write" scope
    ADO_STORY_TYPE=User Story   # optional — 'Product Backlog Item' for
                                # Scrum-template projects, 'User Story'
                                # (default) for Agile-template ones

ADO_PAT is also resolvable via Key Vault (logical name ADO_PAT →
secret 'ado-pat' once added to secret_manager's map; env wins today).

Work-item mapping (Azure DevOps REST 7.1, _apis/wit/workitems):
    Epic    -> 'Epic'      System.Title + System.Description
    Feature -> 'Feature'   + parent link to its epic
    Story   -> ADO_STORY_TYPE + parent link to its feature
               + Microsoft.VSTS.Common.AcceptanceCriteria (Given/When/Then)
Parent links use the System.LinkTypes.Hierarchy-Reverse relation.
Create = POST .../workitems/$<Type>, update = PATCH .../workitems/<id>,
both with an application/json-patch+json body — per Microsoft's API,
not a REST-convention slip.

Same urllib + degraded-failure conventions as graph_teams.py: per-item
errors are captured in the result list, never raised through the route.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from app.core.logging import log
from app.services import secret_manager

_API_VERSION = '7.1'
_SSL_CTX = ssl.create_default_context()


# ── configuration ─────────────────────────────────────────────────────
def _org_url() -> str:
    return (os.environ.get('ADO_ORG_URL') or '').strip().rstrip('/')


def _project() -> str:
    return (os.environ.get('ADO_PROJECT') or '').strip()


def _pat() -> str:
    return (secret_manager.get_secret('ADO_PAT', env_fallback='ADO_PAT') or '').strip()


def _story_type() -> str:
    return (os.environ.get('ADO_STORY_TYPE') or '').strip() or 'User Story'


_TYPE_BY_ITEM = {'epic': 'Epic', 'feature': 'Feature'}   # story resolved via _story_type()


def is_configured() -> bool:
    return bool(_org_url() and _project() and _pat())


def config_status() -> dict:
    """What the sync modal shows before pushing — never echoes the PAT."""
    return {
        'configured': is_configured(),
        'org_url': _org_url(),
        'project': _project(),
        'story_type': _story_type(),
        'missing': [name for name, ok in (
            ('ADO_ORG_URL', bool(_org_url())),
            ('ADO_PROJECT', bool(_project())),
            ('ADO_PAT', bool(_pat())),
        ) if not ok],
    }


# ── HTTP helper ───────────────────────────────────────────────────────
def _http_json(method: str, url: str, body: object | None = None,
               content_type: str = 'application/json-patch+json') -> tuple[int, dict]:
    """Tiny urllib helper for the ADO REST API. Returns (status,
    parsed_json|{'raw': text}). PAT goes in as HTTP Basic with a blank
    username — ADO's documented PAT scheme."""
    data = json.dumps(body).encode('utf-8') if body is not None else None
    token = base64.b64encode(f':{_pat()}'.encode('ascii')).decode('ascii')
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Accept': 'application/json',
        'Authorization': f'Basic {token}',
        **({'Content-Type': content_type} if data is not None else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            text = resp.read().decode('utf-8', errors='replace')
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode('utf-8', errors='replace')
        status = e.code
    except Exception as e:
        log.warning('[ADO] %s %s failed (%s)', method, url.split('?')[0], e.__class__.__name__)
        return 0, {'raw': f'network error: {e.__class__.__name__}'}
    try:
        return status, json.loads(text) if text else {}
    except ValueError:
        return status, {'raw': text[:500]}


def _err_of(status: int, j: dict) -> str:
    if status == 401:
        return 'Azure DevOps rejected the PAT (401) — check ADO_PAT and its "Work Items: Read & Write" scope'
    if status == 404:
        return ('project or work-item type not found (404) — check ADO_PROJECT, and set '
                f'ADO_STORY_TYPE (currently "{_story_type()}") to match the project\'s process template')
    msg = j.get('message') or j.get('raw') or ''
    return f'HTTP {status}: {str(msg)[:300]}' if status else str(msg)[:300]


# ── field building ────────────────────────────────────────────────────
def _esc(s: str) -> str:
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _ac_html(criteria: list[dict]) -> str:
    """Given-When-Then scenarios as the HTML ADO's Acceptance Criteria
    field renders."""
    parts = []
    for c in criteria or []:
        bits = []
        if c.get('given'):
            bits.append(f"<b>Given</b> {_esc(c['given'])}")
        if c.get('when'):
            bits.append(f"<b>When</b> {_esc(c['when'])}")
        if c.get('then'):
            bits.append(f"<b>Then</b> {_esc(c['then'])}")
        if bits:
            parts.append('<div>' + '<br>'.join(bits) + '</div>')
    return ''.join(parts)


def _fields_for(item_type: str, item: dict) -> dict:
    """The exact field set pushed for one item — also what gets hashed,
    so any change here marks every item as pending once. Keys are ADO
    field reference names."""
    if item_type == 'epic':
        desc = _esc(item.get('description') or '')
        return {'System.Title': f"{item['epic_id']} · {item['title']}"[:255],
                'System.Description': f'<div>{desc}</div>' if desc else ''}
    if item_type == 'feature':
        return {'System.Title': f"{item['feature_id']} · {item['title']}"[:255]}
    # story
    reqs = ', '.join(item.get('source_req_ids') or [])
    desc = _esc(item.get('text') or '')
    if reqs:
        desc += f'<br><i>Traces to: {_esc(reqs)}</i>'
    return {'System.Title': f"{item['story_id']} · {item['text']}"[:255],
            'System.Description': f'<div>{desc}</div>',
            'Microsoft.VSTS.Common.AcceptanceCriteria': _ac_html(item.get('acceptance_criteria'))}


def _hash_of(fields: dict) -> str:
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


# ── work-item calls ───────────────────────────────────────────────────
def _workitem_url(external_id: str = '', wi_type: str = '') -> str:
    base = f'{_org_url()}/{urllib.parse.quote(_project())}/_apis/wit/workitems'
    if external_id:
        return f'{base}/{external_id}?api-version={_API_VERSION}'
    return f'{base}/${urllib.parse.quote(wi_type)}?api-version={_API_VERSION}'


def _patch_ops(fields: dict, parent_url: Optional[str]) -> list[dict]:
    ops = [{'op': 'add', 'path': f'/fields/{name}', 'value': value}
           for name, value in fields.items() if value != '']
    if parent_url:
        ops.append({'op': 'add', 'path': '/relations/-', 'value': {
            'rel': 'System.LinkTypes.Hierarchy-Reverse', 'url': parent_url}})
    return ops


def _create(wi_type: str, fields: dict, parent_url: Optional[str]) -> tuple[Optional[dict], str]:
    status, j = _http_json('POST', _workitem_url(wi_type=wi_type),
                           _patch_ops(fields, parent_url))
    if status not in (200, 201):
        return None, _err_of(status, j)
    return j, ''


def _update(external_id: str, fields: dict) -> tuple[Optional[dict], str]:
    # Field updates only — the parent link set at create time stands; a
    # replaced tree wipes links and re-creates, so re-parenting an
    # existing item never happens in this flow.
    status, j = _http_json('PATCH', _workitem_url(external_id=external_id),
                           _patch_ops(fields, None))
    if status != 200:
        return None, _err_of(status, j)
    return j, ''


def _board_url(j: dict) -> str:
    links = j.get('_links') or {}
    html = (links.get('html') or {}).get('href') or ''
    return html or (j.get('url') or '')


# ── sync-state computation ────────────────────────────────────────────
def _walk(tree: dict):
    """Yield (item_type, item, parent_key) over the tree in push order —
    parents strictly before children. parent_key is (item_type, row_id)
    of the parent, or None for epics."""
    for e in tree.get('epics') or []:
        yield 'epic', e, None
        for f in e.get('features') or []:
            yield 'feature', f, ('epic', e['id'])
            for st in f.get('stories') or []:
                yield 'story', st, ('feature', f['id'])


def sync_status(workshop_id: int) -> dict:
    """{configured, org_url, project, story_type, missing, total,
    pending, synced, items: {epic: {...}}} — drives the "Push N items"
    count and the sync modal, no ADO calls made."""
    from app.services import backlog_service
    from app.postgres import session_scope
    from app.postgres.repositories import backlog as repo

    out = config_status()
    tree = backlog_service.get_tree(workshop_id)
    links: dict[tuple[str, int], object] = {}
    with session_scope() as s:
        if s is not None:
            links = {(l.item_type, l.item_row_id): l
                     for l in repo.list_sync_links(s, workshop_id)}
    total = pending = synced = 0
    last_synced_at = None
    for item_type, item, _parent in _walk(tree):
        total += 1
        link = links.get((item_type, item['id']))
        if link is not None and link.last_synced_at is not None:
            ts = int(link.last_synced_at.timestamp())
            last_synced_at = max(last_synced_at or 0, ts)
        if link is None or link.content_hash != _hash_of(_fields_for(item_type, item)):
            pending += 1
        else:
            synced += 1
    out.update({'total': total, 'pending': pending, 'synced': synced,
                'last_synced_at': last_synced_at})
    return out


def push_backlog(workshop_id: int) -> dict:
    """Push the workshop's backlog tree to Azure DevOps — create items
    that have no sync link, update items whose content hash changed,
    skip the rest. Children of a failed parent are skipped (they can't
    be parented). Returns {ok, results: [{item_type, code, title,
    action, external_id, url, error?}], created, updated, skipped,
    failed}."""
    if not is_configured():
        missing = ', '.join(config_status()['missing'])
        return {'ok': False, 'error': f'Azure DevOps is not configured — set {missing} in backend/.env'}

    from app.services import backlog_service
    from app.postgres import session_scope
    from app.postgres.repositories import backlog as repo

    tree = backlog_service.get_tree(workshop_id)
    if not tree.get('epics'):
        return {'ok': False, 'error': 'nothing to push — generate the Product Backlog first'}

    with session_scope() as s:
        if s is None:
            return {'ok': False, 'error': 'database unavailable — sync state could not be read'}
        links = {(l.item_type, l.item_row_id): l for l in repo.list_sync_links(s, workshop_id)}
        # Snapshot what push needs; the write session below is separate.
        link_state = {k: {'external_id': l.external_id, 'external_url': l.external_url,
                          'content_hash': l.content_hash} for k, l in links.items()}

    results: list[dict] = []
    created = updated = skipped = failed = 0
    # (item_type, row_id) -> ADO API url, for children's parent links.
    api_urls: dict[tuple[str, int], str] = {}
    failed_parents: set[tuple[str, int]] = set()

    for item_type, item, parent_key in _walk(tree):
        code = item.get('epic_id') or item.get('feature_id') or item.get('story_id')
        title = item.get('title') or item.get('text') or ''
        entry = {'item_type': item_type, 'code': code, 'title': title[:120]}
        key = (item_type, item['id'])

        if parent_key is not None and parent_key in failed_parents:
            entry.update({'action': 'skipped', 'error': 'parent was not pushed'})
            failed_parents.add(key)
            skipped += 1
            results.append(entry)
            continue

        fields = _fields_for(item_type, item)
        content_hash = _hash_of(fields)
        link = link_state.get(key)
        wi_type = _TYPE_BY_ITEM.get(item_type) or _story_type()

        if link is None:
            parent_url = api_urls.get(parent_key) if parent_key else None
            if parent_key is not None and parent_url is None:
                # Parent exists on the board from an earlier push — its
                # API url wasn't in this run's cache; fetch is avoidable:
                # links store external_id, and ADO accepts the canonical
                # work-item URL form for relations.
                pext = link_state.get(parent_key)
                if pext:
                    parent_url = f"{_org_url()}/_apis/wit/workItems/{pext['external_id']}"
            j, err = _create(wi_type, fields, parent_url)
            if j is None:
                entry.update({'action': 'failed', 'error': err})
                failed_parents.add(key)
                failed += 1
                results.append(entry)
                continue
            external_id = str(j.get('id'))
            entry.update({'action': 'created', 'external_id': external_id, 'url': _board_url(j)})
            created += 1
        elif link['content_hash'] != content_hash:
            j, err = _update(link['external_id'], fields)
            if j is None:
                entry.update({'action': 'failed', 'error': err})
                failed_parents.add(key)
                failed += 1
                results.append(entry)
                continue
            external_id = link['external_id']
            entry.update({'action': 'updated', 'external_id': external_id, 'url': _board_url(j)})
            updated += 1
        else:
            api_urls[key] = f"{_org_url()}/_apis/wit/workItems/{link['external_id']}"
            entry.update({'action': 'skipped', 'external_id': link['external_id'],
                          'url': link.get('external_url') or ''})
            skipped += 1
            results.append(entry)
            continue

        api_urls[key] = f'{_org_url()}/_apis/wit/workItems/{external_id}'
        with session_scope() as s:
            if s is not None:
                repo.upsert_sync_link(s, workshop_id=workshop_id, item_type=item_type,
                                      item_row_id=item['id'], provider='azure_devops',
                                      external_id=external_id,
                                      external_url=entry.get('url') or None,
                                      content_hash=content_hash)
        results.append(entry)

    log.info('[ADO] push on workshop=%s: %d created, %d updated, %d skipped, %d failed',
             workshop_id, created, updated, skipped, failed)
    return {'ok': failed == 0, 'results': results, 'created': created,
            'updated': updated, 'skipped': skipped, 'failed': failed,
            **({'error': f'{failed} item{"s" if failed != 1 else ""} failed — see the result list'}
               if failed else {})}
