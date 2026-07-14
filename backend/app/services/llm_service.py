"""
Centralized LLM service — AWS Bedrock.

This is the single entry point for every LLM call in the backend. Every
agent (and the `/api/agents/ping` backbone-proof route) goes through
`complete()`.

ADAPTATION NOTE (ai-discovery-canvas): this file originally called Azure
OpenAI (ported from frd-generator). It has been REWRITTEN to call AWS
Bedrock via `boto3`'s `converse` API instead — per explicit request, the
backend now uses AWS Bedrock, not Azure OpenAI. The public `complete()`
signature is unchanged (same params, same return type) so every caller
(`app/routes/agents.py`, and anything built on top of it later) keeps
working without modification — only this file's internals changed.

Configuration is read from environment variables (see `.env.example`).
Secrets MUST NOT be hard-coded in this file or in any committed source.

Design notes
------------
* **Stable signature** — `complete(prompt, image_path=None, timeout=None,
  *, tag=..., system=None, max_output_tokens=None, model=None)`. `model`,
  if given, overrides the Bedrock model id/ARN for that one call;
  otherwise `BEDROCK_MODEL_ID` from the environment is used — the exact
  model is intentionally NOT hard-coded here, the operator sets it via env.

* **Converse API** — uses Bedrock Runtime's `converse()` operation, which
  normalises the request/response shape across model providers
  (Anthropic, Meta, Amazon, Mistral, …) so this file doesn't need to know
  which model family is configured.

* **Rate limiting** — a process-wide sliding-window limiter (`_RateLimiter`)
  blocks each call until both an RPM and a TPM budget allow it through, so
  parallel callers never blow a Bedrock account/model quota. Defaults are
  conservative (`BEDROCK_MAX_RPM`/`BEDROCK_MAX_TPM`) — raise them via env
  once you know your account's real Bedrock quota for the chosen model.

* **Retry-with-backoff** — Bedrock throttling (`ThrottlingException`) and
  transient/server errors get exponential backoff up to `BEDROCK_MAX_RETRIES`
  attempts. Non-retryable errors (`ValidationException`,
  `AccessDeniedException`, `ResourceNotFoundException`, bad request shape)
  surface immediately — retrying them would never succeed.

* **Context-window safety** — head-and-tail truncation via tiktoken
  (`cl100k_base` — a close-enough estimator for budgeting purposes across
  model families; Bedrock doesn't publish a universal tokenizer) if a
  prompt would blow `BEDROCK_MAX_CONTEXT`.

* **Vision** — `image_path` reads the file from disk and attaches it as an
  `image` content block (raw bytes, not a data-URL — Bedrock's Converse
  API takes bytes directly). PNG, JPEG, WEBP, and GIF are supported by
  Bedrock's multimodal models (e.g. Claude on Bedrock); a model that
  doesn't support vision will reject the image block with a
  `ValidationException`, surfaced as a `RuntimeError`.

* **Logging** — every call gets a sequential `call_id`, the prompt tag
  (`[ENTITY-EXTRACT]`, `[AGENTS/PING]`, etc.), input/output sizes, and
  wall-clock duration so pipeline slowness stays traceable.

* **Credentials** — this file does NOT resolve AWS credentials itself; it
  hands region/model config to `boto3`, which resolves credentials via its
  own standard chain (in priority order: explicit env vars
  `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` → shared
  `~/.aws/credentials` profile → an attached IAM role). For local dev, just
  paste your access key / secret key / region into `backend/.env` — see
  `.env.example`.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)

try:
    import tiktoken
    # No Bedrock model publishes an official tokenizer via tiktoken;
    # cl100k_base gives a close-enough estimate for rate-limit/context
    # budgeting purposes regardless of which model is actually configured.
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover — best-effort fallback
    _ENC = None


log = logging.getLogger("app.llm")


# ── Configuration (env-driven) ───────────────────────────────────────
AWS_REGION       = os.environ.get("AWS_REGION", "").strip() or os.environ.get("AWS_DEFAULT_REGION", "us-east-1").strip()
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "").strip()
# Optional cheaper/faster model for tiny classification calls (Copilot's
# intent router, deepresearch's request classifier) — those run on EVERY
# chat message with a ~150-token output budget, so a Haiku-class model
# here cuts per-conversation cost without touching synthesis quality.
# Falls back to BEDROCK_MODEL_ID when unset.
ROUTER_MODEL_ID  = os.environ.get("BEDROCK_ROUTER_MODEL_ID", "").strip()

# Conservative defaults — Bedrock quotas are per-account/per-model and vary
# a lot, so these are deliberately small. Raise via env once you know your
# real on-demand (or provisioned-throughput) quota for the chosen model.
MAX_RPM = int(os.environ.get("BEDROCK_MAX_RPM", "50"))
MAX_TPM = int(os.environ.get("BEDROCK_MAX_TPM", "100000"))

# Context-window budget. 200k is Claude-3-family's published input window;
# override via env if the configured model has a different limit.
MAX_CONTEXT_TOKENS = int(os.environ.get("BEDROCK_MAX_CONTEXT", "200000"))
MAX_OUTPUT_TOKENS  = int(os.environ.get("BEDROCK_MAX_OUTPUT",  "4096"))

# Default per-request timeout (seconds).
DEFAULT_TIMEOUT = int(os.environ.get("BEDROCK_TIMEOUT", "300"))

# Retry budget. Throttling usually clears within seconds; we cap total wall
# time so a stuck quota doesn't pin a request forever.
MAX_RETRIES = int(os.environ.get("BEDROCK_MAX_RETRIES", "5"))

_IMAGE_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp", ".gif": "gif"}


# ── Client (lazy, thread-safe singleton) ─────────────────────────────
_client = None
_client_lock = threading.Lock()


def _get_client():
    """Lazy double-checked-locking singleton. A `boto3` bedrock-runtime
    client is thread-safe, so one shared instance is fine. Credentials are
    resolved by boto3's own default chain (env vars first — see module
    docstring); we never read/store the secret values ourselves."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        errors = check_configured()
        if errors:
            raise RuntimeError("AWS Bedrock is not configured: " + "; ".join(errors))
        boto_cfg = BotoConfig(
            region_name=AWS_REGION,
            connect_timeout=10,
            read_timeout=DEFAULT_TIMEOUT,
            retries={"max_attempts": 0},  # we do our own retry/backoff below
        )
        _client = boto3.client("bedrock-runtime", config=boto_cfg)
        log.info("[LLM] Bedrock client initialised — region=%s model=%s",
                 AWS_REGION, BEDROCK_MODEL_ID)
        return _client


