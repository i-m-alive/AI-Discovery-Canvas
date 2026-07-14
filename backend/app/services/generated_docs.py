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

import threading
import uuid

from app.core.logging import log
from app.postgres import session_scope
from app.postgres.repositories import generated_docs as repo
from app.services import object_store


def _obj_key(workshop_id: int, doc_id: str) -> str:
    return f'generated_docs/{workshop_id}/{doc_id}.html'


def _index_async(workshop_id: int, doc_id: str, name: str, agent_id: str, html: str) -> None:
    """Embed this generated doc into the RAG corpus on a daemon thread —
    what makes Copilot able to answer from its own past outputs (research
    briefs, risk assessments, summaries), not just original uploads.
    Reuses rag.indexer.index_generated_doc (written for exactly this, but
    previously never called from anywhere). Best-effort: an indexing
    failure never fails the register() that produced the document."""
    def _run():
        try:
            from app.services.rag import indexer
            n = indexer.index_generated_doc(
                doc_id=doc_id, html=html,
                label=name, doc_type=agent_id or 'generated_doc',
                workflow_id=str(workshop_id))
            if n:
                log.info('[GENERATED_DOCS] indexed %s into RAG (%d chunks)', doc_id, n)
        except Exception as e:
            log.info('[GENERATED_DOCS] RAG indexing skipped for %s (%s)', doc_id, e.__class__.__name__)
    threading.Thread(target=_run, name=f'gdoc-index-{doc_id}', daemon=True).start()


def register(workshop_id: int, name: str, html: str, agent_id: str = '', *,
            status: str = 'draft', completion_pct: int = 0, author: str = '',
            description: str = '', category: str = '', tags: list | None = None,
            diagram_xml: str | None = None, diagram_json: list | None = None,
            next_steps: list | None = None, analysis_json: dict | None = None) -> dict:
    """Store the draft's sanitised body_html in the object store, record
    metadata in Postgres, return the record. Returns an empty dict if
    Postgres isn't reachable — the caller (agent_catalog.run_agent)
    already treats a missing docId as a soft failure.
    `diagram_xml`/`diagram_json`/`next_steps` are set by the 'workflow'
    agent, and by 'deepresearch' when the facilitator's own instruction
    asked for a workflow — persisted so the Artifacts grid can still offer
    "View diagram"/"Download .drawio" after a reload, not just in the
    one-off run_agent response."""
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
                          category=category or None, tags=tags or [],
                          diagram_xml=diagram_xml, diagram_json=diagram_json, next_steps=next_steps,
                          analysis_json=analysis_json)
        record = {'doc_id': row.doc_id, 'name': row.name, 'agent_id': row.agent_id, 'chars': row.chars,
                  'status': row.status, 'completion_pct': row.completion_pct, 'author': row.author,
                  'description': row.description, 'category': row.category, 'tags': row.tags,
                  'created_at': int(row.created_at.timestamp())}
    log.info('[GENERATED_DOCS] registered %s (%s, agent=%s, %d chars) on workshop=%s',
             record.get('name'), doc_id, agent_id, record.get('chars', 0), workshop_id)
    _index_async(workshop_id, doc_id, record['name'], agent_id, html or '')
    return record


def list_docs(workshop_id: int) -> list[dict]:
    """[{doc_id,name,agent_id,category,status,completion_pct,author,
    description,tags,created_at,has_diagram,next_steps}, ...] for the
    Pre-Workshop Artifacts card grid. `has_diagram` (bool) is a cheap flag
    for the grid to decide whether to offer "View diagram" — the actual
    (possibly large) XML is fetched lazily via get_diagram()."""
    with session_scope() as s:
        if s is None:
            return []
        rows = repo.list_for_workshop(s, workshop_id)
        return [{'doc_id': d.doc_id, 'name': d.name, 'agent_id': d.agent_id,
                 'category': d.category, 'status': d.status, 'completion_pct': d.completion_pct,
                 'author': d.author, 'description': d.description, 'tags': d.tags,
                 'created_at': int(d.created_at.timestamp()),
                 'has_diagram': bool(d.diagram_xml), 'next_steps': d.next_steps or [],
                 'has_analysis': bool(d.analysis_json)}
                for d in rows]


def get_analysis(workshop_id: int, doc_id: str) -> dict | None:
    """{gaps, readiness, research_topics} for a persisted Pre-Workshop
    Analysis — None if this doc has none / isn't in this workshop."""
    with session_scope() as s:
        if s is None:
            return None
        row = repo.get(s, doc_id)
        if row is None or row.workshop_id != workshop_id or not row.analysis_json:
            return None
        return dict(row.analysis_json)


def get_diagram(workshop_id: int, doc_id: str) -> dict | None:
    """{xml, diagrams} for a persisted generated doc's workflow diagram —
    None if this doc has none, isn't found, or belongs to another
    workshop."""
    with session_scope() as s:
        if s is None:
            return None
        row = repo.get(s, doc_id)
        if row is None or row.workshop_id != workshop_id or not row.diagram_xml:
            return None
        return {'xml': row.diagram_xml, 'diagrams': row.diagram_json or []}


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
        ok = repo.delete(s, doc_id)
    if ok:
        # Drop its vectors too — a deleted doc must stop surfacing in
        # Copilot/RAG answers. 'gdoc:' prefix matches _index_async's key.
        try:
            from app.services import rag
            rag.delete_document(f'gdoc:{doc_id}')
        except Exception as e:
            log.info('[GENERATED_DOCS] RAG de-index skipped for %s (%s)', doc_id, e.__class__.__name__)
        try:
            object_store.delete_key(_obj_key(workshop_id, doc_id))
        except Exception as e:
            log.info('[GENERATED_DOCS] object-store cleanup skipped for %s (%s)', doc_id, e.__class__.__name__)
    return ok
