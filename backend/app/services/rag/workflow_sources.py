"""
Workflow-source embedding (Part 2 — representation).

Embeds the REAL content of a Workflow-Builder source into the existing FAISS
`documents` namespace using the existing text-embedding-3-large pipeline. This
is the path that replaces the dead summary-only approach: source content is
chunked + embedded directly, with an optional hybrid one-shot summary chunk for
high-level questions.

Keying (so retrieval can filter and roles are replaced independently)
----------------------------------------------------------------------
    doc_id = f'wfsrc:{project_id}:{source_node_id}:{role}'
    role ∈ {'content', 'summary', 'code'(reserved)}

Each role is its own FAISS doc, so re-embedding one role replaces only that
role's chunks (service.index_document is idempotent per doc_id). 'code' is NOT
embedded yet — it is reserved so a future capped real-code sample for repos can
be added with `embed_workflow_source(..., role='code', ...)` without touching
the keying or metadata schema.

Per-chunk metadata
-------------------
    project_id, project_name, workflow_id,
    source_node_id, source_type,
    kind='workflow_source', role, label,
    content_hash            (sha256 of the embedded content; drives skip-unchanged)
    (+ chunk_index / chunk_count added by the service layer)

`project_id` / `workflow_id` are already honoured by service._scope_predicate;
`source_node_id` / `source_type` / `role` are present so retrieval can later
scope to a single source or prefer summaries — no retrieval change is made here.

Reliability
-----------
* Never raises. Every outcome (embedded / skipped / failed / unchanged / a
  non-'ok' upstream status) is written to the Postgres `source_embeddings`
  ledger via `with_session`, which degrades to a local JSONL log when Postgres
  is down — the embed itself is never blocked by the ledger.
* Placeholder / empty / status-note content (e.g. fetch_url_content's '[...]'
  notes) is skipped and ledgered, never embedded, so no noise enters the index.
* One retry on a transient zero-chunk embed result before marking 'failed'.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from datetime import datetime

from app.services.rag import config, service

log = logging.getLogger('app.rag.workflow_sources')

# Hybrid summary chunk: default ON, behind a flag (env or per-call override).
_HYBRID_DEFAULT = os.environ.get('RAG_WFSRC_HYBRID_SUMMARY', '1').strip() not in ('0', 'false', 'no', '')

# Roles this module knows about. 'code' is reserved (not embedded yet).
ROLE_CONTENT = 'content'
ROLE_SUMMARY = 'summary'
ROLE_CODE = 'code'   # reserved — see module docstring

_SUMMARY_MAX_INPUT = 24_000   # chars of content fed to the summary LLM call


# ── keying / metadata ────────────────────────────────────────────────
def source_disc(source_uri: str) -> str:
    """Source-identity disambiguator: sha1(identity)[:8], or '0' when there is
    no identity (file uploads — disambiguated by the node-id suffix instead).

    Derived from the source's URL/identity, NOT its content, so re-embedding
    unchanged content yields the SAME disc -> same doc_id (idempotent), while
    skip-if-unchanged stays keyed separately on content_hash. Two sources that
    share (project_id, source_node_id, role) but point at different URLs get
    different discs and therefore distinct keys."""
    u = (source_uri or '').strip()
    return hashlib.sha1(u.encode('utf-8', 'ignore')).hexdigest()[:8] if u else '0'


def doc_id(project_id: str, source_node_id: str, role: str = ROLE_CONTENT,
           disc: str = '0') -> str:
    """FAISS doc id. `disc` (see source_disc) disambiguates a node id reused
    for different URLs within one project. Parse from the RIGHT
    (role=last, disc=2nd-last) since source_node_id may carry a ':<filehash>'
    per-file suffix."""
    return f'wfsrc:{project_id}:{source_node_id}:{disc}:{role}'


def _meta(*, project_id, project_name, workflow_id, source_node_id,
          source_type, role, label, content_hash, disc='0', source_uri='') -> dict:
    return {
        'project_id':     project_id or '',
        'project_name':   project_name or '',
        'workflow_id':    workflow_id or '',
        'source_node_id': source_node_id or '',
        'source_type':    source_type or '',
        'kind':           'workflow_source',
        'role':           role,
        'label':          label or source_type or 'Source',
        'content_hash':   content_hash or '',
        'disc':           disc or '0',
        # The source identity (URL) the disc is derived from — lets the
        # coverage audit and any future migration recompute the key without a
        # Neo4j round-trip. Non-secret (URLs only; never a credential).
        'source_uri':     source_uri or '',
    }


def _hash(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8', 'ignore')).hexdigest()


def _is_embeddable(content: str) -> tuple[bool, str]:
    """Decide whether content is real text worth embedding. Returns
    (ok, reason). Mirrors the ingestion status notes: fetch_url_content and
    friends return '[...]' placeholders for auth/empty/failed fetches."""
    c = (content or '').strip()
    if not c:
        return (False, 'empty')
    if c.startswith('[') and c.endswith(']') and len(c) < 400:
        return (False, 'placeholder')
    if _looks_like_app_shell(c):
        return (False, 'auth_required')
    return (True, '')


def _looks_like_app_shell(c: str) -> bool:
    """True for minified JS / SPA app shells that auth-walled sites (Atlassian,
    SharePoint, …) serve to anonymous fetches. These passed the placeholder
    check and vectorized as noise during the 2026-06 run-all (jira nodes
    embedded Atlassian's JS bundle). Natural language has a space roughly
    every 5-6 chars; minified JS has almost none."""
    sample = c[:4000]
    if len(sample) < 200:
        return False
    space_ratio = sample.count(' ') / len(sample)
    if space_ratio < 0.05:
        return True
    return ('function(' in sample or 'function e(' in sample) \
        and sample.count(';') > 40 and space_ratio < 0.12


# ── Guardrails (honored BEFORE embedding — never vectorize secrets/PII) ──
# Category ids from app/services/guardrails/categories.py.
_PII_CATS    = {'person', 'email', 'phone', 'employee_id', 'ip'}
_SECRET_CATS = {'api_key'}
# Name-like categories that genuinely need the NER pass (regex can't find them).
_NAME_CATS   = {'person', 'company', 'client', 'vendor', 'team', 'project'}


def _mask_for_embedding(content: str, *, project_id: str, source_node_id: str,
                        redact_pii: bool, strip_secrets: bool) -> tuple[str | None, list, bool]:
    """Apply guardrails masking to source content BEFORE it is embedded.

    Combines the node-level flags (redactPii / stripSecrets) with the project's
    effective guardrails config. Returns (safe_text, masked_categories, ok):
      * ok=True, masked_categories=[]   → nothing required; embed `content` as-is
      * ok=True, masked_categories=[…]  → masking applied; embed `safe_text`
      * ok=False                        → masking was REQUIRED but failed → the
                                          caller MUST NOT embed (fail-safe; we
                                          never vectorize unmasked sensitive data)
    Never raises."""
    cats: set[str] = set()
    try:
        from app.services.guardrails.resolver import resolve_effective_config
        cfg = resolve_effective_config(project_id or None)   # never raises
        cats |= set(cfg.enabled_categories or [])
    except Exception as e:
        log.info('[RAG/WFSRC] guardrails config resolve failed (%s)', e)
    if redact_pii:
        cats |= _PII_CATS
    if strip_secrets:
        cats |= _SECRET_CATS
    if not cats:
        return (content, [], True)   # no guardrails in scope → embed verbatim
    try:
        from app.services.guardrails.masker import mask
        from app.services.guardrails.vault import AliasVault
        vault = AliasVault.for_run(f'wfsrc:{source_node_id}')
        use_ner = bool(cats & _NAME_CATS)
        masked, _report = mask(content, enabled_categories=sorted(cats),
                               vault=vault, use_ner=use_ner)
        return (masked, sorted(cats), True)
    except Exception as e:
        # Required but failed → refuse to embed (guarantee #5).
        log.warning('[RAG/WFSRC] guardrail masking failed for %s (%s) — REFUSING to embed',
                    source_node_id, e)
        return (None, sorted(cats), False)


# ── ledger (non-blocking) ────────────────────────────────────────────
def _ledger_record(**kw) -> None:
    """Best-effort ledger write. Uses with_session (None on Postgres-down or
    error, never raises); falls back to a local JSONL so visibility survives
    even when Postgres is unreachable. NEVER blocks or fails the embed."""
    try:
        from app.postgres import with_session
        from app.postgres.repositories import source_embeddings as repo
        res = with_session(lambda s: repo.record(s, **kw))
        if res is not None:
            return
    except Exception as e:   # import/availability hiccup — fall through to local
        log.debug('[RAG/WFSRC] ledger via postgres unavailable (%s)', e)
    _ledger_local_fallback(kw)


def _ledger_local_fallback(kw: dict) -> None:
    try:
        path = os.path.join(str(config.data_dir()), 'source_embeddings_fallback.jsonl')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {**kw, 'ts': datetime.utcnow().isoformat() + 'Z', 'sink': 'local-fallback'}
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as e:   # even the fallback must never raise
        log.warning('[RAG/WFSRC] ledger local fallback failed (%s)', e)


def _ledger_get_state(project_id: str, source_node_id: str,
                      disc: str = '0', role: str = ROLE_CONTENT):
    """(content_hash, status) for the REAL source = (project_id, node, disc,
    role). project_id + disc are part of the key so a node id reused across
    projects / URLs no longer reads another source's row."""
    try:
        from app.postgres import with_session
        from app.postgres.repositories import source_embeddings as repo
        res = with_session(lambda s: repo.get_state(s, project_id, source_node_id, disc, role))
        return res or (None, None)
    except Exception:
        return (None, None)


# ── summary generation (hybrid) ──────────────────────────────────────
def _generate_summary(content: str, label: str, tag: str) -> str:
    """One-shot 5-10 bullet summary of the source content. Best-effort —
    returns '' on any failure (the content chunks still get embedded)."""
    try:
        from app.services.llm_service import complete
        prompt = (
            'Summarize the source below in 5-10 dense bullet points covering its '
            'purpose, key facts, entities, and anything an engineer or analyst would '
            f'ask about it. Do not invent.\n\nSOURCE ({label}):\n{content[:_SUMMARY_MAX_INPUT]}'
        )
        return (complete(prompt, tag=tag, max_output_tokens=512) or '').strip()
    except Exception as e:
        log.info('[RAG/WFSRC] summary generation skipped for %s (%s)', label, e)
        return ''


# ── public API ───────────────────────────────────────────────────────
def embed_workflow_source(*, project_id: str, source_node_id: str,
                          source_type: str, content: str,
                          label: str = '', workflow_id: str = '',
                          project_name: str = '', status: str = 'ok',
                          source_uri: str = '',
                          hybrid_summary: bool | None = None,
                          summary_text: str | None = None,
                          skip_if_unchanged: bool = True,
                          redact_pii: bool = False,
                          strip_secrets: bool = False,
                          tag: str = '[RAG/WFSRC]') -> dict:
    """Embed one workflow source's content (+ optional hybrid summary).

    `status` is the upstream extraction status — anything other than 'ok'
    (auth_required, fetch_skipped, cred_missing, not_connected, empty, …) is
    ledgered and skipped, never embedded. `source_uri` is the source's identity
    (URL for repos/connectors; empty for file uploads) — it derives the `disc`
    that distinguishes a node id reused for different URLs. `redact_pii`/
    `strip_secrets` are the node's guardrail flags; combined with project
    guardrails they mask the content BEFORE embedding (and refuse to embed if
    masking is required but fails). Returns a result dict:
        {status, content_chunks, summary_chunks, content_hash, skipped, reason}
    Never raises."""
    if hybrid_summary is None:
        hybrid_summary = _HYBRID_DEFAULT
    result = {'status': None, 'content_chunks': 0, 'summary_chunks': 0,
              'content_hash': None, 'skipped': True, 'reason': ''}

    if not source_node_id or not project_id:
        result.update(status='skipped', reason='missing project_id/source_node_id')
        return result

    disc = source_disc(source_uri)
    # `disc` rides in `common` so EVERY ledger row for this source carries the
    # full key (project_id, source_node_id, role, disc).
    common = dict(project_id=project_id, source_node_id=source_node_id,
                  source_type=source_type, workflow_id=workflow_id, disc=disc)

    if not service.is_enabled():
        result.update(status='disabled', reason='rag subsystem disabled')
        _ledger_record(role=ROLE_CONTENT, status='disabled', note='rag disabled', **common)
        return result

    # Upstream extraction already failed/locked → record the exact status so it
    # shows on the waiting list; do not embed.
    if status and status != 'ok':
        result.update(status=status, reason=f'upstream status={status}')
        _ledger_record(role=ROLE_CONTENT, status=status, note='upstream not ok', **common)
        return result

    ok, why = _is_embeddable(content)
    if not ok:
        result.update(status=why, reason=why)
        _ledger_record(role=ROLE_CONTENT, status=why, note='non-embeddable content', **common)
        return result

    # Guardrails BEFORE embedding — never vectorize secrets/PII (guarantee #5).
    safe, masked_cats, mask_ok = _mask_for_embedding(
        content, project_id=project_id, source_node_id=source_node_id,
        redact_pii=redact_pii, strip_secrets=strip_secrets)
    if not mask_ok:
        result.update(status='redaction_failed',
                      reason='guardrail masking required but failed; refused to embed')
        _ledger_record(role=ROLE_CONTENT, status='redaction_failed',
                       error='mask required but failed', **common)
        return result
    embed_content = safe if masked_cats else content
    mask_note = ('masked:' + ','.join(masked_cats)) if masked_cats else None

    # Hash the ORIGINAL content for change detection (so a real source edit
    # re-triggers embedding even if masking output is identical).
    content_hash = _hash(content)
    result['content_hash'] = content_hash

    # Skip if this exact content was already embedded (cost control + idempotency).
    if skip_if_unchanged:
        prior_hash, prior_status = _ledger_get_state(project_id, source_node_id, disc, ROLE_CONTENT)
        if prior_hash == content_hash and prior_status == 'embedded':
            result.update(status='unchanged', reason='content unchanged since last embed')
            return result

    # ── embed CONTENT ────────────────────────────────────────────────
    cmeta = _meta(project_id=project_id, project_name=project_name,
                  workflow_id=workflow_id, source_node_id=source_node_id,
                  source_type=source_type, role=ROLE_CONTENT, label=label,
                  content_hash=content_hash, disc=disc, source_uri=source_uri)
    n = _embed_role(doc_id(project_id, source_node_id, ROLE_CONTENT, disc), embed_content, cmeta, tag)
    if n <= 0:
        result.update(status='failed', reason='content embed produced 0 chunks')
        _ledger_record(role=ROLE_CONTENT, status='failed',
                       error='0 chunks (embed failed or empty after chunking)', **common)
        return result
    result['content_chunks'] = n
    result['skipped'] = False
    result['status'] = 'embedded'
    _ledger_record(role=ROLE_CONTENT, status='embedded', chunks=n,
                   content_hash=content_hash, note=mask_note, **common)

    # ── embed SUMMARY (hybrid) — built from the MASKED content ───────
    if hybrid_summary:
        s_text = (summary_text or '').strip() or _generate_summary(embed_content, label or source_type, tag + ':SUMM')
        if s_text:
            smeta = _meta(project_id=project_id, project_name=project_name,
                          workflow_id=workflow_id, source_node_id=source_node_id,
                          source_type=source_type, role=ROLE_SUMMARY, label=label,
                          content_hash=content_hash, disc=disc, source_uri=source_uri)
            sn = _embed_role(doc_id(project_id, source_node_id, ROLE_SUMMARY, disc),
                             s_text, smeta, tag + ':SUMM')
            result['summary_chunks'] = sn
            _ledger_record(role=ROLE_SUMMARY,
                           status='embedded' if sn > 0 else 'failed',
                           chunks=sn, content_hash=content_hash,
                           error=None if sn > 0 else '0 chunks', **common)
        else:
            _ledger_record(role=ROLE_SUMMARY, status='skipped',
                           note='summary generation returned empty', **common)

    return result


def _embed_role(did: str, text: str, meta: dict, tag: str) -> int:
    """Embed one role's text, with a single retry on a transient zero result.
    Returns chunk count (0 on failure). The embedder itself already does
    429/5xx backoff; this guards the 'returned 0 but content was real' case."""
    try:
        n = service.index_document(doc_id=did, text=text, metadata=meta,
                                   is_html=False, tag=tag)
        if n <= 0 and (text or '').strip():
            n = service.index_document(doc_id=did, text=text, metadata=meta,
                                       is_html=False, tag=tag + ':retry')
        return n or 0
    except Exception as e:   # service.index_document already swallows most; belt-and-braces
        log.warning('%s embed failed for %s (%s)', tag, did, e)
        return 0


def delete_workflow_source(project_id: str, source_node_id: str) -> int:
    """Remove every role AND every disc's vectors for a source (e.g. node
    deleted). A node id can carry multiple discs (one per URL it pointed at),
    so delete by prefix `wfsrc:{pid}:{nid}:` rather than per-role doc_id.
    Returns the number of chunks removed. Ledger rows marked 'removed'."""
    removed = 0
    prefix = f'wfsrc:{project_id}:{source_node_id}:'
    try:
        for did in service.list_doc_ids(prefix):
            removed += service.delete_document(did)
    except Exception as e:
        log.warning('[RAG/WFSRC] delete %s:%s failed (%s)', project_id, source_node_id, e)
    _ledger_record(role=ROLE_CONTENT, status='removed', project_id=project_id,
                   source_node_id=source_node_id, note='source deleted')
    return removed


# ── Background execution (non-blocking; never slows/blocks a caller) ──
_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()
# Single-flight OAuth token refresh so N sources sharing a connection don't
# race (critical for rotating refresh tokens, e.g. GitLab).
_REFRESH_LOCK = threading.Lock()


def _spawn(key: str, fn, **kwargs) -> bool:
    """Run fn(**kwargs) on a daemon thread unless an identical job is already
    in flight. Any exception inside the thread is logged, never propagated —
    the caller (a workflow run / save) is never slowed, blocked, or broken."""
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return False
        _INFLIGHT.add(key)

    def _worker():
        try:
            fn(**kwargs)
        except Exception as e:   # belt-and-braces — embed_* already never raises
            log.warning('[RAG/WFSRC] background job %s failed (%s)', key, e)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(key)

    threading.Thread(target=_worker, daemon=True, name=f'wfsrc-{key[:24]}').start()
    return True


def kickoff_embed_workflow_source(**kwargs) -> bool:
    """Background, best-effort embed of already-extracted source content.
    This is the capture-at-run entry point — content (incl. file-upload text)
    is passed in directly because it only exists at run time. Returns True if a
    job was started. NEVER blocks the caller."""
    nid = kwargs.get('source_node_id')
    if not nid:
        return False
    pid = kwargs.get('project_id') or ''
    return _spawn(f'embed:{pid}:{nid}', embed_workflow_source, **kwargs)


def _embed_file(*, project_id, source_node_id, source_type, file_path,
                filename='', label='', workflow_id='', project_name='',
                redact_pii=False, strip_secrets=False):
    """Worker: extract text from a persisted file on disk, then embed. Used for
    Modernization-wizard sources, whose bytes ARE persisted under _SOURCES_DIR
    (unlike Workflow-Builder file uploads)."""
    text = ''
    try:
        from app.services.rag.file_extractor import extract_text_from_bytes
        with open(file_path, 'rb') as fh:
            data = fh.read()
        ext = os.path.splitext(filename or file_path)[1].lstrip('.')
        text = extract_text_from_bytes(data, file_ext=ext) or ''
    except Exception as e:
        log.info('[RAG/WFSRC] file extract failed for %s (%s)', file_path, e)
    embed_workflow_source(
        project_id=project_id, source_node_id=source_node_id,
        source_type=source_type, content=text, label=label or filename,
        workflow_id=workflow_id, project_name=project_name,
        status='ok' if text.strip() else 'empty',
        redact_pii=redact_pii, strip_secrets=strip_secrets,
        tag='[RAG/WFSRC/FILE]')


def kickoff_embed_file(**kwargs) -> bool:
    """Background, best-effort embed of a persisted file source (extracts text
    from disk in the worker). NEVER blocks the caller."""
    nid = kwargs.get('source_node_id')
    if not nid:
        return False
    pid = kwargs.get('project_id') or ''
    return _spawn(f'file:{pid}:{nid}', _embed_file, **kwargs)


# ── Re-fetchable extraction (capture-at-save + backfill share this) ───
# Connector/URL kinds whose content can be re-fetched anonymously from config.
URL_FETCH_TYPES = {
    'weburl', 'websearch', 'jira', 'confluence', 'sharepoint', 'onenote',
    'apm', 'oracle', 'config', 'email', 'database', 'packaged',
}
_REPO_TYPES = {'repository'}
# File-upload kinds whose bytes are NOT persisted in config (going-forward only).
_FILE_TYPES = {'documents', 'document', 'diagrams', 'diagram', 'image', 'video',
               'incidents', 'incident_logs', 'release_notes', 'application_logs', 'logs'}


# GitHub /tree/ and /blob/ links are repo SUBPATHS, not clonable remotes —
# users paste them from the browser. Normalize to the base repo so clone /
# ls-remote work (found during the 2026-06 backfill: 34 nodes on one project
# all carried a /tree/ URL and could never embed).
_GH_SUBPATH_RE = re.compile(
    r'(https://github\.com/[^/]+/[^/]+?)(?:\.git)?/(?:tree|blob)/.*')

# user@host URLs (Azure DevOps copies them with the org as userinfo). git can
# clone them but urllib cannot fetch them; strip the userinfo so both the
# clone path and any HTTP fallback see a clean URL.
_USERINFO_RE = re.compile(r'^(https?://)[^@/\s]+@(.+)$', re.I)

# Repo-shaped URLs on UNTYPED nodes (or wrong types) must still go down the
# clone path — found via NKompass modernization, whose untyped n_source node
# carried an Azure DevOps _git URL and failed forever on the HTTP path.
_REPO_URL_RE = re.compile(
    r'https?://(?:[^@/\s]+@)?(?:'
    r'dev\.azure\.com/[^\s?#]+/_git/[^\s?#]+'
    r'|[^/\s]+\.visualstudio\.com/[^\s?#]+/_git/[^\s?#]+'
    r'|github\.com/[^/\s?#]+/[^/\s?#]+'
    r'|gitlab\.com/[^\s?#]+'
    r'|bitbucket\.org/[^/\s?#]+/[^/\s?#]+'
    r')', re.I)


def _normalize_repo_url(url: str) -> str:
    url = (url or '').strip()
    m = _USERINFO_RE.match(url)
    if m:
        url = m.group(1) + m.group(2)
    m = _GH_SUBPATH_RE.match(url)
    return m.group(1) if m else url


def looks_like_repo_url(url: str) -> bool:
    """True when a URL is a clonable git remote regardless of node type."""
    u = (url or '').strip()
    return bool(u) and (u.endswith('.git') or bool(_REPO_URL_RE.match(u)))


def owner_tenant_of(workflow_id: str = '', project_id: str = '') -> str:
    """The tenant that OWNS a workflow, read from its PERSISTED record
    (created_by.tenant_id) — NO request context, so it is correct in background
    embed threads. Returns '' for single-tenant / mock auth / unknown. Never
    raises."""
    try:
        if workflow_id:
            from app.database.definitions_store import WORKFLOWS
            wf = WORKFLOWS.get(workflow_id) or {}
            t = ((wf.get('created_by') or {}).get('tenant_id') or '').strip()
            if t:
                return t
    except Exception as e:
        log.info('[RAG/WFSRC] owner_tenant lookup failed for wf=%s (%s)',
                 workflow_id, e.__class__.__name__)
    return ''


def resolve_credential(credential_ref: str, *, owner_tenant: str = '') \
        -> tuple[tuple[str, str, str] | None, str | None]:
    """Resolve a source node's `credential_ref` to (username, secret, auth_kind).

    `credential_ref` is a CONNECTION ID (the productized OAuth path). For
    back-compat a raw 'src-cred-...' Key Vault name still works (PAT/bare token).

    Multi-tenant authz: a connection owned by a DIFFERENT tenant than the
    workflow's PERSISTED `owner_tenant` is REFUSED (cred_missing + security log).

    Returns (credential, status):
      * no ref                          -> (None, None)        anonymous
      * resolved                        -> ((user, secret, auth_kind), None)
      * not found / pending / revoked /
        cross-tenant / auth_required /
        token unresolved                -> (None, 'cred_missing')   skip, never stall

    The token is NEVER logged — only the ref/connection id. Never raises."""
    ref = (credential_ref or '').strip()
    if not ref:
        return (None, None)

    # ── back-compat: a direct Key Vault name (PAT-style) ─────────────
    if ref.startswith('src-cred-'):
        return _resolve_kv_name(ref)

    # ── connection-id path (OAuth) ───────────────────────────────────
    try:
        from app.postgres import with_session
        from app.postgres.repositories import source_connections as crepo
        conn = with_session(lambda s: crepo.get(s, ref))
    except Exception as e:
        log.info('[RAG/WFSRC] connection %s load error (%s)', ref, e.__class__.__name__)
        return (None, 'cred_missing')
    if conn is None or conn.status in ('pending', 'revoked'):
        log.info('[RAG/WFSRC] connection %s missing/not-usable (status=%s) -> cred_missing',
                 ref, getattr(conn, 'status', 'none'))
        return (None, 'cred_missing')

    # Tenant isolation: connection tenant '' is dev/single-tenant (always ok);
    # a real tenant must equal the workflow's persisted owner_tenant.
    conn_tenant = (conn.tenant_id or '').strip()
    if conn_tenant and conn_tenant != (owner_tenant or ''):
        log.warning('[RAG/WFSRC][SECURITY] cross-tenant credential_ref refused: '
                    'connection %s owned by tenant %s but workflow tenant is %s',
                    ref, conn_tenant[:8], (owner_tenant or '<none>')[:8])
        return (None, 'cred_missing')

    if conn.status == 'auth_required':
        log.info('[RAG/WFSRC] connection %s needs re-auth -> cred_missing', ref)
        return (None, 'cred_missing')

    # Refresh if the access token has expired (providers that expire).
    if not _ensure_fresh(conn):
        return (None, 'cred_missing')

    # Read the token JSON via the prefix-guarded KV reader.
    try:
        from app.services.secret_manager import get_secret_by_name
        raw = get_secret_by_name(conn.kv_secret_name)
    except Exception as e:
        log.info('[RAG/WFSRC] connection %s secret read error (%s)', ref, e.__class__.__name__)
        raw = None
    if not raw:
        log.info('[RAG/WFSRC] connection %s token unresolved -> cred_missing', ref)
        return (None, 'cred_missing')
    try:
        token_json = json.loads(raw)
    except Exception:
        token_json = {'access_token': raw}
    try:
        from app.services import source_oauth
        username, secret, auth_kind = source_oauth.clone_credential(conn.provider, token_json)
    except Exception:
        username, secret, auth_kind = 'x', token_json.get('access_token', ''), 'basic'
    if not secret:
        return (None, 'cred_missing')
    return ((username, secret, auth_kind), None)


def _resolve_kv_name(ref: str) -> tuple[tuple[str, str, str] | None, str | None]:
    """Back-compat: credential_ref is a raw Key Vault secret name (PAT path).
    Value may be JSON {username, token} or a bare token. auth_kind='basic'."""
    try:
        from app.services.secret_manager import get_secret_by_name
        secret = get_secret_by_name(ref)
    except Exception as e:
        log.info('[RAG/WFSRC] credential_ref %s resolve error (%s)', ref, e.__class__.__name__)
        secret = None
    if not secret:
        return (None, 'cred_missing')
    username, token = 'x', secret
    try:
        obj = json.loads(secret)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        tok = (obj.get('token') or obj.get('access_token') or obj.get('password') or '').strip()
        if not tok:
            return (None, 'cred_missing')
        token = tok
        username = (obj.get('username') or 'x').strip() or 'x'
    return ((username, token, 'basic'), None)


def _ensure_fresh(conn) -> bool:
    """Refresh the connection's access token if expired (single-flight per
    connection). On refresh failure mark the connection auth_required and
    return False (caller -> cred_missing). True when the token is usable."""
    import time
    exp = conn.expires_at
    if not exp:
        return True                      # provider tokens that don't expire
    try:
        remaining = exp.timestamp() - time.time()
    except Exception:
        return True
    if remaining > 60:
        return True
    # Expired (or about to) — attempt a refresh.
    with _REFRESH_LOCK:
        try:
            from app.services.secret_manager import get_secret_by_name, set_secret_by_name
            from app.services import source_oauth
            raw = get_secret_by_name(conn.kv_secret_name)
            tok = json.loads(raw) if raw else {}
            # Single-flight re-check: another thread may have refreshed while we
            # waited on the lock. Re-read the token's expiry from KV and skip if
            # it is now fresh — so N concurrent resolves trigger ONE refresh.
            cur_exp = tok.get('expires_at')
            if cur_exp and (cur_exp - time.time() > 60):
                return True
            rtok = tok.get('refresh_token')
            if not rtok:
                raise RuntimeError('no refresh_token')
            fresh = source_oauth.refresh(conn.provider, rtok)
            # GitLab-style rotation: persist the (possibly new) refresh token.
            if not fresh.get('refresh_token'):
                fresh['refresh_token'] = rtok
            set_secret_by_name(conn.kv_secret_name, json.dumps(fresh))
            from app.postgres import with_session
            from app.postgres.repositories import source_connections as crepo
            from datetime import datetime, timezone
            new_exp = (datetime.fromtimestamp(fresh['expires_at'], tz=timezone.utc)
                       if fresh.get('expires_at') else None)
            with_session(lambda s: crepo.mark_connected(
                s, conn.id, scopes=fresh.get('scope'), expires_at=new_exp))
            return True
        except Exception as e:
            log.info('[RAG/WFSRC] connection %s refresh failed (%s) -> auth_required',
                     conn.id, e.__class__.__name__)
            try:
                from app.postgres import with_session
                from app.postgres.repositories import source_connections as crepo
                with_session(lambda s: crepo.mark_status(s, conn.id, 'auth_required'))
            except Exception:
                pass
            return False


def _probe_repo_remote(url: str, credential: tuple[str, str] | None = None) -> tuple[bool, str]:
    """Cheap `git ls-remote` preflight (no clone). Returns (ok, status) where
    status maps to the ledger taxonomy: auth_required / fetch_skipped / failed.
    Never raises; never prompts (GIT_TERMINAL_PROMPT=0). When `credential` is
    supplied, the token is injected via GIT_CONFIG_* env (same mechanism as the
    clone) — never in argv/URL — so a private repo probes as reachable."""
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}
    probe_url = url
    if credential:
        from app.services.stack_analyzer import git_auth_env, _strip_userinfo
        env.update(git_auth_env(url, credential))
        probe_url = _strip_userinfo(url)
    try:
        p = subprocess.run(['git', 'ls-remote', '--heads', probe_url],
                           capture_output=True, text=True, timeout=25, env=env)
    except Exception as e:
        log.info('[RAG/WFSRC] ls-remote probe failed for %s (%s)', url, e)
        return (False, 'failed')
    if p.returncode == 0:
        return (True, 'ok')
    err = (p.stderr or '').lower()
    if any(t in err for t in ('authentication', 'could not read username',
                              '401', '403', 'permission denied', 'invalid credentials')):
        return (False, 'auth_required')
    if any(t in err for t in ('not found', '404', 'does not exist',
                              'could not resolve host')):
        return (False, 'fetch_skipped')
    return (False, 'failed')