# ── Sliding-window rate limiter ──────────────────────────────────────
class _RateLimiter:
    """Process-wide sliding-window limiter on both requests-per-minute
    and tokens-per-minute. Threads call `acquire(est_tokens)` and block
    until both budgets allow the call. Estimates are based on the input
    prompt + reserved output; actual usage from the API response is not
    fed back (the limiter intentionally over-budgets to stay conservative
    rather than racing the quota window)."""

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
                cur_tokens = sum(t for _, t in self._tok_events)
                rpm_ok = len(self._req_times) < self.max_rpm
                tpm_ok = (cur_tokens + tokens) <= self.max_tpm
                if rpm_ok and tpm_ok:
                    self._req_times.append(now)
                    self._tok_events.append((now, tokens))
                    return
                waits = []
                if not rpm_ok:
                    waits.append(60.0 - (now - self._req_times[0]))
                if not tpm_ok and self._tok_events:
                    waits.append(60.0 - (now - self._tok_events[0][0]))
                wait = max(0.2, min(waits) if waits else 0.5)
            time.sleep(min(wait, 5.0))


_limiter = _RateLimiter(MAX_RPM, MAX_TPM)


# ── Token / image helpers ────────────────────────────────────────────
def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is None:
        return max(1, len(text) // 4)
    try:
        return len(_ENC.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _truncate_for_context(prompt: str, max_input_tokens: int) -> tuple[str, int, bool]:
    """Head-and-tail truncate to fit the input budget. Returns
    (text, est_tokens, was_truncated)."""
    tokens = _estimate_tokens(prompt)
    if tokens <= max_input_tokens:
        return prompt, tokens, False
    notice = "\n\n...[truncated to fit context window]...\n\n"
    if _ENC is None:
        keep_chars = max_input_tokens * 4
        half = keep_chars // 2
        return prompt[:half] + notice + prompt[-half:], max_input_tokens, True
    ids = _ENC.encode(prompt)
    half = max_input_tokens // 2
    head = _ENC.decode(ids[:half])
    tail = _ENC.decode(ids[-half:])
    return head + notice + tail, max_input_tokens, True


def _read_image_bytes(image_path: str) -> tuple[bytes, str]:
    """Return (raw_bytes, bedrock_format) for a local image file. Bedrock's
    Converse API wants raw bytes plus an explicit format string — no
    base64/data-URL wrapping (that was an Azure OpenAI-specific need)."""
    p = Path(image_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Image not found: {p}")
    ext = p.suffix.lower()
    fmt = _IMAGE_FORMATS.get(ext)
    if not fmt:
        guessed = (mimetypes.guess_type(p.name)[0] or "").split("/")[-1]
        fmt = _IMAGE_FORMATS.get(f".{guessed}", "png")
    return p.read_bytes(), fmt


# ── Public API ───────────────────────────────────────────────────────
_CALL_COUNT = 0
_CALL_LOCK = threading.Lock()


def complete(
    prompt: str,
    image_path: Optional[str] = None,
    timeout: Optional[int] = None,
    *,
    tag: str = "[LLM]",
    system: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
    model: Optional[str] = None,
    cache_system: bool = False,
) -> str:
    """Send a single user prompt to AWS Bedrock and return the response
    text.

    ``cache_system=True`` appends a Bedrock prompt-caching checkpoint
    (``cachePoint``) after the system block — for callers whose system
    prompt is identical on every call (the chat/router prompts), repeated
    calls within the cache TTL are billed at the cached-read rate instead
    of full price. Honest caveat: Bedrock only creates a checkpoint once
    the prefix exceeds the model's minimum cacheable size (1024 tokens
    for Sonnet-class models); below that it's a silent no-op, and on
    models that reject the field outright we strip it and retry once
    rather than failing the call.

    Parameters
    ----------
    prompt : str
        The user prompt. If it exceeds the configured context window, it
        is head-and-tail truncated with a visible notice in the middle.
    image_path : str, optional
        Absolute path to an image to attach (vision). PNG/JPEG/WEBP/GIF —
        only meaningful if the configured model supports multimodal input.
    timeout : int, optional
        Per-request read timeout in seconds. NOTE: boto3 sets this at
        client-construction time, not per-call — the shared client uses
        BEDROCK_TIMEOUT; passing a different value here is accepted for
        signature compatibility but only takes effect on the very first
        call (which builds the shared client). Change BEDROCK_TIMEOUT in
        `.env` if you need a different default.
    tag : str
        Short label used in log lines so an operator can trace which
        pipeline stage the call belongs to.
    system : str, optional
        Optional system message.
    max_output_tokens : int, optional
        Override the default output budget for this call.
    model : str, optional
        Override which Bedrock model id/ARN to invoke for this call.
        Defaults to `BEDROCK_MODEL_ID` from the environment — the model is
        intentionally not hard-coded in this file.

    Returns
    -------
    str
        The assistant's reply, stripped of trailing whitespace. Empty
        string is possible if the model returned no text content.
    """
    global _CALL_COUNT
    with _CALL_LOCK:
        _CALL_COUNT += 1
        call_id = _CALL_COUNT

    req_timeout = timeout or DEFAULT_TIMEOUT
    max_out = max_output_tokens or MAX_OUTPUT_TOKENS
    max_in = max(1024, MAX_CONTEXT_TOKENS - max_out)
    model_id = (model or BEDROCK_MODEL_ID or "").strip()

    prompt_text, prompt_tokens, was_truncated = _truncate_for_context(prompt or "", max_in)
    if was_truncated:
        log.warning("%s call #%d prompt truncated to ~%d tokens (original exceeded the input budget)",
                    tag, call_id, max_in)

    content_blocks: list[dict] = []
    if image_path:
        try:
            data, fmt = _read_image_bytes(image_path)
            content_blocks.append({"image": {"format": fmt, "source": {"bytes": data}}})
        except Exception as e:
            log.error("%s call #%d image attach failed (%s) — sending text only",
                      tag, call_id, e)
    content_blocks.append({"text": prompt_text})

    messages = [{"role": "user", "content": content_blocks}]

    log.info("%s call #%d → %d input chars (~%d tokens), timeout=%ds%s model=%s",
              tag, call_id, len(prompt_text), prompt_tokens, req_timeout,
              f", image={Path(image_path).name}" if image_path else "", model_id or "<unset>")

    if not model_id:
        raise RuntimeError(
            "BEDROCK_MODEL_ID is not set — pick a Bedrock model id/ARN "
            "(e.g. an Anthropic/Meta/Amazon model on Bedrock) and set it "
            "in backend/.env, or pass model= explicitly."
        )

    client = _get_client()
    _limiter.acquire(prompt_tokens + max_out)

    kwargs: dict = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_out},
    }
    if system:
        kwargs["system"] = [{"text": system}]
        if cache_system:
            kwargs["system"].append({"cachePoint": {"type": "default"}})

    t0 = time.time()
    last_exc: Optional[Exception] = None
    backoff = 1.0

    _RETRYABLE_CODES = {
        "ThrottlingException", "ServiceUnavailableException",
        "InternalServerException", "ModelTimeoutException",
        "ModelNotReadyException",
    }
    _NON_RETRYABLE_CODES = {
        "ValidationException", "AccessDeniedException",
        "ResourceNotFoundException", "ModelErrorException",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.converse(**kwargs)
            dur = time.time() - t0
            blocks = resp.get("output", {}).get("message", {}).get("content", [])
            content = "".join(b.get("text", "") for b in blocks if "text" in b).strip()
            usage = resp.get("usage") or {}
            log.info("%s call #%d done in %.1fs → %d chars out (in_tok=%s out_tok=%s)",
                      tag, call_id, dur, len(content),
                      usage.get("inputTokens", "?"), usage.get("outputTokens", "?"))
            return content

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ValidationException" and cache_system and \
                    any("cachePoint" in b for b in kwargs.get("system", [])):
                # Model/region doesn't accept prompt-cache checkpoints —
                # strip and retry once at full price instead of failing.
                log.info("%s call #%d cachePoint rejected — retrying without prompt caching", tag, call_id)
                kwargs["system"] = [{"text": system}]
                cache_system = False
                continue
            if code in _NON_RETRYABLE_CODES:
                log.error("%s call #%d non-retryable Bedrock error %s: %s", tag, call_id, code, e)
                raise RuntimeError(f"AWS Bedrock error ({code}): {e}") from e
            last_exc = e
            retry_after = _retry_after_seconds(e) or backoff
            log.warning("%s call #%d Bedrock error %s (attempt %d/%d) — retrying in %.1fs",
                        tag, call_id, code or "unknown", attempt, MAX_RETRIES, retry_after)
            time.sleep(retry_after)
            backoff = min(backoff * 2, 30.0)

        except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError) as e:
            last_exc = e
            log.warning("%s call #%d network error (attempt %d/%d): %s — retrying in %.1fs",
                        tag, call_id, attempt, MAX_RETRIES, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

        except NoCredentialsError as e:
            log.error("%s call #%d no AWS credentials found: %s", tag, call_id, e)
            raise RuntimeError(
                "No AWS credentials found — set AWS_ACCESS_KEY_ID / "
                "AWS_SECRET_ACCESS_KEY (and AWS_SESSION_TOKEN if using "
                "temporary credentials) in backend/.env"
            ) from e

        except BotoCoreError as e:
            last_exc = e
            log.warning("%s call #%d boto core error (attempt %d/%d): %s — retrying in %.1fs",
                        tag, call_id, attempt, MAX_RETRIES, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    log.error("%s call #%d gave up after %d retries: %s", tag, call_id, MAX_RETRIES, last_exc)
    raise RuntimeError(f"AWS Bedrock request failed after {MAX_RETRIES} retries: {last_exc}")


def _retry_after_seconds(exc: ClientError) -> Optional[float]:
    """Pull a Retry-After-style hint off a throttling response, if present."""
    try:
        headers = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}) or {}
        ra = headers.get("retry-after") or headers.get("Retry-After")
        return float(ra) if ra else None
    except Exception:
        return None


def complete_stream(
    prompt: str,
    *,
    tag: str = "[LLM/STREAM]",
    system: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
    model: Optional[str] = None,
    cache_system: bool = False,
):
    """Streaming variant of ``complete`` — yields text deltas as the model
    produces them (Bedrock ``converse_stream``). Same truncation, rate
    limiting, and prompt-caching behaviour; deliberately NO mid-stream
    retry (a throttle before the first token raises normally so the
    caller can fall back to ``complete``, but once tokens have been sent
    to a client there is nothing sane to retry into — the generator just
    raises and the caller ends the stream with an error frame)."""
    global _CALL_COUNT
    with _CALL_LOCK:
        _CALL_COUNT += 1
        call_id = _CALL_COUNT

    max_out = max_output_tokens or MAX_OUTPUT_TOKENS
    max_in = max(1024, MAX_CONTEXT_TOKENS - max_out)
    model_id = (model or BEDROCK_MODEL_ID or "").strip()
    if not model_id:
        raise RuntimeError("BEDROCK_MODEL_ID is not set")

    prompt_text, prompt_tokens, was_truncated = _truncate_for_context(prompt or "", max_in)
    if was_truncated:
        log.warning("%s call #%d prompt truncated to ~%d tokens", tag, call_id, max_in)

    client = _get_client()
    _limiter.acquire(prompt_tokens + max_out)

    kwargs: dict = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": prompt_text}]}],
        "inferenceConfig": {"maxTokens": max_out},
    }
    if system:
        kwargs["system"] = [{"text": system}]
        if cache_system:
            kwargs["system"].append({"cachePoint": {"type": "default"}})

    log.info("%s call #%d → %d input chars (~%d tokens) model=%s (streaming)",
             tag, call_id, len(prompt_text), prompt_tokens, model_id)
    t0 = time.time()
    try:
        resp = client.converse_stream(**kwargs)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ValidationException" and cache_system:
            kwargs["system"] = [{"text": system}]
            resp = client.converse_stream(**kwargs)
        else:
            raise
    n_chars = 0
    for event in resp.get("stream", []):
        delta = event.get("contentBlockDelta", {}).get("delta", {}).get("text")
        if delta:
            n_chars += len(delta)
            yield delta
    log.info("%s call #%d stream done in %.1fs → %d chars out",
             tag, call_id, time.time() - t0, n_chars)


def check_configured() -> list[str]:
    """Return a list of human-readable configuration errors. Empty list
    means the service is ready to serve requests."""
    errors: list[str] = []
    if not AWS_REGION:
        errors.append("AWS_REGION is not set")
    if not BEDROCK_MODEL_ID:
        errors.append("BEDROCK_MODEL_ID is not set (pick a model id/ARN in AWS Bedrock)")
    try:
        creds = boto3.Session().get_credentials()
        if creds is None:
            errors.append(
                "No AWS credentials found (checked AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY "
                "env vars, ~/.aws/credentials, and IAM role)"
            )
    except Exception as e:  # pragma: no cover — defensive, boto3 session init rarely fails
        errors.append(f"Could not resolve AWS credentials: {e}")
    return errors
