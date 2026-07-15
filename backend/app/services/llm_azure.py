"""
Azure OpenAI backend for the centralized LLM service.

This module is NOT called directly by agents — `llm_service.complete()`
stays the single entry point for every LLM call in the backend and
dispatches here when the resolved provider is ``azure_openai`` (the
user's header-menu choice, or the ``LLM_PROVIDER`` env default). It
mirrors the Bedrock path's semantics one-for-one so callers can't tell
the difference:

  * same public contract: text in → text out, ``complete_stream`` yields
    deltas, errors surface as RuntimeError;
  * its own process-wide RPM/TPM sliding-window limiter
    (``AZURE_OPENAI_MAX_RPM`` / ``AZURE_OPENAI_MAX_TPM``);
  * retry-with-backoff on 429/5xx honouring ``Retry-After``; 4xx config
    errors surface immediately;
  * head-and-tail context truncation (reuses llm_service's tokenizer
    helpers — cl100k_base is the *correct* tokenizer family here);
  * vision via base64 ``data:`` URL (the Chat Completions image shape).

Configuration (backend/.env):
    AZURE_OPENAI_ENDPOINT      https://<resource>.openai.azure.com
    AZURE_OPENAI_API_KEY       resource key
    AZURE_OPENAI_API_VERSION   e.g. 2024-12-01-preview
    AZURE_OPENAI_DEPLOYMENT    deployment name (e.g. gpt-4.1)
    AZURE_OPENAI_MAX_RPM / _MAX_TPM / _MAX_CONTEXT / _MAX_OUTPUT
    AZURE_OPENAI_TIMEOUT / _MAX_RETRIES

Bedrock-specific ``model=`` overrides (router-model ARNs) do NOT apply
here — every call runs on the configured deployment. If you later add a
cheap router deployment, set ``AZURE_OPENAI_ROUTER_DEPLOYMENT`` and the
dispatcher will use it for router-tagged calls automatically.

NOTE: only chat/completion traffic can run on Azure. Embeddings (RAG
indexing/search) stay on Bedrock Titan — there is no Azure embeddings
deployment configured, and mixing embedding spaces would corrupt the
existing vector index anyway.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("app.llm.azure")

# ── Configuration (env-driven) ───────────────────────────────────────
ENDPOINT    = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
API_KEY     = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview").strip()
DEPLOYMENT  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
# Optional cheaper deployment for tiny router/classifier calls — the
# Azure counterpart of BEDROCK_ROUTER_MODEL_ID. Falls back to DEPLOYMENT.
ROUTER_DEPLOYMENT = os.environ.get("AZURE_OPENAI_ROUTER_DEPLOYMENT", "").strip()

MAX_RPM            = int(os.environ.get("AZURE_OPENAI_MAX_RPM", "60"))
MAX_TPM            = int(os.environ.get("AZURE_OPENAI_MAX_TPM", "100000"))
MAX_CONTEXT_TOKENS = int(os.environ.get("AZURE_OPENAI_MAX_CONTEXT", "128000"))
MAX_OUTPUT_TOKENS  = int(os.environ.get("AZURE_OPENAI_MAX_OUTPUT", "4096"))
DEFAULT_TIMEOUT    = int(os.environ.get("AZURE_OPENAI_TIMEOUT", "300"))
MAX_RETRIES        = int(os.environ.get("AZURE_OPENAI_MAX_RETRIES", "5"))

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

# Some api-versions/model families take `max_tokens`, newer ones want
# `max_completion_tokens` (and 400 on the legacy name). Start with the
# modern name and flip permanently on the first complaint.
_max_tokens_param = "max_completion_tokens"
_param_lock = threading.Lock()


def check_configured() -> list[str]:
    """Human-readable config errors; empty list == ready."""
    errors: list[str] = []
    if not ENDPOINT:
        errors.append("AZURE_OPENAI_ENDPOINT is not set")
    if not API_KEY:
        errors.append("AZURE_OPENAI_API_KEY is not set")
    if not DEPLOYMENT:
        errors.append("AZURE_OPENAI_DEPLOYMENT is not set")
    return errors


def _url(deployment: str) -> str:
    return (f"{ENDPOINT}/openai/deployments/{deployment}"
            f"/chat/completions?api-version={API_VERSION}")


def _headers() -> dict:
    return {"api-key": API_KEY, "Content-Type": "application/json"}


def _image_data_url(image_path: str) -> str:
    p = Path(image_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Image not found: {p}")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def _build_messages(prompt_text: str, system: Optional[str],
                    image_path: Optional[str], tag: str, call_id: int) -> list[dict]:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    if image_path:
        try:
            url = _image_data_url(image_path)
            messages.append({"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": prompt_text},
            ]})
            return messages
        except Exception as e:
            log.error("%s call #%d image attach failed (%s) — sending text only",
                      tag, call_id, e)
    messages.append({"role": "user", "content": prompt_text})
    return messages


def _body(messages: list[dict], max_out: int, stream: bool) -> dict:
    body: dict = {"messages": messages, _max_tokens_param: max_out}
    if stream:
        body["stream"] = True
    return body


def _swap_tokens_param(body: dict, resp_text: str) -> bool:
    """If the API rejected the max-tokens parameter name, flip to the
    other one (module-wide, so we only ever pay this 400 once) and patch
    the given body in place. Returns True when a swap happened."""
    global _max_tokens_param
    lowered = (resp_text or "").lower()
    if "max_tokens" not in lowered and "max_completion_tokens" not in lowered:
        return False
    with _param_lock:
        old = _max_tokens_param
        new = "max_tokens" if old == "max_completion_tokens" else "max_completion_tokens"
        if old not in body:          # someone already swapped
            return False
        _max_tokens_param = new
    body[new] = body.pop(old)
    log.info("[LLM/AZURE] API rejected '%s' — switching to '%s' for all calls", old, new)
    return True


def _retry_after(resp: requests.Response, fallback: float) -> float:
    try:
        ra = resp.headers.get("Retry-After")
        return max(0.5, float(ra)) if ra else fallback
    except Exception:
        return fallback


def _limiter_and_budget(prompt: str, max_output_tokens: Optional[int]):
    """Shared truncation + rate-limit acquisition. Imported lazily from
    llm_service to reuse its tokenizer helpers without a circular import
    at module-load time (llm_service only imports THIS module inside its
    dispatch functions)."""
    from app.services import llm_service as _ls
    max_out = max_output_tokens or MAX_OUTPUT_TOKENS
    max_in = max(1024, MAX_CONTEXT_TOKENS - max_out)
    text, tokens, truncated = _ls._truncate_for_context(prompt or "", max_in)
    _limiter.acquire(tokens + max_out)
    return text, tokens, truncated, max_out


class _RateLimiter:
    """Same sliding-window RPM+TPM limiter as llm_service's — duplicated
    (24 lines) rather than imported at module top to keep this module
    importable standalone and the import graph acyclic."""

    def __init__(self, max_rpm: int, max_tpm: int) -> None:
        self.max_rpm, self.max_tpm = max_rpm, max_tpm
        self._lock = threading.Lock()
        self._req_times: list[float] = []
        self._tok_events: list[tuple[float, int]] = []

    def acquire(self, tokens: int) -> None:
        tokens = min(max(1, tokens), self.max_tpm)
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                self._req_times = [t for t in self._req_times if t >= cutoff]
                self._tok_events = [(t, n) for t, n in self._tok_events if t >= cutoff]
                cur = sum(n for _, n in self._tok_events)
                if len(self._req_times) < self.max_rpm and cur + tokens <= self.max_tpm:
                    self._req_times.append(now)
                    self._tok_events.append((now, tokens))
                    return
                waits = []
                if self._req_times and len(self._req_times) >= self.max_rpm:
                    waits.append(60.0 - (now - self._req_times[0]))
                if self._tok_events and cur + tokens > self.max_tpm:
                    waits.append(60.0 - (now - self._tok_events[0][0]))
                wait = max(0.2, min(waits) if waits else 0.5)
            time.sleep(min(wait, 5.0))


_limiter = _RateLimiter(MAX_RPM, MAX_TPM)


def pick_deployment(router: bool = False) -> str:
    return (ROUTER_DEPLOYMENT if router and ROUTER_DEPLOYMENT else DEPLOYMENT)


def complete(prompt: str,
             image_path: Optional[str] = None,
             timeout: Optional[int] = None,
             *,
             tag: str = "[LLM/AZURE]",
             system: Optional[str] = None,
             max_output_tokens: Optional[int] = None,
             deployment: Optional[str] = None,
             call_id: int = 0) -> str:
    """Blocking chat completion against Azure OpenAI. Same contract as
    llm_service.complete() — returns the assistant text, raises
    RuntimeError on unrecoverable failure."""
    errors = check_configured()
    if errors:
        raise RuntimeError("Azure OpenAI is not configured: " + "; ".join(errors))

    dep = (deployment or DEPLOYMENT).strip()
    req_timeout = timeout or DEFAULT_TIMEOUT
    prompt_text, prompt_tokens, truncated, max_out = _limiter_and_budget(prompt, max_output_tokens)
    if truncated:
        log.warning("%s call #%d prompt truncated (exceeded the Azure input budget)", tag, call_id)

    messages = _build_messages(prompt_text, system, image_path, tag, call_id)
    body = _body(messages, max_out, stream=False)

    log.info("%s call #%d → %d input chars (~%d tokens), timeout=%ds deployment=%s",
             tag, call_id, len(prompt_text), prompt_tokens, req_timeout, dep)

    t0 = time.time()
    backoff = 1.0
    last_err: Optional[str] = None

    attempt = 1
    while attempt <= MAX_RETRIES:
        try:
            resp = requests.post(_url(dep), headers=_headers(),
                                 data=json.dumps(body), timeout=req_timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = str(e)
            log.warning("%s call #%d network error (attempt %d/%d): %s — retrying in %.1fs",
                        tag, call_id, attempt, MAX_RETRIES, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            attempt += 1
            continue

        if resp.status_code == 200:
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            content = ((choice.get("message") or {}).get("content") or "").strip()
            usage = data.get("usage") or {}
            log.info("%s call #%d done in %.1fs → %d chars out (in_tok=%s out_tok=%s)",
                     tag, call_id, time.time() - t0, len(content),
                     usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
            return content

        if resp.status_code == 400 and _swap_tokens_param(body, resp.text):
            continue  # same attempt, corrected parameter name

        if resp.status_code in _RETRYABLE_STATUS:
            last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            wait = _retry_after(resp, backoff)
            log.warning("%s call #%d Azure OpenAI %d (attempt %d/%d) — retrying in %.1fs",
                        tag, call_id, resp.status_code, attempt, MAX_RETRIES, wait)
            time.sleep(wait)
            backoff = min(backoff * 2, 30.0)
            attempt += 1
            continue

        # Non-retryable (401 bad key, 403, 404 bad deployment, other 4xx)
        log.error("%s call #%d non-retryable Azure OpenAI error %d: %s",
                  tag, call_id, resp.status_code, resp.text[:500])
        raise RuntimeError(f"Azure OpenAI error ({resp.status_code}): {resp.text[:500]}")

    log.error("%s call #%d gave up after %d retries: %s", tag, call_id, MAX_RETRIES, last_err)
    raise RuntimeError(f"Azure OpenAI request failed after {MAX_RETRIES} retries: {last_err}")


def complete_stream(prompt: str,
                    *,
                    tag: str = "[LLM/AZURE/STREAM]",
                    system: Optional[str] = None,
                    max_output_tokens: Optional[int] = None,
                    deployment: Optional[str] = None,
                    call_id: int = 0):
    """Streaming variant — yields text deltas (SSE). Mirrors the Bedrock
    path: no mid-stream retry; a pre-first-token failure raises normally
    so the caller can fall back to complete()."""
    errors = check_configured()
    if errors:
        raise RuntimeError("Azure OpenAI is not configured: " + "; ".join(errors))

    dep = (deployment or DEPLOYMENT).strip()
    prompt_text, prompt_tokens, truncated, max_out = _limiter_and_budget(prompt, max_output_tokens)
    if truncated:
        log.warning("%s call #%d prompt truncated", tag, call_id)

    messages = _build_messages(prompt_text, system, None, tag, call_id)
    body = _body(messages, max_out, stream=True)

    log.info("%s call #%d → %d input chars (~%d tokens) deployment=%s (streaming)",
             tag, call_id, len(prompt_text), prompt_tokens, dep)
    t0 = time.time()

    resp = requests.post(_url(dep), headers=_headers(),
                         data=json.dumps(body), timeout=DEFAULT_TIMEOUT, stream=True)
    if resp.status_code == 400 and _swap_tokens_param(body, resp.text):
        resp = requests.post(_url(dep), headers=_headers(),
                             data=json.dumps(body), timeout=DEFAULT_TIMEOUT, stream=True)
    if resp.status_code != 200:
        raise RuntimeError(f"Azure OpenAI stream error ({resp.status_code}): {resp.text[:500]}")

    n_chars = 0
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        for choice in chunk.get("choices") or []:
            delta = (choice.get("delta") or {}).get("content")
            if delta:
                n_chars += len(delta)
                yield delta
    log.info("%s call #%d stream done in %.1fs → %d chars out",
             tag, call_id, time.time() - t0, n_chars)
