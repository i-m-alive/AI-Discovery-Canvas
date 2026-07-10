"""
Centralized LLM service — Azure OpenAI GPT-4.1.

This is the single entry point for every LLM call in the backend. All
features (knowledge graph extraction, agent execution, workflow
orchestration, NaviCORE Assistant chat, capability map generation,
document structuring, video frame analysis, …) go through `complete()`.

Configuration is read from environment variables (see `.env.example`).
Secrets MUST NOT be hard-coded in this file or in any committed source.

Design notes
------------
* **Stable signature** — `complete(prompt, image_path=None, timeout=None,
  *, tag=..., system=None, max_output_tokens=None)`. The 40+ callers in
  `legacy_routes.py` all funnel through the module-level `run_llm`
  helper which delegates here, so a future provider swap only needs to
  touch this file.

* **Rate limiting** — Azure deployment quotas for gpt-4.1 are 250
  requests / 250k tokens per minute. A process-wide sliding-window
  limiter (`_RateLimiter`) blocks each call until both the RPM and TPM
  budgets allow it through, so parallel callers (ThreadPoolExecutor in
  video frame analysis, concurrent SSE pipelines, etc.) never blow the
  quota.

* **Retry-with-backoff** — `RateLimitError` honours `Retry-After`;
  transient connect/timeout errors and 5xx get exponential backoff up
  to 5 attempts. Non-retryable errors (`BadRequestError`, 4xx other than
  429) surface immediately.

* **Context-window safety** — gpt-4.1 advertises ~1M input tokens but
  practical deployments are often configured lower. If a prompt would
  blow the budget we head-and-tail truncate via tiktoken and log a
  warning rather than failing the request.

* **Vision** — `image_path` reads the file from disk, base64-encodes it,
  and embeds it as an `image_url` content part alongside the text. PNG,
  JPEG, WEBP, and GIF are supported by GPT-4.1.

* **Logging** — every call gets a sequential `call_id`, the prompt tag
  (`[ENTITY-EXTRACT]`, `[CHAT/CAPEX]`, etc.), input / output sizes, and
  wall-clock duration so the existing operator playbook for tracing
  pipeline slowness still works.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from openai import (
    AzureOpenAI,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    RateLimitError,
)

try:
    import tiktoken
    # gpt-4.1 isn't in the tiktoken model registry yet; cl100k_base is the
    # encoder used by the rest of the GPT-4 family and gives a close enough
    # token estimate for budgeting purposes.
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover — best-effort fallback
    _ENC = None


log = logging.getLogger("app.llm")


# ── Configuration (env-driven) ───────────────────────────────────────
# Non-secret values are read at import time. The API KEY is fetched
# lazily on first client init via the centralised secret manager so it
# always reflects the freshest Key Vault state and never lands in a
# module-level constant that could leak via repr.
AZURE_OPENAI_ENDPOINT    = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview").strip()
AZURE_OPENAI_DEPLOYMENT  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1").strip()


def _get_api_key() -> str:
    """Resolve AZURE_OPENAI_API_KEY via Azure Key Vault, falling back to
    the env var for local dev. The value is cached inside the secret
    manager, so subsequent calls are free."""
    from app.services.secret_manager import get_secret
    return (get_secret(
        'AZURE_OPENAI_API_KEY',
        env_fallback='AZURE_OPENAI_API_KEY',
    ) or '').strip()

# Deployment quotas. Defaults match the gpt-4.1 quota the user
# provisioned (250 RPM / 250k TPM); override via env if Azure resizes
# the deployment.
MAX_RPM       = int(os.environ.get("AZURE_OPENAI_MAX_RPM", "250"))
MAX_TPM       = int(os.environ.get("AZURE_OPENAI_MAX_TPM", "250000"))

# Context-window budget. gpt-4.1's published max is ~1M tokens; keep a
# generous headroom for the response so we don't get cut off mid-answer.
MAX_CONTEXT_TOKENS = int(os.environ.get("AZURE_OPENAI_MAX_CONTEXT", "1000000"))
MAX_OUTPUT_TOKENS  = int(os.environ.get("AZURE_OPENAI_MAX_OUTPUT",  "16384"))

# Default per-request timeout (seconds). Long-form structuring prompts
# (FRD / Technical / SOP document generation) can take several minutes
# end-to-end, so the default is generous.
DEFAULT_TIMEOUT = int(os.environ.get("AZURE_OPENAI_TIMEOUT", "300"))

# Retry budget. 429s usually clear within a minute; we cap total wall
# time so a stuck quota doesn't pin a request forever.
MAX_RETRIES = int(os.environ.get("AZURE_OPENAI_MAX_RETRIES", "5"))


# ── GPT-5.1 configuration ────────────────────────────────────────────
# Second Azure OpenAI deployment on a separate resource. The API key is
# fetched via the secret manager under 'AZURE_OPENAI_GPT51_KEY' /
# 'azure-openapi-gpt5-1-key' in Key Vault. All other config is
# env-overridable; defaults point at the navicoreinst resource.
AZURE_OPENAI_GPT51_ENDPOINT    = os.environ.get("AZURE_OPENAI_GPT51_ENDPOINT",    "https://navicoreinst.openai.azure.com/").strip()
AZURE_OPENAI_GPT51_API_VERSION = os.environ.get("AZURE_OPENAI_GPT51_API_VERSION", "2024-12-01-preview").strip()
GPT51_DEPLOYMENT               = os.environ.get("AZURE_OPENAI_GPT51_DEPLOYMENT",  "gpt-5.1").strip()
GPT51_TARGET_URI               = "https://navicoreinst.openai.azure.com/openai/responses?api-version=2025-04-01-preview"

MAX_RPM_GPT51 = int(os.environ.get("AZURE_OPENAI_GPT51_MAX_RPM", str(MAX_RPM)))
MAX_TPM_GPT51 = int(os.environ.get("AZURE_OPENAI_GPT51_MAX_TPM", str(MAX_TPM)))


# ── Client (lazy, thread-safe singleton) ─────────────────────────────
_client: Optional[AzureOpenAI] = None
_client_lock = threading.Lock()


def _get_client() -> AzureOpenAI:
    """Lazy double-checked-locking singleton. The AzureOpenAI client is
    thread-safe per the SDK docs, so one shared instance is fine."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        errors = check_configured()
        if errors:
            raise RuntimeError(
                "Azure OpenAI is not configured: " + "; ".join(errors)
            )
        _client = AzureOpenAI(
            api_version=AZURE_OPENAI_API_VERSION,
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=_get_api_key(),
        )
        log.info("[LLM] AzureOpenAI client initialised — endpoint=%s deployment=%s api_version=%s",
                 AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION)
        return _client


