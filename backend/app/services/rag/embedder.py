"""
Embedding generation — Azure OpenAI ``text-embedding-3-large``.

Single entry point for turning text into vectors. Mirrors the design of
``app/services/llm_service.py``:

* **Secret hygiene** — the API key is fetched lazily from Azure Key Vault
  (secret ``embedding-api-key``) via the centralised secret manager, with
  an ``EMBEDDING_API_KEY`` env fallback for local dev. It is NEVER a
  module constant.
* **Rate limiting** — a process-wide sliding-window limiter on both RPM
  (1,500) and TPM (250,000). Every batch reserves its estimated tokens
  before the network call, so parallel workers never blow the quota.
* **Batching + parallelism** — inputs are packed into batches and the
  batches are dispatched across a bounded thread pool. Order is preserved
  in the returned matrix.
* **Caching** — each input is hashed; cache hits skip the network
  entirely (see ``cache.py``).
* **Retry-with-backoff** — 429s honour ``Retry-After``; transient
  network/5xx errors get exponential backoff; 4xx surfaces immediately.

Vectors are returned **L2-normalised** so the FAISS inner-product index
is exact cosine similarity.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from app.services.rag import config
from app.services.rag.cache import CACHE
from app.services.rag.chunking import count_tokens, _truncate_tokens

try:
    import numpy as np
except Exception:                                   # pragma: no cover
    np = None

try:
    from openai import (
        AzureOpenAI,
        APIConnectionError,
        APITimeoutError,
        APIStatusError,
        BadRequestError,
        RateLimitError,
    )
    _OPENAI_OK = True
except Exception:                                   # pragma: no cover
    _OPENAI_OK = False


log = logging.getLogger('app.rag.embedder')


# ── Sliding-window RPM/TPM limiter (same shape as llm_service) ────────
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
                if len(self._req_times) >= self.max_rpm and self._req_times:
                    waits.append(60.0 - (now - self._req_times[0]))
                if (cur + tokens) > self.max_tpm and self._tok_events:
                    waits.append(60.0 - (now - self._tok_events[0][0]))
                wait = max(0.2, min(waits) if waits else 0.5)
            time.sleep(min(wait, 5.0))


_limiter = _RateLimiter(config.EMBED_MAX_RPM, config.EMBED_MAX_TPM)


# ── Client (lazy, thread-safe singleton) ─────────────────────────────
_client = None
_client_lock = threading.Lock()


def _get_api_key() -> str:
    from app.services.secret_manager import get_secret
    return (get_secret('EMBEDDING_API_KEY', env_fallback='EMBEDDING_API_KEY') or '').strip()


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        if not _OPENAI_OK:
            raise RuntimeError('openai SDK not installed')
        key = _get_api_key()
        if not key:
            raise RuntimeError("EMBEDDING_API_KEY unavailable (checked Key Vault 'embedding-api-key' + env)")
        if not config.EMBED_ENDPOINT:
            raise RuntimeError('AZURE_EMBEDDING_ENDPOINT not set')
        _client = AzureOpenAI(
            api_version=config.EMBED_API_VERSION,
            azure_endpoint=config.EMBED_ENDPOINT,
            api_key=key,
        )
        log.info('[RAG/EMBED] AzureOpenAI client ready — endpoint=%s deployment=%s api_version=%s dim=%d',
                 config.EMBED_ENDPOINT, config.EMBED_DEPLOYMENT,
                 config.EMBED_API_VERSION, config.EMBED_DIM)
        return _client


_avail_cache: Optional[bool] = None
_avail_lock = threading.Lock()


def is_available() -> bool:
    """Cheap readiness probe — does NOT raise and does NOT build the
    client. True means numpy + openai + endpoint + a resolvable key are
    all present, so retrieval/indexing can run.

    The result is memoised for the process: resolving the key hits Key
    Vault, and this is called on every chat request, so we must not pay
    that round-trip repeatedly (especially the negative case, which the
    secret manager does not cache). Call ``reset_availability()`` after
    provisioning the key without a restart."""
    global _avail_cache
    if _avail_cache is not None:
        return _avail_cache
    with _avail_lock:
        if _avail_cache is not None:
            return _avail_cache
        ok = False
        if np is not None and _OPENAI_OK and config.is_configured():
            try:
                ok = bool(_get_api_key())
            except Exception:
                ok = False
        _avail_cache = ok
        return ok


def reset_availability() -> None:
    """Drop the memoised availability probe (e.g. after the embedding key
    is added to Key Vault) so the next call re-checks."""
    global _avail_cache
    with _avail_lock:
        _avail_cache = None


# ── Core embedding call ──────────────────────────────────────────────
def _embed_batch(texts: list[str], *, tag: str) -> "np.ndarray":
    """Embed one batch (already size-bounded). Reserves quota, retries,
    returns an (n, dim) float32 matrix (NOT yet normalised)."""
    est = sum(count_tokens(t) for t in texts) or 1
    _limiter.acquire(est)
    client = _get_client()
    backoff = 1.0
    last_exc: Optional[Exception] = None
    for attempt in range(1, config.EMBED_MAX_RETRIES + 1):
        try:
            resp = client.embeddings.create(
                model=config.EMBED_DEPLOYMENT,
                input=texts,
            )
            # The SDK returns data sorted by `index`; sort defensively.
            rows = sorted(resp.data, key=lambda d: d.index)
            return np.asarray([r.embedding for r in rows], dtype='float32')
        except RateLimitError as e:
            last_exc = e
            ra = _retry_after_seconds(e) or backoff
            log.warning('%s batch rate-limited (%d/%d) — sleep %.1fs',
                        tag, attempt, config.EMBED_MAX_RETRIES, ra)
            time.sleep(ra)
            backoff = min(backoff * 2, 30.0)
        except (APITimeoutError, APIConnectionError) as e:
            last_exc = e
            log.warning('%s batch net error (%d/%d): %s — retry %.1fs',
                        tag, attempt, config.EMBED_MAX_RETRIES, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        except BadRequestError as e:
            log.error('%s batch bad request (no retry): %s', tag, e)
            raise RuntimeError(f'embedding bad request: {e}') from e
        except APIStatusError as e:
            last_exc = e
            status = getattr(e, 'status_code', None)
            if status and 500 <= status < 600:
                log.warning('%s batch server %s (%d/%d) — retry %.1fs',
                            tag, status, attempt, config.EMBED_MAX_RETRIES, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            raise RuntimeError(f'embedding error: {e}') from e
    raise RuntimeError(f'embedding failed after {config.EMBED_MAX_RETRIES} retries: {last_exc}')


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    try:
        resp = getattr(exc, 'response', None)
        headers = getattr(resp, 'headers', None) or {}
        ra = headers.get('Retry-After') or headers.get('retry-after')
        return float(ra) if ra else None
    except Exception:
        return None


def _normalize(mat: "np.ndarray") -> "np.ndarray":
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype('float32')


def _pack_batches(indices: list[int], texts: list[str]) -> list[list[int]]:
    """Pack input indices into batches bounded by both count and a token
    budget so no single request approaches the per-request ceiling."""
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_tok = 0
    # Keep each request comfortably under the TPM window so one batch
    # can always be admitted by the limiter.
    tok_budget = max(8000, min(config.EMBED_MAX_TPM // 4, 100_000))
    for i in indices:
        t = count_tokens(texts[i]) or 1
        if cur and (len(cur) >= config.EMBED_BATCH_SIZE or cur_tok + t > tok_budget):
            batches.append(cur)
            cur, cur_tok = [], 0
        cur.append(i)
        cur_tok += t
    if cur:
        batches.append(cur)
    return batches


def embed_texts(texts: list[str], *, tag: str = '[RAG/EMBED]',
                use_cache: bool = True) -> "np.ndarray":
    """Embed a list of texts → an (n, dim) L2-normalised float32 matrix.

    Cache-aware (skips already-embedded inputs), batched, and dispatched
    across a bounded thread pool while the limiter holds the call under
    the RPM/TPM quota. Order matches ``texts``.
    """
    if np is None:
        raise RuntimeError('numpy not installed — embeddings unavailable')
    n = len(texts)
    if n == 0:
        return np.zeros((0, config.EMBED_DIM), dtype='float32')

    # Trim each input to the model's token ceiling up front.
    clean = [_truncate_tokens(t or '', config.EMBED_MAX_INPUT_TOKENS) for t in texts]
    out: list[Optional["np.ndarray"]] = [None] * n

    # 1. Cache lookup.
    misses: list[int] = []
    if use_cache:
        cached = CACHE.get_many(config.EMBED_MODEL, clean)
        for i, v in enumerate(cached):
            if v is not None:
                out[i] = v
            else:
                misses.append(i)
    else:
        misses = list(range(n))

    # 2. Embed misses, batched + parallel.
    if misses:
        batches = _pack_batches(misses, clean)
        log.info('%s embedding %d text(s): %d cached, %d to fetch in %d batch(es)',
                 tag, n, n - len(misses), len(misses), len(batches))

        def _run(batch_idx: list[int]):
            mat = _embed_batch([clean[i] for i in batch_idx], tag=tag)
            return batch_idx, mat

        workers = max(1, min(config.EMBED_MAX_WORKERS, len(batches)))
        if workers == 1:
            for b in batches:
                idxs, mat = _run(b)
                for row, i in enumerate(idxs):
                    out[i] = mat[row]
                    if use_cache:
                        CACHE.put(config.EMBED_MODEL, clean[i], mat[row])
        else:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix='rag-embed') as ex:
                futures = [ex.submit(_run, b) for b in batches]
                for fut in as_completed(futures):
                    idxs, mat = fut.result()
                    for row, i in enumerate(idxs):
                        out[i] = mat[row]
                        if use_cache:
                            CACHE.put(config.EMBED_MODEL, clean[i], mat[row])
        if use_cache:
            CACHE.flush()

    mat = np.vstack([
        v if v is not None else np.zeros((config.EMBED_DIM,), dtype='float32')
        for v in out
    ]).astype('float32')
    return _normalize(mat)


def embed_query(text: str, *, tag: str = '[RAG/QUERY]') -> "np.ndarray":
    """Embed a single query string → a (dim,) L2-normalised vector.
    Cached like everything else (repeat questions are free)."""
    mat = embed_texts([text or ''], tag=tag)
    return mat[0]
