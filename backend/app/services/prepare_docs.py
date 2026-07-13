"""
Prepare-zone document registry.

Durable, server-side source of truth for "every document ever uploaded
to this workshop's Prepare zone": metadata in Postgres
(app.postgres.models.prepare_doc — one row per doc, scoped by a real
`workshop_id`), full extracted text in the content-addressed object
store (`object_store.py`, unchanged).

Previously this scoped by a hardcoded `board_id='default'` string and
kept its index in a flat JSON file; now every call takes a real
`workshop_id` (the Postgres `workshops.id` — see app/routes/projects.py),
one per BA engagement instead of the single global board.

Public API:
    register(workshop_id, name, text, uploaded_by) -> doc record (with doc_id)
    list_docs(workshop_id) -> [doc record, ...]   (metadata only, no text)
    get_text(workshop_id, doc_id) -> str | None   (full extracted text)
    get_all_texts(workshop_id) -> [{name, text}, ...]
    delete(workshop_id, doc_id) -> bool
"""

from __future__ import annotations

import uuid

from app.core.logging import log
from app.postgres import session_scope
from app.postgres.repositories import prepare_docs as repo
from app.services import object_store


def _obj_key(workshop_id: int, doc_id: str) -> str:
    return f'prepare_docs/{workshop_id}/{doc_id}.txt'


def _file_key(workshop_id: int, doc_id: str) -> str:
    """Original uploaded bytes — separate from the extracted-text object
    above, so the BA can actually VIEW the source file (PDF/DOCX/XLSX/
    PPTX/...) they uploaded, not just its extracted text. Was previously
    discarded right after extraction; upload() now keeps it."""
    return f'prepare_docs/{workshop_id}/{doc_id}.orig'


def register(workshop_id: int, name: str, text: str, uploaded_by: str = '',
            file_bytes: bytes | None = None) -> dict:
    """Store the full text in the object store, record metadata in
    Postgres, return the record (WITHOUT the text — callers that need it
    call get_text). Returns an empty dict if Postgres isn't reachable —
    callers already treat "no doc registered" as a soft failure.
    `file_bytes`, when given, is the ORIGINAL uploaded file (pre-
    extraction) — stored so the document can be viewed in its native
    format later (see get_original_bytes / routes/agents.py's
    document/<id>/file, /view routes)."""
    doc_id = uuid.uuid4().hex[:16]
    object_store.put_bytes(_obj_key(workshop_id, doc_id), (text or '').encode('utf-8'),
                           content_type='text/plain')
    if file_bytes:
        object_store.put_bytes(_file_key(workshop_id, doc_id), file_bytes)
    with session_scope() as s:
        if s is None:
            log.warning('[PREPARE_DOCS] Postgres unavailable — %s not registered', name)
            return {}
        row = repo.create(s, doc_id=doc_id, workshop_id=workshop_id, name=(name or 'document')[:200],
                          chars=len(text or ''), uploaded_by=uploaded_by or '')
        record = {'doc_id': row.doc_id, 'name': row.name, 'chars': row.chars,
                  'uploaded_by': row.uploaded_by, 'uploaded_at': int(row.uploaded_at.timestamp()),
                  'status': row.status, 'status_detail': row.status_detail}
    log.info('[PREPARE_DOCS] registered %s (%s, %d chars) on workshop=%s',
             record.get('name'), doc_id, record.get('chars', 0), workshop_id)
    return record


def list_docs(workshop_id: int) -> list[dict]:
    with session_scope() as s:
        if s is None:
            return []
        rows = repo.list_for_workshop(s, workshop_id)
        return [{'doc_id': d.doc_id, 'name': d.name, 'chars': d.chars,
                 'uploaded_by': d.uploaded_by, 'uploaded_at': int(d.uploaded_at.timestamp()),
                 'status': d.status, 'status_detail': d.status_detail}
                for d in rows]


def set_status(workshop_id: int, doc_id: str, status: str, detail: str | None = None) -> bool:
    """Best-effort — a status-tracking failure must never fail whatever
    background indexing step called this. Returns False (silently) on
    any error, matching this module's existing degraded-Postgres
    tolerance elsewhere."""
    try:
        with session_scope() as s:
            if s is None:
                return False
            row = repo.get(s, doc_id)
            if row is None or row.workshop_id != workshop_id:
                return False
            repo.set_status(s, doc_id, status, detail)
            return True
    except Exception as e:
        log.info('[PREPARE_DOCS] set_status(%s, %s) skipped (%s)', doc_id, status, e.__class__.__name__)
        return False


def get_text(workshop_id: int, doc_id: str) -> str | None:
    with session_scope() as s:
        if s is None:
            return None
        row = repo.get(s, doc_id)
        if row is None or row.workshop_id != workshop_id:
            return None
    data = object_store.get_bytes(_obj_key(workshop_id, doc_id))
    return data.decode('utf-8', errors='replace') if data is not None else None


def get_original_bytes(workshop_id: int, doc_id: str) -> bytes | None:
    """The original uploaded file, if it was uploaded after this feature
    shipped — pre-existing rows (uploaded before original bytes were
    kept) correctly return None; callers fall back to the extracted-text
    view for those, never error."""
    with session_scope() as s:
        if s is None:
            return None
        row = repo.get(s, doc_id)
        if row is None or row.workshop_id != workshop_id:
            return None
    return object_store.get_bytes(_file_key(workshop_id, doc_id))


def get_all_texts(workshop_id: int) -> list[dict]:
    """[{name, text}, ...] for every registered document — what the
    deep-research pipeline consumes (the full persistent corpus for this
    workshop, not just whatever happens to be attached in the current
    browser tab)."""
    out = []
    for d in list_docs(workshop_id):
        text = get_text(workshop_id, d['doc_id'])
        if text:
            out.append({'name': d['name'], 'text': text})
    return out


def delete(workshop_id: int, doc_id: str) -> bool:
    with session_scope() as s:
        if s is None:
            return False
        row = repo.get(s, doc_id)
        if row is None or row.workshop_id != workshop_id:
            return False
        return repo.delete(s, doc_id)
