"""
FAISS vector store.

A thin, persistent, incrementally-updatable wrapper around FAISS, split
into **namespaces** (one index per logical corpus: ``documents``,
``summaries``, ``entities``, …). Each namespace is an
``IndexIDMap2(IndexFlatIP)`` — exact cosine (vectors arrive
L2-normalised) with stable int64 ids so we can add and **remove** vectors
in place. Flat is the right default at NaviCORE's scale (robust, exact,
delete-friendly); swap to IVF/HNSW later by changing ``_new_index`` if
the corpus reaches millions of vectors.

Per namespace we persist two files under ``data/rag/``:

    <ns>.faiss        the FAISS index
    <ns>.meta.json    id↔chunk metadata + the doc→chunk-ids map used for
                      incremental document replacement

Documents are the unit of update: re-indexing a document deletes its old
chunk vectors and adds the new ones, so edits/renames/regenerations never
leave stale vectors behind. Everything degrades to a clean no-op when
faiss/numpy aren't installed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional

from app.services.rag import config

try:
    import numpy as np
except Exception:                                   # pragma: no cover
    np = None

try:
    import faiss
    _FAISS_OK = True
except Exception:                                   # pragma: no cover
    _FAISS_OK = False


log = logging.getLogger('app.rag.store')


def faiss_available() -> bool:
    return _FAISS_OK and np is not None


class FaissNamespace:
    """One FAISS index + its metadata sidecar."""

    def __init__(self, name: str, dim: int | None = None):
        self.name = name
        self.dim = dim or config.EMBED_DIM
        self._lock = threading.RLock()
        self._index = None
        self._next_id = 1
        self._items: dict[int, dict] = {}        # int64 → {chunk_id, doc_id, text, meta}
        self._doc_index: dict[str, list[int]] = {}  # doc_id → [int64 ...]
        self._loaded = False
        d = config.data_dir()
        self._index_path = d / f'{name}.faiss'
        self._meta_path  = d / f'{name}.meta.json'

    # ── lifecycle ────────────────────────────────────────────────────
    def _new_index(self):
        return faiss.IndexIDMap2(faiss.IndexFlatIP(self.dim))

    def _ensure_loaded(self):
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            if not faiss_available():
                return
            try:
                if self._index_path.exists() and self._meta_path.exists():
                    self._index = faiss.read_index(str(self._index_path))
                    meta = json.loads(self._meta_path.read_text(encoding='utf-8'))
                    self.dim = meta.get('dim', self.dim)
                    self._next_id = meta.get('next_id', 1)
                    self._items = {int(k): v for k, v in (meta.get('items') or {}).items()}
                    self._doc_index = {k: list(v) for k, v in (meta.get('doc_index') or {}).items()}
                    log.info('[RAG/STORE] loaded ns=%s: %d vectors, %d docs',
                             self.name, len(self._items), len(self._doc_index))
                else:
                    self._index = self._new_index()
            except Exception as e:
                log.warning('[RAG/STORE] load ns=%s failed (%s) — fresh index', self.name, e)
                self._index = self._new_index()
                self._items, self._doc_index, self._next_id = {}, {}, 1

    def persist(self):
        if not faiss_available():
            return
        with self._lock:
            self._ensure_loaded()
            try:
                self._index_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_idx = self._index_path.with_suffix('.faiss.tmp')
                faiss.write_index(self._index, str(tmp_idx))
                os.replace(tmp_idx, self._index_path)
                meta = {
                    'dim':       self.dim,
                    'next_id':   self._next_id,
                    'metric':    'ip',
                    'items':     {str(k): v for k, v in self._items.items()},
                    'doc_index': self._doc_index,
                }
                tmp_meta = self._meta_path.with_suffix('.json.tmp')
                tmp_meta.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
                os.replace(tmp_meta, self._meta_path)
            except Exception as e:
                log.warning('[RAG/STORE] persist ns=%s failed (%s)', self.name, e)

    # ── mutation ─────────────────────────────────────────────────────
    def delete_doc(self, doc_id: str) -> int:
        """Remove every chunk vector belonging to ``doc_id``. Returns the
        count removed. Caller already-or-not holding the lock both work."""
        if not faiss_available():
            return 0
        with self._lock:
            self._ensure_loaded()
            ids = self._doc_index.pop(doc_id, [])
            if not ids:
                return 0
            try:
                self._index.remove_ids(np.asarray(ids, dtype='int64'))
            except Exception as e:
                log.warning('[RAG/STORE] remove_ids ns=%s doc=%s failed (%s)',
                            self.name, doc_id, e)
            for i in ids:
                self._items.pop(i, None)
            return len(ids)

    def upsert_doc(self, doc_id: str, vectors, chunks: list[dict]) -> int:
        """Replace ``doc_id``'s vectors with a fresh set.

        ``vectors`` is an (m, dim) normalised matrix; ``chunks`` is a list
        of m dicts ``{chunk_id, text, meta}`` aligned with the rows.
        Returns the number of vectors written.
        """
        if not faiss_available():
            return 0
        with self._lock:
            self._ensure_loaded()
            self.delete_doc(doc_id)
            m = 0 if vectors is None else len(vectors)
            if not m:
                return 0
            ids = []
            for row in range(m):
                vid = self._next_id
                self._next_id += 1
                ids.append(vid)
                ch = chunks[row] if row < len(chunks) else {}
                self._items[vid] = {
                    'chunk_id': ch.get('chunk_id') or f'{doc_id}#{row}',
                    'doc_id':   doc_id,
                    'text':     ch.get('text') or '',
                    'meta':     ch.get('meta') or {},
                }
            try:
                self._index.add_with_ids(
                    np.asarray(vectors, dtype='float32'),
                    np.asarray(ids, dtype='int64'),
                )
            except Exception as e:
                log.warning('[RAG/STORE] add_with_ids ns=%s doc=%s failed (%s)',
                            self.name, doc_id, e)
                for i in ids:
                    self._items.pop(i, None)
                return 0
            self._doc_index[doc_id] = ids
            return m

    # ── query ────────────────────────────────────────────────────────
    def search(self, query_vec, k: int,
               where: Optional[Callable[[dict], bool]] = None,
               min_score: float = 0.0) -> list[dict]:
        """Top-k by cosine. ``where`` post-filters on each hit's metadata
        dict (so scoped retrieval — by project_id/workflow_id — is a
        predicate). Over-fetches when filtering so the filter doesn't
        starve k."""
        if not faiss_available():
            return []
        with self._lock:
            self._ensure_loaded()
            if self._index is None or self._index.ntotal == 0:
                return []
            fetch = k if where is None else min(self._index.ntotal, max(k * 8, k + 32))
            q = np.asarray([query_vec], dtype='float32')
            scores, ids = self._index.search(q, int(fetch))
            hits = []
            for score, vid in zip(scores[0], ids[0]):
                if vid == -1:
                    continue
                rec = self._items.get(int(vid))
                if not rec:
                    continue
                if score < min_score:
                    continue
                meta = rec.get('meta') or {}
                if where is not None and not where(meta):
                    continue
                hits.append({
                    'score':    float(score),
                    'chunk_id': rec.get('chunk_id'),
                    'doc_id':   rec.get('doc_id'),
                    'text':     rec.get('text') or '',
                    'meta':     meta,
                })
                if len(hits) >= k:
                    break
            return hits

    def stats(self) -> dict:
        self._ensure_loaded()
        with self._lock:
            return {
                'namespace': self.name,
                'vectors':   (self._index.ntotal if (faiss_available() and self._index is not None) else 0),
                'documents': len(self._doc_index),
                'dim':       self.dim,
            }


# ── Namespace registry ───────────────────────────────────────────────
_NAMESPACES: dict[str, FaissNamespace] = {}
_REG_LOCK = threading.Lock()


def namespace(name: str, dim: int | None = None) -> FaissNamespace:
    """`dim` only matters the first time `name` is created with no
    existing on-disk index — see `FaissNamespace.__init__`/`_ensure_loaded`
    (a persisted index always restores its own dim from the meta sidecar).
    This lets a brand-new per-provider namespace (see rag/service.py's
    `_ns_name`) start at the right width instead of defaulting to
    Bedrock's `config.EMBED_DIM`."""
    with _REG_LOCK:
        ns = _NAMESPACES.get(name)
        if ns is None:
            ns = FaissNamespace(name, dim=dim)
            _NAMESPACES[name] = ns
        return ns


def all_stats() -> list[dict]:
    """Stats for every namespace that has a persisted index on disk plus
    any loaded in this process."""
    names = set(_NAMESPACES.keys())
    try:
        d = config.data_dir()
        if d.exists():
            for p in d.glob('*.faiss'):
                names.add(p.stem)
    except Exception:
        pass
    return [namespace(n).stats() for n in sorted(names)]


def persist_all() -> None:
    for ns in list(_NAMESPACES.values()):
        ns.persist()
