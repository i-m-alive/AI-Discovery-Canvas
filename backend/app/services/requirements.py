"""
Requirements service — the During-Workshop "Business Requirements —
Live" panel's backend (see postgres/models/requirement.py).

Two write paths, one shape:
    add(workshop_id, text, ...)            — the facilitator's "+ Add requirement"
    add_extracted(workshop_id, items)      — the extract_reqs agent's batch upsert

`REQ-NN` ids are assigned here, per workshop, monotonically — re-running
extraction after a new transcript import ADDS new rows (dedup below),
it never renumbers or duplicates what's already captured. Dedup is by
normalized requirement text: the extraction prompt is also told what
already exists, so this is the belt to the prompt's braces.

All functions follow this codebase's degraded-Postgres convention:
empty/False results instead of raising when the database is away.
"""

from __future__ import annotations

import re

from app.core.logging import log
from app.postgres import session_scope
from app.postgres.repositories import requirements as repo

MOSCOW = ('must', 'should', 'could', 'wont')
STATUSES = ('in_review', 'approved')


def _norm(text: str) -> str:
    """Normalization key for dedup — lowercase alphanumerics only, so
    'The system shall auto-assign shifts.' == 'the system SHALL auto
    assign shifts'."""
    return re.sub(r'[^a-z0-9]+', '', (text or '').lower())


def _to_dict(row) -> dict:
    return {'id': row.id, 'req_id': row.req_id, 'text': row.text,
            'category': row.category, 'moscow': row.moscow,
            'source_label': row.source_label, 'source_doc_id': row.source_doc_id,
            'source_quote': row.source_quote, 'status': row.status,
            'created_at': int(row.created_at.timestamp())}


def list_requirements(workshop_id: int) -> list[dict]:
    with session_scope() as s:
        if s is None:
            return []
        return [_to_dict(r) for r in repo.list_for_workshop(s, workshop_id)]


def count(workshop_id: int) -> int:
    with session_scope() as s:
        if s is None:
            return 0
        return repo.count_for_workshop(s, workshop_id)


def _next_req_id(existing_req_ids: list[str]) -> int:
    """The next numeric suffix — max(existing)+1, never len()+1, so a
    deleted REQ-03 is never reissued to a different requirement."""
    top = 0
    for rid in existing_req_ids:
        m = re.search(r'(\d+)$', rid or '')
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def add(workshop_id: int, text: str, *, category: str = '', moscow: str = 'should',
        source_label: str | None = None, source_doc_id: str | None = None,
        source_quote: str | None = None, status: str = 'in_review') -> dict:
    """One requirement (the facilitator's manual add). Returns the stored
    record, or {} when Postgres is unavailable / text is empty."""
    text = (text or '').strip()
    if not text:
        return {}
    moscow = moscow if moscow in MOSCOW else 'should'
    status = status if status in STATUSES else 'in_review'
    with session_scope() as s:
        if s is None:
            return {}
        existing = repo.list_for_workshop(s, workshop_id)
        n = _next_req_id([r.req_id for r in existing])
        row = repo.create(s, workshop_id=workshop_id, req_id=f'REQ-{n:02d}',
                          text=text[:2000], category=(category or '')[:60], moscow=moscow,
                          source_label=(source_label or None), source_doc_id=source_doc_id,
                          source_quote=(source_quote or '')[:500] or None,
                          status=status, sort=n)
        return _to_dict(row)


def add_extracted(workshop_id: int, items: list[dict]) -> list[dict]:
    """Batch upsert from the extract_reqs agent. Skips any item whose
    normalized text matches an existing requirement (stable across
    re-runs); returns ONLY the newly added records."""
    with session_scope() as s:
        if s is None:
            return []
        existing = repo.list_for_workshop(s, workshop_id)
        seen = {_norm(r.text) for r in existing}
        n = _next_req_id([r.req_id for r in existing])
        added: list[dict] = []
        for it in items:
            text = (it.get('text') or '').strip()
            key = _norm(text)
            if not text or not key or key in seen:
                continue
            seen.add(key)
            moscow = str(it.get('moscow') or 'should').lower()
            row = repo.create(s, workshop_id=workshop_id, req_id=f'REQ-{n:02d}',
                              text=text[:2000], category=str(it.get('category') or '')[:60],
                              moscow=moscow if moscow in MOSCOW else 'should',
                              source_label=(str(it.get('source_label') or '')[:240] or None),
                              source_doc_id=(str(it.get('source_doc_id') or '')[:32] or None),
                              source_quote=(str(it.get('source_quote') or '')[:500] or None),
                              sort=n)
            added.append(_to_dict(row))
            n += 1
    if added:
        log.info('[REQUIREMENTS] +%d extracted on workshop=%s (now %s)',
                 len(added), workshop_id, added[-1]['req_id'])
    return added


def update(workshop_id: int, row_id: int, fields: dict) -> dict:
    """Patch one requirement (inline edit / MoSCoW change / approve).
    Only known fields are applied; returns the updated record or {}."""
    with session_scope() as s:
        if s is None:
            return {}
        row = repo.get(s, row_id)
        if row is None or row.workshop_id != workshop_id:
            return {}
        if 'text' in fields and (fields['text'] or '').strip():
            row.text = str(fields['text']).strip()[:2000]
        if 'category' in fields:
            row.category = str(fields['category'] or '')[:60]
        if 'moscow' in fields and str(fields['moscow']).lower() in MOSCOW:
            row.moscow = str(fields['moscow']).lower()
        if 'status' in fields and str(fields['status']).lower() in STATUSES:
            row.status = str(fields['status']).lower()
        if 'source_quote' in fields:
            row.source_quote = str(fields['source_quote'] or '')[:500] or None
        s.flush()
        return _to_dict(row)


def delete(workshop_id: int, row_id: int) -> bool:
    with session_scope() as s:
        if s is None:
            return False
        row = repo.get(s, row_id)
        if row is None or row.workshop_id != workshop_id:
            return False
        return repo.delete(s, row_id)


def as_context_text(workshop_id: int, *, max_items: int = 60) -> str:
    """The captured requirements formatted for an agent prompt (capmap /
    brd context) — one line per requirement with id, MoSCoW, category and
    source. Empty string when none exist."""
    reqs = list_requirements(workshop_id)[:max_items]
    if not reqs:
        return ''
    lines = []
    for r in reqs:
        src = f" (source: {r['source_label']})" if r.get('source_label') else ''
        cat = f" [{r['category']}]" if r.get('category') else ''
        lines.append(f"{r['req_id']} ({r['moscow'].upper()}){cat}: {r['text']}{src}")
    return '\n'.join(lines)
