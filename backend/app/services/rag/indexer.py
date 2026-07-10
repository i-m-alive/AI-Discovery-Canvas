"""
Ingestion → embedding indexer.

Pulls the unstructured corpus out of Neo4j (generated documents + per-
source summaries) plus project metadata, and feeds it through the RAG
service so it's searchable. Designed for two access patterns:

* **Incremental** — ``index_generated_doc`` / ``index_project`` are called
  as data is created (a pipeline emits a doc, a project is ingested), so
  the vector index tracks the graph without a full rebuild.
* **Bulk** — ``reindex_all`` walks every project once (cold start, or
  after changing the embedding model / chunk settings).

Background ``kickoff_*`` variants run the work on a daemon thread with an
in-flight guard, mirroring the capability-map generator, so request
handlers return immediately and never block on embedding latency.

Every function is best-effort: it logs and returns 0 on any failure and
no-ops entirely when ``service.is_enabled()`` is False.
"""

from __future__ import annotations

import hashlib
import logging
import threading

from app.services.rag import service

log = logging.getLogger('app.rag.indexer')


def _hash(s: str) -> str:
    return hashlib.sha1((s or '').encode('utf-8')).hexdigest()[:16]


# ── per-document entry points ────────────────────────────────────────
def index_generated_doc(*, doc_id: str, html: str,
                        project_id: str | None = None,
                        project_name: str = '',
                        label: str = '',
                        doc_type: str = '',
                        node_id: str | None = None,
                        workflow_id: str | None = None) -> int:
    """Index one generated document's HTML body. ``doc_id`` is the
    GeneratedDoc node id; the index key is namespaced so it can't collide
    with summaries."""
    if not service.is_enabled() or not doc_id or not (html or '').strip():
        return 0
    return service.index_document(
        doc_id=f'gdoc:{doc_id}',
        text=html,
        is_html=True,
        namespace=service.NS_DOCUMENTS,
        metadata={
            'project_id':   project_id or '',
            'project_name': project_name or '',
            'workflow_id':  workflow_id or '',
            'kind':         doc_type or 'generated_doc',
            'label':        label or doc_type or 'Generated document',
            'source':       'generated_doc',
        },
        tag='[RAG/INDEX/DOC]',
    )


def index_project(project_id: str) -> dict:
    """(Re)index everything attached to one project: its source summaries,
    generated documents, and the project's own name+description. Returns
    {summaries, documents, project} chunk counts."""
    out = {'summaries': 0, 'documents': 0, 'project': 0}
    if not service.is_enabled() or not project_id:
        return out
    from app.database import neo4j_client as neo4j_store

    # 1. Project name + description → fuzzy-lookup corpus.
    try:
        rows = neo4j_store.read(
            "MATCH (p:Project {id: $id}) "
            "RETURN p.name AS name, p.description AS description",
            id=project_id,
        )
        if rows:
            name = (rows[0].get('name') or '').strip()
            desc = (rows[0].get('description') or '').strip()
            text = (name + '\n\n' + desc).strip()
            if text:
                out['project'] = service.index_document(
                    doc_id=f'project:{project_id}',
                    text=text,
                    namespace=service.NS_PROJECTS,
                    metadata={'project_id': project_id, 'project_name': name,
                              'kind': 'project', 'label': name},
                    tag='[RAG/INDEX/PROJ]',
                )
    except Exception as e:
        log.warning('[RAG/INDEX] project meta %s failed (%s)', project_id[:8], e)

    project_name = ''
    try:
        project_name = (rows[0].get('name') or '') if rows else ''
    except Exception:
        project_name = ''

    # 2. Per-source summaries.
    try:
        summaries = neo4j_store.read(
            "MATCH (s:Source)-[:SOURCE_PRODUCED_SUMMARY]->(sm:Summary) "
            "WHERE coalesce(s.project_id,'') = $id "
            "RETURN s.label AS label, s.kind AS kind, sm.text AS text, "
            "       sm.id AS sid "
            "ORDER BY sm.created_at DESC LIMIT 200",
            id=project_id,
        )
        items = []
        for s in summaries:
            text = (s.get('text') or '').strip()
            if not text:
                continue
            sid = s.get('sid') or _hash((s.get('label') or '') + text)
            items.append({
                'id':   f'summary:{sid}',
                'text': text,
                'metadata': {
                    'project_id':   project_id,
                    'project_name': project_name,
                    'kind':         (s.get('kind') or 'summary'),
                    'label':        (s.get('label') or 'Source summary'),
                    'source':       'summary',
                },
            })
        if items:
            out['summaries'] = service.index_documents(items, tag='[RAG/INDEX/SUMM]')
    except Exception as e:
        log.warning('[RAG/INDEX] summaries %s failed (%s)', project_id[:8], e)

    # 3. Generated documents.
    try:
        docs = neo4j_store.read(
            "MATCH (g:GeneratedDoc) WHERE coalesce(g.project_id,'') = $id "
            "RETURN g.id AS id, g.doc_label AS label, g.doc_type AS doc_type, "
            "       g.html AS html, g.workflow_id AS workflow_id, "
            "       g.node_id AS node_id LIMIT 200",
            id=project_id,
        )
        items = []
        for d in docs:
            html = (d.get('html') or '')
            if not html.strip():
                continue
            items.append({
                'id':      f'gdoc:{d.get("id")}',
                'text':    html,
                'is_html': True,
                'metadata': {
                    'project_id':   project_id,
                    'project_name': project_name,
                    'workflow_id':  d.get('workflow_id') or '',
                    'kind':         d.get('doc_type') or 'generated_doc',
                    'label':        d.get('label') or d.get('doc_type') or 'Generated document',
                    'source':       'generated_doc',
                },
            })
        if items:
            out['documents'] = service.index_documents(items, tag='[RAG/INDEX/DOC]')
    except Exception as e:
        log.warning('[RAG/INDEX] generated docs %s failed (%s)', project_id[:8], e)

    log.info('[RAG/INDEX] project %s indexed: %s', project_id[:8], out)
    return out