def _extract_repository_text(url: str,
                             credential: tuple[str, str] | None = None) -> tuple[str, str]:
    """Code-analysis text for a repo URL (clone + stack analysis). Bounded —
    NOT raw file trees (matches what the assistant answers from). Returns
    (text, status). When `credential` is supplied, the private repo is probed
    and cloned with it (token via env, never argv/URL). Reserved role:'code'
    can add a real-code sample later."""
    url = _normalize_repo_url(url)
    ok, probe_status = _probe_repo_remote(url, credential=credential)
    if not ok:
        log.info('[RAG/WFSRC] repo %s unreachable (%s) — skipping clone', url, probe_status)
        return ('', probe_status)
    try:
        from app.services.stack_analyzer import analyze_from_url
        bp = analyze_from_url(url, credential=credential)
        meta = bp.pop('_meta', {}) if isinstance(bp, dict) else {}
        tree = (meta.get('file_tree') or [])[:200]
        block = [
            f'Repository: {url}',
            '',
            'Detected technology stack (reverse-engineered):',
            json.dumps(bp, indent=2)[:8000],
        ]
        if tree:
            block += ['', 'File tree (sample):', '\n'.join(tree)]
        return ('\n'.join(block).strip(), 'ok')
    except Exception as e:
        log.info('[RAG/WFSRC] repo analyze failed for %s (%s)', url, e)
        return ('', 'failed')


