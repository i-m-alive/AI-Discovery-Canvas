"""
Retrieval-Augmented (RAG) subsystem.

A FAISS + Azure OpenAI ``text-embedding-3-large`` pipeline that lets every
consumer retrieve only the most relevant chunks/entities instead of
stuffing whole corpora into the LLM context — the scalability + token-
optimisation goal as NaviCORE's project count grows.

Layers
------
    config    env-driven settings (endpoint, deployment, quotas, paths)
    embedder  batched + parallel + rate-limited embedding generation
    cache     content-hash embedding cache (skip re-embedding)
    chunking  semantic chunking (structure-aware, token-bounded)
    store     FAISS namespaces with incremental upsert/delete + persistence
    service   the facade: index_document / retrieve / retrieve_context
    indexer   Neo4j → embeddings ingestion (incremental + bulk reindex)

Public surface (import from ``app.services.rag``):

    is_enabled()                          full pipeline ready?
    retrieve(query, ...)                  semantic top-k hits
    retrieve_context(query, ...)          prompt-ready block + hits
    index_document(...) / index_documents(...)
    delete_document(...)
    stats()                               diagnostics for /rag/stats

Everything is best-effort: when FAISS / numpy / openai / the Key Vault
secret aren't available, ``is_enabled()`` is False and all calls no-op,
so the existing graph-context code paths keep working unchanged.
"""

from __future__ import annotations

from app.services.rag.service import (          # noqa: F401
    NS_DOCUMENTS,
    NS_ENTITIES,
    NS_PROJECTS,
    delete_document,
    index_document,
    index_documents,
    is_enabled,
    retrieve,
    retrieve_context,
    select_relevant,
    stats,
)

__all__ = [
    'NS_DOCUMENTS',
    'NS_ENTITIES',
    'NS_PROJECTS',
    'delete_document',
    'index_document',
    'index_documents',
    'is_enabled',
    'retrieve',
    'retrieve_context',
    'select_relevant',
    'stats',
]