# ── GPT-5.1 client (lazy, thread-safe singleton) ─────────────────────
def _get_gpt51_api_key() -> str:
    """Resolve AZURE_OPENAI_GPT51_KEY via Azure Key Vault, falling back to
    the env var. Cached inside the secret manager after first fetch."""
    from app.services.secret_manager import get_secret
    return (get_secret(
        'AZURE_OPENAI_GPT51_KEY',
        env_fallback='AZURE_OPENAI_GPT51_KEY',
    ) or '').strip()


_gpt51_client: Optional[AzureOpenAI] = None
_gpt51_client_lock = threading.Lock()


def _get_gpt51_client() -> AzureOpenAI:
    """Lazy double-checked-locking singleton for GPT-5.1."""
    global _gpt51_client
    if _gpt51_client is not None:
        return _gpt51_client
    with _gpt51_client_lock:
        if _gpt51_client is not None:
            return _gpt51_client
        api_key = _get_gpt51_api_key()
        if not AZURE_OPENAI_GPT51_ENDPOINT or not api_key:
            raise RuntimeError(
                "GPT-5.1 is not configured: AZURE_OPENAI_GPT51_ENDPOINT or "
                "AZURE_OPENAI_GPT51_KEY is missing (checked Key Vault + env)"
            )
        _gpt51_client = AzureOpenAI(
            api_version=AZURE_OPENAI_GPT51_API_VERSION,
            azure_endpoint=AZURE_OPENAI_GPT51_ENDPOINT,
            api_key=api_key,
        )
        log.info("[LLM] AzureOpenAI GPT-5.1 client initialised — endpoint=%s deployment=%s api_version=%s",
                 AZURE_OPENAI_GPT51_ENDPOINT, GPT51_DEPLOYMENT, AZURE_OPENAI_GPT51_API_VERSION)
        return _gpt51_client


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
        # Cap one-shot reservations to the per-minute budget so a single
        # giant prompt isn't unsatisfiable.
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
                # Compute how long to wait until at least one budget frees up.
                waits = []
                if not rpm_ok:
                    waits.append(60.0 - (now - self._req_times[0]))
                if not tpm_ok and self._tok_events:
                    waits.append(60.0 - (now - self._tok_events[0][0]))
                wait = max(0.2, min(waits) if waits else 0.5)
            time.sleep(min(wait, 5.0))