def extract_source_content(source_type: str, cfg: dict, *,
                           project_id: str = '', workflow_id: str = '') -> tuple[str, str]:
    """Best-effort (content, status) for a source node from its PERSISTED
    config — used by capture-at-save and the backfill. File-upload kinds have
    no persisted bytes, so they return ('', 'not_persisted').

    project_id/workflow_id let the repo branch authorize a per-source
    connection against the workflow's PERSISTED tenant (no request needed)."""
    t = (source_type or '').lower()
    cfg = cfg or {}
    url = (cfg.get('url') or cfg.get('repo_url') or '').strip()
    # Repo detection is URL-shape based, NOT type based: untyped nodes (and
    # mis-typed ones) carrying a clonable git URL must clone, not HTTP-fetch
    # (urllib can't even fetch user@host Azure DevOps URLs).
    if t in _REPO_TYPES or cfg.get('isRepo') or looks_like_repo_url(url):
        if not url:
            return ('', 'no_url')
        # Resolve the per-source credential via its CONNECTION ID, authorized
        # against the workflow's persisted tenant. Missing/cross-tenant/expired
        # -> 'cred_missing' (skip, never stall); no ref -> anonymous clone.
        owner_tenant = owner_tenant_of(workflow_id, project_id)
        credential, cred_status = resolve_credential(
            cfg.get('credential_ref'), owner_tenant=owner_tenant)
        if cred_status:
            return ('', cred_status)
        return _extract_repository_text(url, credential=credential)
    if t in _FILE_TYPES:
        return ('', 'not_persisted')
    if url:
        try:
            from app.routes.legacy_routes import fetch_url_content
            content = fetch_url_content(url)
        except Exception as e:
            log.info('[RAG/WFSRC] url fetch failed for %s (%s)', url, e)
            return ('', 'failed')
        if (content or '').startswith('['):
            low = content.lower()
            status = 'auth_required' if 'auth' in low else 'fetch_skipped'
            return (content, status)
        return (content, 'ok')
    return ('', 'no_url')


