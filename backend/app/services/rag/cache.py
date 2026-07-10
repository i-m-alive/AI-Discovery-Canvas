"""
Embedding cache.

Embeddings are deterministic for a given (model, text), so we never pay
to embed the same chunk twice — across a single ingest run AND across
process restarts. The key is ``sha256(model | text)``; the value is the
float32 vector.

Persistence is a single ``.npz`` (keys + matrix) written atomically. The
cache is bounded: once it exceeds ``RAG_EMBED_CACHE_MAX`` entries the
oldest-loaded rows are evicted on the next flush. Everything degrades to
an in-memory-only dict if numpy isn't importable, so a slim image still
works (just without cross-restart reuse).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

from app.services.rag import config

try:
    import numpy as np
except Exception:                                   # pragma: no cover
    np = None


log = logging.getLogger('app.rag.cache')

_MAX_ENTRIES = int(os.environ.get('RAG_EMBED_CACHE_MAX', '200000'))


def key_for(model: str, text: str) -> str:
    h = hashlib.sha256()
    h.update((model or '').encode('utf-8'))
    h.update(b'\x00')
    h.update((text or '').encode('utf-8'))
    return h.hexdigest()


class EmbeddingCache:
    """Process-wide, thread-safe, disk-backed cache of text → vector."""

    def __init__(self, path: Path | None = None):
        self._lock = threading.RLock()
        self._mem: dict[str, "np.ndarray"] = {}
        self._dirty = False
        self._loaded = False
        self._path = path or (config.data_dir() / 'embed_cache.npz')

    # ── load / persist ───────────────────────────────────────────────
    def _ensure_loaded(self):
        if self._loaded or np is None:
            self._loaded = True
            return
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                if self._path.exists():
                    data = np.load(self._path, allow_pickle=False)
                    keys = data['keys']
                    vecs = data['vecs']
                    for i, k in enumerate(keys):
                        self._mem[str(k)] = vecs[i]
                    log.info('[RAG/CACHE] loaded %d cached embeddings from %s',
                             len(self._mem), self._path)
            except Exception as e:
                log.warning('[RAG/CACHE] load failed (%s) — starting empty', e)
                self._mem = {}

    def flush(self):
        """Atomically persist the in-memory cache. Cheap no-op when clean
        or when numpy is unavailable."""
        if np is None:
            return
        with self._lock:
            if not self._dirty:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                items = list(self._mem.items())
                if len(items) > _MAX_ENTRIES:
                    items = items[-_MAX_ENTRIES:]
                    self._mem = dict(items)
                keys = np.array([k for k, _ in items])
                vecs = (np.vstack([v for _, v in items])
                        if items else np.zeros((0, config.EMBED_DIM), dtype='float32'))
                tmp = self._path.with_suffix('.npz.tmp')
                np.savez(tmp, keys=keys, vecs=vecs.astype('float32'))
                os.replace(tmp, self._path)
                self._dirty = False
                log.info('[RAG/CACHE] persisted %d embeddings → %s',
                         len(items), self._path)
            except Exception as e:
                log.warning('[RAG/CACHE] flush failed (%s)', e)

    # ── get / put ────────────────────────────────────────────────────
    def get(self, model: str, text: str):
        if np is None:
            return None
        self._ensure_loaded()
        with self._lock:
            return self._mem.get(key_for(model, text))

    def put(self, model: str, text: str, vector) -> None:
        if np is None:
            return
        self._ensure_loaded()
        with self._lock:
            self._mem[key_for(model, text)] = np.asarray(vector, dtype='float32')
            self._dirty = True

    def get_many(self, model: str, texts: list[str]):
        """Return a list aligned with ``texts``; misses are None."""
        if np is None:
            return [None] * len(texts)
        self._ensure_loaded()
        with self._lock:
            return [self._mem.get(key_for(model, t)) for t in texts]

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._mem)


# Module-level singleton used by the embedder.
CACHE = EmbeddingCache()
