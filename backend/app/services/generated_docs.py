"""
Generated-document registry.

Persists the full HTML body of agent-generated drafts (Research Brief,
Context brief, ...) server-side the moment they're generated, scoped by
a real `workshop_id` (the Postgres `workshops.id`) — mirrors
app.services.prepare_docs exactly (metadata in Postgres, full body in
the content-addressed object store) so a generated draft's "open
document" affordance has something real to fetch.

Public API:
    register(workshop_id, name, html, agent_id='') -> doc record (with doc_id)
    get_html(workshop_id, doc_id) -> str | None
    get_name(workshop_id, doc_id) -> str | None
    delete(workshop_id, doc_id) -> bool
"""

from __future__ import annotations

import uuid

from app.core.logging import log
from app.postgres import session_scope
from app.postgres.repositories import generated_docs as repo
from app.services import object_store


def _obj_key(workshop_id: int, doc_id: str) -> str:
    return f'generated_docs/{workshop_id}/{doc_id}.html'


def register(workshop_id: int, name: str, html: str, agent_id: str = '', *,
            status: str = 'draft', completion_pct: int = 0, author: str = '',
            description: str = '', category: str = '', tags: list | None = None) -> dict:
    """Store the draft's sanitised body_html in the object store, record
    metadata in Postgres, return the record. Returns an empty dict if
    Postgres isn't reachable — the caller (agent_catalog.run_agent)
    already treats a missing docId as a soft failure."""
    doc_id = uuid.uuid4().hex[:16]
    object_store.put_bytes(_obj_key(workshop_id, doc_id), (html or '').encode('utf-8'),
                           content_type='text/html')
    with session_scope() as s:
        if s is None:
            log.warning('[GENERATED_DOCS] Postgres unavailable — %s not registered', name)
            return {}
        row = repo.create(s, doc_id=doc_id, workshop_id=workshop_id, name=(name or 'document')[:200],
                          agent_id=agent_id or '', chars=len(html or ''), status=status,
                          completion_pct=completion_pct, author=author or None,
                          description=(description or '')[:500] or None,
                          category=category or None, tags=tags or [])
        record = {'doc_id': row.doc_id, 'name': row.name, 'agent_id': row.agent_id, 'chars': row.chars,
                  'status': row.status, 'completion_pct': row.completion_pct, 'author': row.author,
                  'description': row.description, 'category': row.category, 'tags': row.tags,
                  'created_at': int(row.created_at.timestamp())}
    log.info('[GENERATED_DOCS] registered %s (%s, agent=%s, %d chars) on workshop=%s',
             record.get('name'), doc_id, agent_id, record.get('chars', 0), workshop_id)
    return record


def list_docs(workshop_id: int) -> list[dict]:
    """[{doc_id,name,agent_id,category,status,completion_pct,author,
    description,tags,created_at}, ...] for the Pre-Workshop Artifacts
    card grid."""
    with session_scope() as s:
        if s is None:
            return []
        rows = repo.list_for_workshop(s, workshop_id)
        return [{'doc_id': d.doc_id, 'name': d.name, 'agent_id': d.agent_id,
                 'category': d.category, 'status': d.status, 'completion_pct': d.completion_pct,
                 'author': d.author, 'description': d.description, 'tags': d.tags,
                 'created_at': int(d.created_at.timestamp())}
                for d in rows]


def get(workshop_id: int, doc_id: str) -> dict | None:
    """Full metadata row (author/category/tags/completion/created_at) for
    one generated doc — used by the Word-export route to build a proper
    document header. Returns None if not found in this workshop."""
    with session_scope() as s:
        if s is None:
            return None
        row = repo.get(s, doc_id)
        if row is None or row.workshop_id != workshop_id:
            return None
        return {'doc_id': row.doc_id, 'name': row.name, 'agent_id': row.agent_id,
                'category': row.category, 'status': row.status, 'completion_pct': row.completion_pct,
                'author': row.author, 'description': row.description, 'tags': row.tags,
                'created_at': int(row.created_at.timestamp())}


def get_html(workshop_id: int, doc_id: str) -> str | None:
    with session_scope() as s:
        if s is None:
            return None
        row = repo.get(s, doc_id)
        if row is None or row.workshop_id != workshop_id:
            return None
    data = object_store.get_bytes(_obj_key(workshop_id, doc_id))
    return data.decode('utf-8', errors='replace') if data is not None else None


def get_name(workshop_id: int, doc_id: str) -> str | None:
    with session_scope() as s:
        if s is None:
            return None
        row = repo.get(s, doc_id)
        if row is None or row.workshop_id != workshop_id:
            return None
        return row.name


def delete(workshop_id: int, doc_id: str) -> bool:
    with session_scope() as s:
        if s is None:
            return False
        row = repo.get(s, doc_id)
        if row is None or row.workshop_id != workshop_id:
            return False
        return repo.delete(s, doc_id)