def canonical_source_uri(source_type: str, cfg: dict) -> str:
    """The identity string the disc is derived from, mirroring
    extract_source_content's branching: normalized repo URL for repo-shaped
    sources, the raw URL for connectors, '' for file uploads (which rely on
    the node-id suffix). Must match what embed time uses so the key is stable."""
    cfg = cfg or {}
    url = (cfg.get('url') or cfg.get('repo_url') or '').strip()
    t = (source_type or '').lower()
    if t in _REPO_TYPES or cfg.get('isRepo') or looks_like_repo_url(url):
        return _normalize_repo_url(url) if url else ''
    return url


def _embed_from_config(*, project_id, source_node_id, source_type, cfg,
                       label='', workflow_id='', project_name=''):
    """Capture-at-SAVE worker: re-fetch/clone content from a source node's
    persisted config, then embed. Auth-walled fetches resolve to a non-'ok'
    status → ledgered (the Tier-C waiting list), never embedded."""
    content, status = extract_source_content(source_type, cfg,
                                             project_id=project_id, workflow_id=workflow_id)
    embed_workflow_source(
        project_id=project_id, source_node_id=source_node_id,
        source_type=source_type, content=content, label=label,
        workflow_id=workflow_id, project_name=project_name, status=status,
        source_uri=canonical_source_uri(source_type, cfg),
        redact_pii=bool((cfg or {}).get('redactPii')),
        strip_secrets=bool((cfg or {}).get('stripSecrets')),
        tag='[RAG/WFSRC/SAVE]',
    )


