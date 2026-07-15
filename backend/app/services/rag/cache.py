"""
Embedding cache.

Embeddings are deterministic for a given (model, text), so we never pay
to embed the same chunk twice — across a single ingest run AND across
process restarts. The key is ``sha256(model | text)``; the value is the
float32 vector.

Persistence is bucketed BY VECTOR DIMENSION, one ``.npz`` (keys + matrix)
per dimension, written atomically. This matters since this project
supports two embedding providers with DIFFERENT native widths (Bedrock
Titan: 1024, Azure text-embedding-3-large: 3072) — a single shared matrix
can't hold both (``np.vstack`` requires uniform width), so mixing them
silently broke persistence entirely (a 400-something-vector flush
failure logged as a warning on every call once a second dimension
appeared). The original bare ``embed_cache.npz`` name is kept for
whichever dimension already had a cache on disk (so existing installs
need no migration); any additional dimension gets its own
``embed_cache__<dim>.npz`` sibling.

The cache is bounded per-dimension: once a bucket exceeds
``RAG_EMBED_CACHE_MAX`` entries, the oldest-loaded rows in THAT bucket
are evicted on the next flush. Everything degrades to an in-memory-only
dict if numpy isn't importable, so a slim image still works (just
without cross-restart reuse).
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
    """Process-wide, thread-safe, disk-backed cache of text → vector.
    Persisted as one .npz file PER VECTOR DIMENSION — see module
    docstring for why a single shared matrix doesn't work here."""

    def __init__(self, path: Path | None = None):
        self._lock = threading.RLock()
        self._mem: dict[str, "np.ndarray"] = {}
        self._dirty = False
        self._loaded = False
        # `path` (if given) is the legacy/first-dimension file name; other
        # dimensions get a sibling `<stem>__<dim><suffix>` next to it.
        self._legacy_path = path or (config.data_dir() / 'embed_cache.npz')
        self._dim_paths: dict[int, Path] = {}   # dim -> the file it loaded from / will save to

    def _path_for_dim(self, dim: int) -> Path:
        """Which file a given dimension reads from / writes to. The first
        dimension ever seen on disk (or the first ever flushed) keeps the
        original bare filename — no migration needed for existing
        single-provider installs; any other dimension gets its own file."""
        known = self._dim_paths.get(dim)
        if known is not None:
            return known
        if not self._dim_paths and not self._legacy_path.exists():
            # Nothing on disk yet at all — this dimension claims the
            # legacy name.
            self._dim_paths[dim] = self._legacy_path
            return self._legacy_path
        p = self._legacy_path.parent / f'{self._legacy_path.stem}__{dim}{self._legacy_path.suffix}'
        self._dim_paths[dim] = p
        return p

    def _all_candidate_paths(self) -> list[Path]:
        """Every cache file that might exist on disk: the legacy bare name
        plus any `__<dim>` siblings already present."""
        paths = [self._legacy_path]
        try:
            paths.extend(self._legacy_path.parent.glob(
                f'{self._legacy_path.stem}__*{self._legacy_path.suffix}'))
        except Exception:
            pass
        return paths

    # ── load / persist ───────────────────────────────────────────────
    def _ensure_loaded(self):
        if self._loaded or np is None:
            self._loaded = True
            return
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            for p in self._all_candidate_paths():
                if not p.exists():
                    continue
                try:
                    data = np.load(p, allow_pickle=False)
                    keys, vecs = data['keys'], data['vecs']
                    dim = int(vecs.shape[1]) if vecs.ndim == 2 and vecs.shape[0] else None
                    if dim is not None:
                        self._dim_paths.setdefault(dim, p)
                    for i, k in enumerate(keys):
                        self._mem[str(k)] = vecs[i]
                    log.info('[RAG/CACHE] loaded %d cached embeddings from %s',
                             len(keys), p)
                except Exception as e:
                    log.warning('[RAG/CACHE] load of %s failed (%s) — skipping', p, e)

    def flush(self):
        """Atomically persist the in-memory cache, one file per vector
        dimension. Cheap no-op when clean or when numpy is unavailable."""
        if np is None:
            return
        with self._lock:
            if not self._dirty:
                return
            try:
                self._legacy_path.parent.mkdir(parents=True, exist_ok=True)
                buckets: dict[int, list[tuple[str, "np.ndarray"]]] = {}
                for k, v in self._mem.items():
                    buckets.setdefault(int(v.shape[0]), []).append((k, v))

                total_written = 0
                for dim, items in buckets.items():
                    if len(items) > _MAX_ENTRIES:
                        items = items[-_MAX_ENTRIES:]   # oldest-evicted, per bucket
                    path = self._path_for_dim(dim)
                    keys = np.array([k for k, _ in items])
                    vecs = np.vstack([v for _, v in items]).astype('float32')
                    tmp = path.with_suffix(path.suffix + '.tmp')
                    # np.savez appends '.npz' to a bare path/string that
                    # doesn't already end with it, so passing `tmp` (ending
                    # in '.npz.tmp') would silently write to a wrongly-
                    # suffixed file — os.replace then can't find `tmp` and
                    # this whole flush becomes a no-op. Passing an open
                    # file object bypasses that auto-suffixing.
                    with open(tmp, 'wb') as fh:
                        np.savez(fh, keys=keys, vecs=vecs)
                    os.replace(tmp, path)
                    total_written += len(items)
                    log.info('[RAG/CACHE] persisted %d embeddings (dim=%d) → %s',
                             len(items), dim, path)

                # Rebuild _mem from what was actually written (applies any
                # per-bucket trimming uniformly).
                self._mem = {k: v for _, items in buckets.items() for k, v in items}
                self._dirty = False
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