def reindex_all(*, limit_projects: int | None = None) -> dict:
    """Full cold-start build: index every project in the graph. Returns a
    summary {projects, summaries, documents, project_meta}."""
    summary = {'projects': 0, 'summaries': 0, 'documents': 0, 'project_meta': 0}
    if not service.is_enabled():
        summary['disabled'] = True
        return summary
    try:
        from app.database import KG
        projects = KG.list_projects()
    except Exception as e:
        log.warning('[RAG/REINDEX] could not list projects (%s)', e)
        return summary
    if limit_projects:
        projects = projects[:limit_projects]
    for p in projects:
        pid = p.get('id')
        if not pid:
            continue
        r = index_project(pid)
        summary['projects'] += 1
        summary['summaries'] += r.get('summaries', 0)
        summary['documents'] += r.get('documents', 0)
        summary['project_meta'] += r.get('project', 0)
    log.info('[RAG/REINDEX] done: %s', summary)
    return summary


# ── background kickoffs ──────────────────────────────────────────────
_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()


def _spawn(key: str, fn, *args, **kwargs) -> bool:
    """Run fn on a daemon thread unless an identical job is in flight."""
    if not service.is_enabled():
        return False
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return False
        _INFLIGHT.add(key)

    def _worker():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            log.warning('[RAG/INDEX] background %s failed (%s)', key, e)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(key)

    threading.Thread(target=_worker, daemon=True,
                     name=f'rag-{key[:24]}').start()
    return True


def kickoff_index_project(project_id: str) -> bool:
    if not project_id:
        return False
    return _spawn(f'proj:{project_id}', index_project, project_id)


def kickoff_index_generated_doc(**kwargs) -> bool:
    doc_id = kwargs.get('doc_id') or ''
    if not doc_id:
        return False
    return _spawn(f'gdoc:{doc_id}', index_generated_doc, **kwargs)


def kickoff_reindex_all(**kwargs) -> bool:
    return _spawn('reindex_all', reindex_all, **kwargs)


# ── Workflow-output entry points ─────────────────────────────────────

