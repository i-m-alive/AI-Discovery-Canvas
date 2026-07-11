"""
Embedding generation — AWS Bedrock.

Single entry point for turning text into vectors. Mirrors the design of
``app/services/llm_service.py``.

ADAPTATION NOTE (ai-discovery-canvas): originally Azure OpenAI
``text-embedding-3-large``; REWRITTEN to call AWS Bedrock's
``invoke_model`` API instead — no Azure OpenAI dependency remains
anywhere in this project. AWS credentials are resolved by boto3's own
default chain (``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY``/
``AWS_SESSION_TOKEN``/``AWS_REGION`` env vars, or ``~/.aws/credentials``,
or an attached IAM role) — never a Key-Vault-fetched secret, never a
module constant, same pattern as ``llm_service.py``.

Supports two Bedrock embedding model families today, picked by model-id
prefix (see ``_build_request`` / ``_parse_response``):
  * Amazon Titan Embed Text v1 & v2 — model id starts with
    ``amazon.titan-embed``
  * Cohere Embed English/Multilingual v3 — model id starts with
    ``cohere.embed``
Configure ``BEDROCK_EMBEDDING_MODEL_ID`` in ``.env`` to pick one; add
another provider's request/response shape to the two functions above if
you need a different embedding model family.

Design otherwise unchanged from the Azure version:
* **Rate limiting** — a process-wide sliding-window limiter on both RPM
  and TPM (see ``config.py``). Every call reserves its estimated tokens
  before the network call, so parallel workers never blow the quota.
* **Parallelism** — Bedrock embeds one text per ``invoke_model`` call (no
  multi-input batch endpoint), so ``embed_texts`` dispatches individual
  texts across a bounded thread pool instead of grouping them into
  request batches. Order is preserved in the returned matrix.
* **Caching** — each input is hashed; cache hits skip the network call
  entirely (see ``cache.py``).
* **Retry-with-backoff** — Bedrock throttling gets exponential backoff;
  transient network/5xx errors retry; validation/access errors surface
  immediately.

Vectors are returned **L2-normalised** so the FAISS inner-product index
is exact cosine similarity.
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

from app.services.rag import config
from app.services.rag.cache import CACHE
from app.services.rag.chunking import count_tokens, _truncate_tokens

try:
    import numpy as np
except Exception:                                   # pragma: no cover
    np = None

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    _BOTO_OK = True
except Exception:                                   # pragma: no cover
    _BOTO_OK = False


log = logging.getLogger('app.rag.embedder')

AWS_REGION = (os.environ.get('AWS_REGION', '').strip()
              or os.environ.get('AWS_DEFAULT_REGION', 'us-east-1').strip())


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


def _config_errors() -> list[str]:
    errors: list[str] = []
    if not config.EMBED_MODEL:
        errors.append('BEDROCK_EMBEDDING_MODEL_ID is not set')
    if not AWS_REGION:
        errors.append('AWS_REGION is not set')
    if not _BOTO_OK:
        errors.append('boto3 is not installed')
        return errors
    try:
        if boto3.Session().get_credentials() is None:
            errors.append('No AWS credentials found (env vars / ~/.aws/credentials / IAM role)')
    except Exception as e:  # pragma: no cover — defensive
        errors.append(f'Could not resolve AWS credentials: {e}')
    return errors


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        errors = _config_errors()
        if errors:
            raise RuntimeError('AWS Bedrock embeddings not configured: ' + '; '.join(errors))
        boto_cfg = BotoConfig(
            region_name=AWS_REGION,
            connect_timeout=10,
            read_timeout=60,
            retries={'max_attempts': 0},  # we do our own retry/backoff below
        )
        _client = boto3.client('bedrock-runtime', config=boto_cfg)
        log.info('[RAG/EMBED] Bedrock client ready — region=%s model=%s dim=%d',
                 AWS_REGION, config.EMBED_MODEL, config.EMBED_DIM)
        return _client


_avail_cache: Optional[bool] = None
_avail_lock = threading.Lock()


def is_available() -> bool:
    """Cheap readiness probe — does NOT raise and does NOT build the
    client. True means numpy + boto3 + a model id + resolvable AWS
    credentials are all present, so retrieval/indexing can run.

    Memoised for the process (checking credentials on every chat request
    would be wasteful). Call ``reset_availability()`` after fixing
    credentials/config without a restart."""
    global _avail_cache
    if _avail_cache is not None:
        return _avail_cache
    with _avail_lock:
        if _avail_cache is not None:
            return _avail_cache
        ok = bool(np is not None and _BOTO_OK and config.is_configured() and not _config_errors())
        _avail_cache = ok
        return ok


def reset_availability() -> None:
    """Drop the memoised availability probe (e.g. after fixing AWS
    credentials) so the next call re-checks."""
    global _avail_cache
    with _avail_lock:
        _avail_cache = None


# ── Request/response shape per Bedrock embedding model family ────────
def _build_request(text: str) -> bytes:
    model = config.EMBED_MODEL
    if model.startswith('amazon.titan-embed-text-v2'):
        body = {'inputText': text, 'dimensions': config.EMBED_DIM, 'normalize': True}
    elif model.startswith('amazon.titan-embed'):
        # Titan Embed Text v1 — fixed 1536-dim, no dimensions/normalize field.
        body = {'inputText': text}
    elif model.startswith('cohere.embed'):
        body = {'texts': [text], 'input_type': 'search_document'}
    else:
        # Unrecognized family — default to the Titan shape (most common)
        # and let a ValidationException from Bedrock surface precisely if
        # the configured model expects something else. Add a branch above
        # for any other provider you configure.
        body = {'inputText': text}
    return json.dumps(body).encode('utf-8')


def _parse_response(raw: bytes) -> list[float]:
    data = json.loads(raw)
    if 'embedding' in data:            # Titan
        return data['embedding']
    if 'embeddings' in data:           # Cohere -> list of lists, one per input
        embs = data['embeddings']
        return embs[0] if embs else []
    raise RuntimeError(f'Unrecognized Bedrock embedding response shape: keys={list(data.keys())}')


# ── Core embedding call ──────────────────────────────────────────────
def _embed_one(text: str, *, tag: str) -> "np.ndarray":
    """Embed a single text via one Bedrock invoke_model call. Reserves
    quota, retries, returns a (dim,) float32 vector (NOT yet normalised)."""
    _limiter.acquire(count_tokens(text) or 1)
    client = _get_client()
    body = _build_request(text)
    backoff = 1.0
    last_exc: Optional[Exception] = None
    for attempt in range(1, config.EMBED_MAX_RETRIES + 1):
        try:
            resp = client.invoke_model(
                modelId=config.EMBED_MODEL,
                body=body,
                contentType='application/json',
                accept='application/json',
            )
            raw = resp['body'].read()
            vec = _parse_response(raw)
            return np.asarray(vec, dtype='float32')
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            if code in ('ValidationException', 'AccessDeniedException', 'ResourceNotFoundException'):
                log.error('%s embed bad request (no retry): %s', tag, e)
                raise RuntimeError(f'Bedrock embedding error ({code}): {e}') from e
            last_exc = e
            log.warning('%s embed error %s (attempt %d/%d) — retry %.1fs',
                        tag, code or 'unknown', attempt, config.EMBED_MAX_RETRIES, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        except NoCredentialsError as e:
            raise RuntimeError('No AWS credentials found for Bedrock embeddings') from e
        except BotoCoreError as e:
            last_exc = e
            log.warning('%s embed network error (attempt %d/%d): %s — retry %.1fs',
                        tag, attempt, config.EMBED_MAX_RETRIES, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    raise RuntimeError(f'embedding failed after {config.EMBED_MAX_RETRIES} retries: {last_exc}')


def _normalize(mat: "np.ndarray") -> "np.ndarray":
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype('float32')


def embed_texts(texts: list[str], *, tag: str = '[RAG/EMBED]',
                use_cache: bool = True) -> "np.ndarray":
    """Embed a list of texts → an (n, dim) L2-normalised float32 matrix.

    Cache-aware (skips already-embedded inputs) and dispatched across a
    bounded thread pool (Bedrock embeds one text per call, so parallelism
    happens across individual texts rather than request batches — see
    module docstring). Order matches ``texts``.
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

    # 2. Embed misses, parallel across individual texts.
    if misses:
        log.info('%s embedding %d text(s): %d cached, %d to fetch',
                 tag, n, n - len(misses), len(misses))

        def _run(i: int):
            return i, _embed_one(clean[i], tag=tag)

        workers = max(1, min(config.EMBED_MAX_WORKERS, len(misses)))
        if workers == 1:
            for i in misses:
                _, vec = _run(i)
                out[i] = vec
                if use_cache:
                    CACHE.put(config.EMBED_MODEL, clean[i], vec)
        else:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix='rag-embed') as ex:
                futures = [ex.submit(_run, i) for i in misses]
                for fut in as_completed(futures):
                    i, vec = fut.result()
                    out[i] = vec
                    if use_cache:
                        CACHE.put(config.EMBED_MODEL, clean[i], vec)
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
