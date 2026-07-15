"""
Future Opportunities Register service — the Post-Workshop opportunities
panel's backend (see postgres/models/opportunity.py).

Two write paths, one shape (mirrors services/requirements.py exactly):
    add(workshop_id, title, ...)          — a manual register entry
    add_generated(workshop_id, items)     — the 'opportunities' agent's batch add

`OPP-NN` ids are assigned here, per workshop, monotonically — re-running
the agent after new material ADDS new rows (dedup by normalized title
below), it never renumbers or wipes existing ones, so the facilitator's
own triage (accept / flag for pruning / reject) survives regeneration.

All functions follow this codebase's degraded-Postgres convention:
empty/False results instead of raising when the database is away.
"""

from __future__ import annotations

import re

from app.core.logging import log
from app.postgres import session_scope
from app.postgres.repositories import opportunities as repo

HORIZONS = ('phase_1', 'phase_2', 'phase_3', 'explore')
SIZES = ('S', 'M', 'L')
PRIORITIES = ('high', 'med', 'low')
STATUSES = ('open', 'flagged_for_pruning', 'accepted', 'rejected')


def _norm(text: str) -> str:
    """Normalization key for dedup — lowercase alphanumerics only."""
    return re.sub(r'[^a-z0-9]+', '', (text or '').lower())


def _to_dict(row) -> dict:
    return {'id': row.id, 'opp_id': row.opp_id, 'title': row.title,
            'description': row.description, 'horizon': row.horizon,
            'size': row.size, 'priority': row.priority, 'status': row.status,
            'source_req_ids': row.source_req_ids or [],
            'created_at': int(row.created_at.timestamp())}


def list_opportunities(workshop_id: int) -> list[dict]:
    with session_scope() as s:
        if s is None:
            return []
        return [_to_dict(r) for r in repo.list_for_workshop(s, workshop_id)]


def count(workshop_id: int) -> int:
    with session_scope() as s:
        if s is None:
            return 0
        return repo.count_for_workshop(s, workshop_id)


def _next_opp_id(existing_opp_ids: list[str]) -> int:
    """max(existing)+1, never len()+1 — a deleted OPP-03 is never
    reissued to a different opportunity."""
    top = 0
    for oid in existing_opp_ids:
        m = re.search(r'(\d+)$', oid or '')
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def add_generated(workshop_id: int, items: list[dict]) -> list[dict]:
    """Batch add from the 'opportunities' agent. Skips any item whose
    normalized title matches an existing row (stable across re-runs);
    returns ONLY the newly added records. Items are pre-coerced by
    agent_catalog._coerce_opportunities — enums are trusted here."""
    with session_scope() as s:
        if s is None:
            return []
        existing = repo.list_for_workshop(s, workshop_id)
        seen = {_norm(r.title) for r in existing}
        n = _next_opp_id([r.opp_id for r in existing])
        added: list[dict] = []
        for it in items:
            title = (it.get('title') or '').strip()
            key = _norm(title)
            if not title or not key or key in seen:
                continue
            seen.add(key)
            row = repo.create(s, workshop_id=workshop_id, opp_id=f'OPP-{n:02d}',
                              title=title[:200], description=(it.get('description') or '')[:2000],
                              horizon=it.get('horizon') or 'explore',
                              size=it.get('size') or 'M',
                              priority=it.get('priority') or 'med',
                              source_req_ids=it.get('source_req_ids') or [], sort=n)
            added.append(_to_dict(row))
            n += 1
    if added:
        log.info('[OPPORTUNITIES] +%d generated on workshop=%s (now %s)',
                 len(added), workshop_id, added[-1]['opp_id'])
    return added


def update(workshop_id: int, row_id: int, fields: dict) -> dict:
    """Patch one opportunity (triage status, inline edits). Only known
    fields with valid enum values are applied; returns the updated
    record or {}."""
    with session_scope() as s:
        if s is None:
            return {}
        row = repo.get(s, row_id)
        if row is None or row.workshop_id != workshop_id:
            return {}
        if 'title' in fields and (fields['title'] or '').strip():
            row.title = str(fields['title']).strip()[:200]
        if 'description' in fields:
            row.description = str(fields['description'] or '')[:2000]
        if 'horizon' in fields and str(fields['horizon']).lower() in HORIZONS:
            row.horizon = str(fields['horizon']).lower()
        if 'size' in fields and str(fields['size']).upper() in SIZES:
            row.size = str(fields['size']).upper()
        if 'priority' in fields and str(fields['priority']).lower() in PRIORITIES:
            row.priority = str(fields['priority']).lower()
        if 'status' in fields and str(fields['status']).lower() in STATUSES:
            row.status = str(fields['status']).lower()
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