def index_workflow_output(*,
                          doc_id: str,
                          data_b64: str = '',
                          mime_type: str = '',
                          file_ext: str = '',
                          html: str = '',
                          project_id: str | None = None,
                          project_name: str = '',
                          label: str = '',
                          doc_type: str = '',
                          node_id: str | None = None,
                          workflow_id: str | None = None) -> int:
    """Index a workflow output file by extracting text from its binary payload
    (data_b64 / mime_type / file_ext) and falling back to HTML when the
    extractor yields nothing. Tagged source_type='workflow_output' so
    the chat pipeline can prioritise these chunks for project_purpose queries.
    Returns chunk count; 0 on any failure."""
    if not service.is_enabled() or not doc_id:
        return 0
    text = ''
    if data_b64:
        try:
            from app.services.rag.file_extractor import extract_from_base64
            text = extract_from_base64(data_b64,
                                       mime_type=mime_type,
                                       file_ext=file_ext)
        except Exception as exc:
            log.warning('[RAG/INDEX/WFOUT] extractor failed doc=%s (%s)',
                        doc_id[:8], exc)
    if not text.strip() and (html or '').strip():
        from app.services.rag.chunking import html_to_text
        text = html_to_text(html) if html.lstrip().startswith('<') else html
    if not text.strip():
        return 0
    return service.index_document(
        doc_id=f'wfout:{doc_id}',
        text=text,
        is_html=False,
        namespace=service.NS_DOCUMENTS,
        metadata={
            'project_id':   project_id or '',
            'project_name': project_name or '',
            'workflow_id':  workflow_id or '',
            'kind':         doc_type or 'workflow_output',
            'label':        label or doc_type or 'Workflow output',
            'source':       'workflow_output',
            'source_type':  'workflow_output',
        },
        tag='[RAG/INDEX/WFOUT]',
    )


def kickoff_index_workflow_output(**kwargs) -> bool:
    """Background variant of index_workflow_output — identical dedup guard."""
    doc_id = kwargs.get('doc_id') or ''
    if not doc_id:
        return False
    return _spawn(f'wfout:{doc_id}', index_workflow_output, **kwargs)


def backfill_workflow_outputs(*,
                               project_id: str | None = None,
                               limit: int = 500) -> dict:
    """One-time backfill: walk every GeneratedDoc that carries a data_b64
    payload and index it as a workflow_output chunk. Idempotent — already-
    indexed docs simply get their vectors replaced.

    Returns a summary dict {processed, indexed_chunks, skipped, errors}."""
    summary: dict = {'processed': 0, 'indexed_chunks': 0, 'skipped': 0, 'errors': 0}
    if not service.is_enabled():
        summary['disabled'] = True
        return summary
    try:
        from app.database import neo4j_client as _neo
        where = 'WHERE g.data_b64 IS NOT NULL AND g.data_b64 <> ""'
        if project_id:
            safe = project_id.replace("'", '')
            where += f" AND coalesce(g.project_id,'') = '{safe}'"
        rows = _neo.read(
            f'MATCH (g:GeneratedDoc) {where} '
            'RETURN g.id AS id, g.project_id AS project_id, '
            '       g.doc_label AS label, g.doc_type AS doc_type, '
            '       g.data_b64 AS data_b64, g.mime_type AS mime_type, '
            '       g.file_ext AS file_ext, g.workflow_id AS workflow_id '
            f'LIMIT {int(limit)}',
        )
    except Exception as exc:
        log.warning('[RAG/BACKFILL/WF] query failed: %s', exc)
        summary['errors'] += 1
        return summary
    for row in rows:
        doc_id = row.get('id')
        if not doc_id:
            summary['skipped'] += 1
            continue
        try:
            n = index_workflow_output(
                doc_id=doc_id,
                data_b64=row.get('data_b64') or '',
                mime_type=row.get('mime_type') or '',
                file_ext=row.get('file_ext') or '',
                project_id=row.get('project_id'),
                label=row.get('label') or '',
                doc_type=row.get('doc_type') or '',
                workflow_id=row.get('workflow_id'),
            )
            summary['processed'] += 1
            summary['indexed_chunks'] += n
        except Exception as exc:
            log.warning('[RAG/BACKFILL/WF] doc=%s failed: %s', doc_id, exc)
            summary['errors'] += 1
    log.info('[RAG/BACKFILL/WF] done: %s', summary)
    return summary
