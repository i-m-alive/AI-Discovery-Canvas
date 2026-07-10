"""
RAG service facade.

The one surface every consumer (NaviCORE Assistant, document generation,
capability/entity extraction, Knowledge Atlas) should call. It hides the
chunk → embed → FAISS pipeline behind two verbs:

    index_document(...)      preprocess + embed + upsert (incremental)
    retrieve(...)            semantic top-k, scoped by project/workflow

Plus ``retrieve_context(...)`` which formats hits into a prompt-ready
block so callers can prepend "only the most relevant chunks" instead of
dumping whole corpora into the LLM context — the token-optimisation goal.

Everything is best-effort: if FAISS/numpy/openai/the key aren't present,
``is_enabled()`` is False and every call is a clean no-op (indexing
returns 0, retrieval returns []), so the existing graph-context paths keep
working untouched.
"""

from __future__ import annotations

import logging

from app.services.rag import config, embedder, store
from app.services.rag.chunking import chunk_text, html_to_text

log = logging.getLogger('app.rag.service')


# Logical corpora. Keep these stable — they're the on-disk index names.
NS_DOCUMENTS = 'documents'   # generated docs + source/document summaries
NS_PROJECTS  = 'projects'    # project name + description (fuzzy lookup)
NS_ENTITIES  = 'entities'    # extracted entity/relationship statements


def is_enabled() -> bool:
    """True when the full pipeline can run (vector store + embeddings +
    a resolvable key)."""
    return store.faiss_available() and embedder.is_available()


# ── Indexing ─────────────────────────────────────────────────────────
def index_document(*, doc_id: str, text: str,
                   namespace: str = NS_DOCUMENTS,
                   metadata: dict | None = None,
                   is_html: bool = False,
                   tag: str = '[RAG/INDEX]') -> int:
    """Chunk, embed, and (re)index one document. Idempotent per ``doc_id``
    — prior chunks for the same id are replaced, so this doubles as the
    incremental-update path. Returns the number of chunks indexed."""
    if not is_enabled() or not doc_id:
        return 0
    body = html_to_text(text) if is_html else (text or '')
    chunks = chunk_text(body)
    ns = store.namespace(namespace)
    if not chunks:
        # Empty/blank document → drop any stale vectors for it.
        ns.delete_doc(doc_id)
        ns.persist()
        return 0
    meta = dict(metadata or {})
    try:
        vectors = embedder.embed_texts(chunks, tag=tag)
    except Exception as e:
        log.warning('%s embed failed for doc=%s (%s)', tag, doc_id, e)
        return 0
    chunk_recs = [
        {'chunk_id': f'{doc_id}#{i}', 'text': chunks[i],
         'meta': {**meta, 'chunk_index': i, 'chunk_count': len(chunks)}}
        for i in range(len(chunks))
    ]
    written = ns.upsert_doc(doc_id, vectors, chunk_recs)
    ns.persist()
    return written


def index_documents(items: list[dict], *, namespace: str = NS_DOCUMENTS,
                    tag: str = '[RAG/INDEX]') -> int:
    """Bulk index. Each item: {id, text, metadata?, is_html?}. Embeds all
    chunks across all docs in one batched/parallel pass (so the rate
    limiter amortises), then writes per-doc. Returns total chunks."""
    if not is_enabled() or not items:
        return 0
    ns = store.namespace(namespace)
    # Flatten to chunks first so embedding is one big batched call.
    flat_texts: list[str] = []
    plan: list[tuple[str, dict, list[int]]] = []   # (doc_id, meta, row_indices)
    for it in items:
        doc_id = it.get('id')
        if not doc_id:
            continue
        body = html_to_text(it.get('text') or '') if it.get('is_html') else (it.get('text') or '')
        chunks = chunk_text(body)
        if not chunks:
            ns.delete_doc(doc_id)
            continue
        start = len(flat_texts)
        flat_texts.extend(chunks)
        plan.append((doc_id, dict(it.get('metadata') or {}),
                     list(range(start, start + len(chunks)))))
    if not flat_texts:
        ns.persist()
        return 0
    try:
        vectors = embedder.embed_texts(flat_texts, tag=tag)
    except Exception as e:
        log.warning('%s bulk embed failed (%s)', tag, e)
        return 0
    total = 0
    for doc_id, meta, rows in plan:
        sub = vectors[rows]
        recs = [
            {'chunk_id': f'{doc_id}#{j}', 'text': flat_texts[r],
             'meta': {**meta, 'chunk_index': j, 'chunk_count': len(rows)}}
            for j, r in enumerate(rows)
        ]
        total += ns.upsert_doc(doc_id, sub, recs)
    ns.persist()
    log.info('%s indexed %d chunks across %d docs in ns=%s', tag, total, len(plan), namespace)
    return total


def delete_document(doc_id: str, *, namespace: str = NS_DOCUMENTS) -> int:
    if not store.faiss_available() or not doc_id:
        return 0
    ns = store.namespace(namespace)
    n = ns.delete_doc(doc_id)
    ns.persist()
    return n


def list_doc_ids(prefix: str = '', *, namespace: str = NS_DOCUMENTS) -> list[str]:
    """All doc_ids in a namespace, optionally restricted to those starting with
    `prefix`. Read-only. Used to delete every disc of a source whose node id is
    reused for multiple URLs."""
    if not store.faiss_available():
        return []
    ns = store.namespace(namespace)
    ns._ensure_loaded()
    return [d for d in list(ns._doc_index.keys()) if not prefix or d.startswith(prefix)]


