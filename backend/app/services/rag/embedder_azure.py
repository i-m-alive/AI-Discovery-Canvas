"""
Embedding generation — Azure OpenAI.

Mirrors `embedder.py` (the Bedrock implementation) function-for-function
so `service.py` can dispatch between the two transparently based on the
active LLM provider (see `service._active_embedder()`):

    is_available() / config_errors()
    embed_texts(texts, *, tag=..., use_cache=True) -> (n, dim) float32
    embed_query(text, *, tag=...) -> (dim,) float32

Unlike Bedrock's `invoke_model` (one text per call), Azure's embeddings
endpoint accepts a batch of inputs per POST — `embed_texts` groups chunks
into `AZURE_EMBED_BATCH_SIZE`-sized batches and parallelizes across
batches (bounded by `AZURE_EMBED_MAX_WORKERS`), rather than across
individual texts.

Vectors are L2-normalised on return, same as the Bedrock path, so the
FAISS inner-product index is exact cosine similarity regardless of which
provider embedded them.

Configuration (backend/.env):
    AZURE_EMBEDDING_ENDPOINT     https://<resource>.openai.azure.com
    AZURE_EMBEDDING_DEPLOYMENT   e.g. text-embedding-3-large
    AZURE_EMBEDDING_API_VERSION  e.g. 2024-02-01
    AZURE_EMBEDDING_API_KEY      falls back to AZURE_OPENAI_API_KEY when
                                 unset (same Azure OpenAI resource)
    AZURE_EMBEDDING_DIM          native width of the deployment (3072 for
                                 text-embedding-3-large); lower values are
                                 sent as the request's `dimensions` param
    AZURE_EMBED_MAX_RPM / _MAX_TPM / _MAX_RETRIES / _BATCH_SIZE / _MAX_WORKERS
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

from app.services.rag import config
from app.services.rag.cache import CACHE
from app.services.rag.chunking import count_tokens, _truncate_tokens

try:
    import numpy as np
except Exception:                                   # pragma: no cover
    np = None

log = logging.getLogger('app.rag.embedder_azure')

MODEL = config.AZURE_EMBED_MODEL
DIM = config.AZURE_EMBED_DIM

API_KEY = (os.environ.get('AZURE_EMBEDDING_API_KEY', '').strip()
           or os.environ.get('AZURE_OPENAI_API_KEY', '').strip())


def config_errors() -> list[str]:
    errors: list[str] = []
    if not config.AZURE_EMBED_ENDPOINT:
        errors.append('AZURE_EMBEDDING_ENDPOINT is not set')
    if not MODEL:
        errors.append('AZURE_EMBEDDING_DEPLOYMENT is not set')
    if not API_KEY:
        errors.append('AZURE_EMBEDDING_API_KEY (or AZURE_OPENAI_API_KEY) is not set')
    if np is None:
        errors.append('numpy is not installed')
    return errors


_avail_cache: Optional[bool] = None
_avail_lock = threading.Lock()


def is_available() -> bool:
    """Cheap, memoised readiness probe — mirrors embedder.is_available()."""
    global _avail_cache
    if _avail_cache is not None:
        return _avail_cache
    with _avail_lock:
        if _avail_cache is not None:
            return _avail_cache
        _avail_cache = not config_errors()
        return _avail_cache


def reset_availability() -> None:
    global _avail_cache
    with _avail_lock:
        _avail_cache = None


def _url() -> str:
    return (f"{config.AZURE_EMBED_ENDPOINT}/openai/deployments/{MODEL}"
            f"/embeddings?api-version={config.AZURE_EMBED_API_VERSION}")


def _headers() -> dict:
    return {'api-key': API_KEY, 'Content-Type': 'application/json'}


# ── Sliding-window RPM/TPM limiter (same shape as embedder.py's) ─────
class _RateLimiter:
    def __init__(self, max_rpm: int, max_tpm: int) -> None:
        self.max_rpm = max_rpm
        self.max_tpm = max_tpm
        self._lock = threading.Lock()
        self._req_times: deque[float] = deque()
        self._tok_events: deque[tuple[float, int]] = deque()

    def acquire(self, tokens: int) -> None:
        tokens = min(max(1, tokens), self.max_tpm)
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                while self._req_times and self._req_times[0] < cutoff:
                    self._req_times.popleft()
                while self._tok_events and self._tok_events[0][0] < cutoff:
                    self._tok_events.popleft()
                cur = sum(t for _, t in self._tok_events)
                if len(self._req_times) < self.max_rpm and (cur + tokens) <= self.max_tpm:
                    self._req_times.append(now)
                    self._tok_events.append((now, tokens))
                    return
                waits = []
                if self._req_times and len(self._req_times) >= self.max_rpm:
                    waits.append(60.0 - (now - self._req_times[0]))
                if self._tok_events and (cur + tokens) > self.max_tpm:
                    waits.append(60.0 - (now - self._tok_events[0][0]))
                wait = max(0.2, min(waits) if waits else 0.5)
            time.sleep(min(wait, 5.0))


_limiter = _RateLimiter(config.AZURE_EMBED_MAX_RPM, config.AZURE_EMBED_MAX_TPM)


def _retry_after(resp: requests.Response, fallback: float) -> float:
    try:
        ra = resp.headers.get('Retry-After')
        return max(0.5, float(ra)) if ra else fallback
    except Exception:
        return fallback


_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _embed_batch(texts: list[str], *, tag: str) -> list[list[float]]:
    """One POST for up to AZURE_EMBED_BATCH_SIZE texts. Returns vectors in
    the same order as `texts`."""
    est_tokens = sum(count_tokens(t) or 1 for t in texts)
    _limiter.acquire(est_tokens)

    body: dict = {'input': texts}
    if config.AZURE_EMBED_DIM and MODEL.startswith('text-embedding-3'):
        body['dimensions'] = config.AZURE_EMBED_DIM

    backoff = 1.0
    last_err: Optional[str] = None
    for attempt in range(1, config.AZURE_EMBED_MAX_RETRIES + 1):
        try:
            resp = requests.post(_url(), headers=_headers(),
                                 data=json.dumps(body), timeout=60)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = str(e)
            log.warning('%s embed network error (attempt %d/%d): %s — retry %.1fs',
                        tag, attempt, config.AZURE_EMBED_MAX_RETRIES, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue

        if resp.status_code == 200:
            data = resp.json()
            rows = sorted(data.get('data') or [], key=lambda r: r.get('index', 0))
            return [r.get('embedding') or [] for r in rows]

        if resp.status_code in _RETRYABLE_STATUS:
            last_err = f'HTTP {resp.status_code}: {resp.text[:300]}'
            wait = _retry_after(resp, backoff)
            log.warning('%s embed error %d (attempt %d/%d) — retry %.1fs',
                        tag, resp.status_code, attempt, config.AZURE_EMBED_MAX_RETRIES, wait)
            time.sleep(wait)
            backoff = min(backoff * 2, 30.0)
            continue

        log.error('%s non-retryable embedding error %d: %s', tag, resp.status_code, resp.text[:500])
        raise RuntimeError(f'Azure OpenAI embedding error ({resp.status_code}): {resp.text[:500]}')

    raise RuntimeError(f'Azure embedding failed after {config.AZURE_EMBED_MAX_RETRIES} retries: {last_err}')


def _normalize(mat: "np.ndarray") -> "np.ndarray":
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype('float32')


def embed_texts(texts: list[str], *, tag: str = '[RAG/EMBED/AZURE]',
                use_cache: bool = True) -> "np.ndarray":
    """Embed a list of texts → an (n, dim) L2-normalised float32 matrix.
    Cache-aware and batched (see module docstring). Order matches `texts`."""
    errors = config_errors()
    if errors:
        raise RuntimeError('Azure OpenAI embeddings not configured: ' + '; '.join(errors))
    n = len(texts)
    if n == 0:
        return np.zeros((0, DIM), dtype='float32')

    clean = [_truncate_tokens(t or '', config.EMBED_MAX_INPUT_TOKENS) for t in texts]
    out: list[Optional["np.ndarray"]] = [None] * n

    misses: list[int] = []
    if use_cache:
        cached = CACHE.get_many(MODEL, clean)
        for i, v in enumerate(cached):
            if v is not None:
                out[i] = v
            else:
                misses.append(i)
    else:
        misses = list(range(n))

    if misses:
        log.info('%s embedding %d text(s): %d cached, %d to fetch',
                 tag, n, n - len(misses), len(misses))
        batches = [misses[i:i + config.AZURE_EMBED_BATCH_SIZE]
                   for i in range(0, len(misses), config.AZURE_EMBED_BATCH_SIZE)]

        def _run(idxs: list[int]):
            vecs = _embed_batch([clean[i] for i in idxs], tag=tag)
            return idxs, vecs

        workers = max(1, min(config.AZURE_EMBED_MAX_WORKERS, len(batches)))
        results = []
        if workers == 1:
            results = [_run(b) for b in batches]
        else:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix='rag-embed-azure') as ex:
                futures = [ex.submit(_run, b) for b in batches]
                for fut in as_completed(futures):
                    results.append(fut.result())

        for idxs, vecs in results:
            for pos, i in enumerate(idxs):
                vec = np.asarray(vecs[pos] if pos < len(vecs) else [], dtype='float32')
                if vec.size == 0:
                    vec = np.zeros((DIM,), dtype='float32')
                out[i] = vec
                if use_cache:
                    CACHE.put(MODEL, clean[i], vec)
        if use_cache:
            CACHE.flush()

    mat = np.vstack([
        v if v is not None else np.zeros((DIM,), dtype='float32')
        for v in out
    ]).astype('float32')
    return _normalize(mat)


def embed_query(text: str, *, tag: str = '[RAG/QUERY/AZURE]') -> "np.ndarray":
    mat = embed_texts([text or ''], tag=tag)
    return mat[0]