def kickoff_embed_from_config(*, project_id, source_node_id, source_type, cfg,
                              label='', workflow_id='', project_name='',
                              only_if_new: bool = True) -> bool:
    """Background capture-at-save for a URL/repo source node. `only_if_new`
    skips nodes already recorded 'embedded' in the ledger so saves don't
    re-clone/re-fetch every time (a /run or explicit reindex refreshes content).
    File-upload kinds are ignored here (no persisted bytes). NEVER blocks."""
    if not source_node_id or not project_id:
        return False
    t = (source_type or '').lower()
    if t in _FILE_TYPES:
        return False   # captured at run instead
    url = ((cfg or {}).get('url') or (cfg or {}).get('repo_url') or '').strip()
    if not url and not (cfg or {}).get('isRepo'):
        return False
    if only_if_new:
        _disc = source_disc(canonical_source_uri(source_type, cfg))
        _, prior_status = _ledger_get_state(project_id, source_node_id, _disc, ROLE_CONTENT)
        if prior_status == 'embedded':
            return False
    return _spawn(f'save:{project_id}:{source_node_id}', _embed_from_config,
                  project_id=project_id, source_node_id=source_node_id,
                  source_type=source_type, cfg=cfg, label=label,
                  workflow_id=workflow_id, project_name=project_name)