_limiter       = _RateLimiter(MAX_RPM,       MAX_TPM)
_gpt51_limiter = _RateLimiter(MAX_RPM_GPT51, MAX_TPM_GPT51)


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


def _encode_image_data_url(image_path: str) -> str:
    p = Path(image_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Image not found: {p}")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


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
    model: str = "gpt-4.1",
) -> str:
    """Send a single user prompt to Azure OpenAI GPT-4.1 and return the
    response text.

    Used everywhere via the module-level `run_llm` shim in
    `legacy_routes.py`. The positional argument order is stable so
    callers can pass `(prompt, image_path, timeout)` without keyword
    plumbing.

    Parameters
    ----------
    prompt : str
        The user prompt. If it exceeds the configured context window,
        it is head-and-tail truncated with a visible notice in the middle.
    image_path : str, optional
        Absolute path to an image to attach (vision). PNG/JPEG/WEBP/GIF.
    timeout : int, optional
        Per-request timeout in seconds. Defaults to AZURE_OPENAI_TIMEOUT.
    tag : str
        Short label used in log lines so an operator can trace which
        pipeline stage the call belongs to. Examples: `[ENTITY-EXTRACT]`,
        `[CHAT/CAPEX]`, `[STRUCTURING]`.
    system : str, optional
        Optional system message. Most legacy prompts already include
        their role/instruction header inline; this is for new code.
    max_output_tokens : int, optional
        Override the default output budget for this call.

    Returns
    -------
    str
        The assistant's reply, stripped of trailing whitespace. Empty
        string is possible if the model returned no content.
    """
    global _CALL_COUNT
    with _CALL_LOCK:
        _CALL_COUNT += 1
        call_id = _CALL_COUNT

    req_timeout = timeout or DEFAULT_TIMEOUT
    max_out = max_output_tokens or MAX_OUTPUT_TOKENS
    max_in = max(1024, MAX_CONTEXT_TOKENS - max_out)

    prompt_text, prompt_tokens, was_truncated = _truncate_for_context(prompt or "", max_in)
    if was_truncated:
        log.warning("%s call #%d prompt truncated to ~%d tokens (original exceeded the input budget)",
                    tag, call_id, max_in)

    # Build the chat message payload. For text-only calls we send the
    # prompt as a plain string (lighter wire format); only when an
    # image is attached do we switch to the multi-part content array.
    if image_path:
        try:
            data_url = _encode_image_data_url(image_path)
            user_content = [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt_text},
            ]
        except Exception as e:
            log.error("%s call #%d image attach failed (%s) — sending text only",
                      tag, call_id, e)
            user_content = prompt_text
    else:
        user_content = prompt_text

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_content})

    log.info("%s call #%d → %d input chars (~%d tokens), timeout=%ds%s",
             tag, call_id, len(prompt_text), prompt_tokens, req_timeout,
             f", image={Path(image_path).name}" if image_path else "")

    # Select client, deployment, and rate limiter based on the requested model.
    if model == "gpt-5.1":
        active_client     = _get_gpt51_client()
        active_deployment = GPT51_DEPLOYMENT
        active_limiter    = _gpt51_limiter
    else:
        active_client     = _get_client()
        active_deployment = AZURE_OPENAI_DEPLOYMENT
        active_limiter    = _limiter

    # Reserve quota for input + reserved output BEFORE the network call
    # so concurrent callers serialise cleanly against TPM/RPM limits.
    active_limiter.acquire(prompt_tokens + max_out)

    t0 = time.time()
    last_exc: Optional[Exception] = None
    backoff = 1.0

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = active_client.chat.completions.create(
                model=active_deployment,
                messages=messages,
                max_tokens=max_out,
                timeout=req_timeout,
            )
            dur = time.time() - t0
            choice = resp.choices[0] if resp.choices else None
            content = (choice.message.content if choice and choice.message else "") or ""
            content = content.strip()
            usage = getattr(resp, "usage", None)
            in_tok  = getattr(usage, "prompt_tokens", None) if usage else None
            out_tok = getattr(usage, "completion_tokens", None) if usage else None
            log.info("%s call #%d done in %.1fs → %d chars out (in_tok=%s out_tok=%s)",
                     tag, call_id, dur, len(content),
                     in_tok if in_tok is not None else "?",
                     out_tok if out_tok is not None else "?")
            return content

        except RateLimitError as e:
            last_exc = e
            retry_after = _retry_after_seconds(e) or backoff
            log.warning("%s call #%d rate-limited (attempt %d/%d) — sleeping %.1fs",
                        tag, call_id, attempt, MAX_RETRIES, retry_after)
            time.sleep(retry_after)
            backoff = min(backoff * 2, 30.0)

        except (APITimeoutError, APIConnectionError) as e:
            last_exc = e
            log.warning("%s call #%d network error (attempt %d/%d): %s — retrying in %.1fs",
                        tag, call_id, attempt, MAX_RETRIES, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

        except BadRequestError as e:
            # 400-class errors (bad payload, content filter, oversized
            # image, etc.) don't get better on retry.
            log.error("%s call #%d bad request (no retry): %s", tag, call_id, e)
            raise RuntimeError(f"Azure OpenAI bad request: {e}") from e

        except APIStatusError as e:
            last_exc = e
            status = getattr(e, "status_code", None)
            if status and 500 <= status < 600:
                log.warning("%s call #%d server error %s (attempt %d/%d) — retrying in %.1fs",
                            tag, call_id, status, attempt, MAX_RETRIES, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            log.error("%s call #%d non-retryable status %s: %s",
                      tag, call_id, status, e)
            raise RuntimeError(f"Azure OpenAI error: {e}") from e

    log.error("%s call #%d gave up after %d retries: %s",
              tag, call_id, MAX_RETRIES, last_exc)
    raise RuntimeError(f"Azure OpenAI request failed after {MAX_RETRIES} retries: {last_exc}")


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Pull `Retry-After` off a 429 response if Azure provided one."""
    try:
        resp = getattr(exc, "response", None)
        if resp is None:
            return None
        headers = getattr(resp, "headers", None) or {}
        ra = headers.get("Retry-After") or headers.get("retry-after")
        return float(ra) if ra else None
    except Exception:
        return None


def check_configured() -> list[str]:
    """Return a list of human-readable configuration errors. Empty list
    means the service is ready to serve requests. The API key is looked
    up via the secret manager (Key Vault first, env fallback) so an
    operator who has only configured one of the two sees the missing
    one named precisely."""
    errors: list[str] = []
    if not AZURE_OPENAI_ENDPOINT:
        errors.append("AZURE_OPENAI_ENDPOINT is not set")
    if not _get_api_key():
        errors.append("AZURE_OPENAI_API_KEY is not available (checked Key Vault + env)")
    if not AZURE_OPENAI_DEPLOYMENT:
        errors.append("AZURE_OPENAI_DEPLOYMENT is not set")
    return errors