# ── Retrieval ────────────────────────────────────────────────────────
def _scope_predicate(project_id: str | None, workflow_id: str | None):
    if not project_id and not workflow_id:
        return None

    def _where(meta: dict) -> bool:
        if project_id and meta.get('project_id') not in (project_id, None, ''):
            # Keep org-level (project-less) chunks visible to any scope,
            # but exclude chunks owned by a *different* project.
            if meta.get('project_id'):
                return False
        if workflow_id and meta.get('workflow_id') and meta.get('workflow_id') != workflow_id:
            return False
        return True

    return _where


def retrieve(query: str, *,
             namespace: str = NS_DOCUMENTS,
             k: int | None = None,
             project_id: str | None = None,
             workflow_id: str | None = None,
             min_score: float | None = None,
             tag: str = '[RAG/QUERY]') -> list[dict]:
    """Semantic top-k. Returns hits: {score, chunk_id, doc_id, text, meta}.
    Scoped to a project/workflow when ids are supplied (org-level chunks
    stay visible). Never raises — returns [] on any failure."""
    if not is_enabled() or not (query or '').strip():
        return []
    try:
        qv = embedder.embed_query(query, tag=tag)
    except Exception as e:
        log.warning('%s query embed failed (%s)', tag, e)
        return []
    ns = store.namespace(namespace)
    return ns.search(
        qv,
        k or config.RETRIEVE_TOP_K,
        where=_scope_predicate(project_id, workflow_id),
        min_score=(config.RETRIEVE_MIN_SCORE if min_score is None else min_score),
    )


def retrieve_context(query: str, *,
                     namespace: str = NS_DOCUMENTS,
                     k: int | None = None,
                     project_id: str | None = None,
                     workflow_id: str | None = None,
                     max_chars: int = 6000,
                     tag: str = '[RAG/QUERY]') -> tuple[str, list[dict]]:
    """Retrieve + format hits into a prompt-ready block, capped at
    ``max_chars`` so the augmentation itself stays token-bounded. Returns
    (context_text, hits). Empty string when nothing relevant is found."""
    hits = retrieve(query, namespace=namespace, k=k,
                    project_id=project_id, workflow_id=workflow_id, tag=tag)
    if not hits:
        return '', []
    lines, used = [], 0
    for i, h in enumerate(hits, 1):
        label = (h.get('meta') or {}).get('label') or (h.get('meta') or {}).get('kind') or 'excerpt'
        block = f'[{i}] ({label}, relevance {h["score"]:.2f})\n{h["text"].strip()}'
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    return '\n\n'.join(lines), hits


def select_relevant(text: str, queries: list[str], *,
                    top_k_per_query: int = 6,
                    max_context_chars: int = 40000,
                    min_chunks_to_trigger: int = 2,
                    tag: str = '[RAG/SELECT]') -> str:
    """Ephemeral, in-memory faceted selection over a SINGLE blob of text
    — NOT the persistent FAISS store.

    Use it to shrink a large *in-flight* document (e.g. the merged
    summary at entity-extraction time, which isn't indexed yet) before an
    LLM call: chunk → embed → for each facet query keep its top-k chunks →
    union (dedup) → re-join in original order, bounded to
    ``max_context_chars``. This replaces blind first-N-chars truncation:
    fewer tokens AND nothing relevant is lost from the truncated tail.

    Returns the focused context string, or '' when the subsystem is
    disabled or the text is too small to be worth selecting — the caller
    then falls back to its own truncation.
    """
    if not is_enabled() or not (text or '').strip() or not queries:
        return ''
    try:
        import numpy as np
    except Exception:
        return ''
    from app.services.rag.chunking import chunk_text as _chunk
    chunks = _chunk(text)
    if len(chunks) < max(2, min_chunks_to_trigger):
        return ''
    try:
        cvecs = embedder.embed_texts(chunks, tag=tag)
        qvecs = embedder.embed_texts([str(q) for q in queries], tag=tag)
    except Exception as e:
        log.warning('%s embed failed (%s)', tag, e)
        return ''
    sims = qvecs @ cvecs.T                       # (q, n) cosine (rows normalised)
    best = sims.max(axis=0)                       # per-chunk best score across facets
    rankings = [list(np.argsort(-sims[qi])) for qi in range(sims.shape[0])]
    candidates: list[int] = []
    seen: set[int] = set()
    # Round-robin so every facet contributes its top chunks as candidates.
    for rank in range(top_k_per_query):
        for rk in rankings:
            if rank < len(rk):
                idx = int(rk[rank])
                if idx not in seen:
                    seen.add(idx)
                    candidates.append(idx)
    # Fill the char budget by DESCENDING relevance (so a tight budget keeps
    # the most relevant chunks, not merely the earliest)…
    candidates.sort(key=lambda i: -float(best[i]))
    kept: list[int] = []
    used = 0
    for idx in candidates:
        c = chunks[idx]
        if used + len(c) + 2 > max_context_chars:
            continue
        kept.append(idx)
        used += len(c) + 2
    # …then restore original document order for a coherent context block.
    kept.sort()
    focused = '\n\n'.join(chunks[i] for i in kept)
    log.info('%s selected %d/%d chunks (%d→%d chars)',
             tag, len(kept), len(chunks), len(text), len(focused))
    return focused


def stats() -> dict:
    return {
        'enabled':    is_enabled(),
        'configured': config.is_configured(),
        'model':      config.EMBED_MODEL,
        'dim':        config.EMBED_DIM,
        'namespaces': store.all_stats(),
    }
