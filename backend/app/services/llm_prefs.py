"""
Per-user LLM-backend preference ('bedrock' | 'azure_openai').

Stored on users.llm_provider (see app/postgres/models/user.py), chosen
from the header avatar menu (frontend UserMenu → /api/settings/llm),
and consumed by llm_service's per-call provider resolution.

A tiny in-process cache sits in front of the users table so resolving
the preference costs one DB query per user per process lifetime, not
one per LLM call. The settings POST writes through the cache, so a
change takes effect on the user's very next request. (The cache is
process-local by design — this backend runs as a single threaded Flask
process, same trade-off the auth session store already makes.)
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from app.postgres import session_scope
from app.postgres.repositories import users as users_repo

log = logging.getLogger("app.llm.prefs")

VALID_PROVIDERS = ("bedrock", "azure_openai")

_cache: dict[str, Optional[str]] = {}
_lock = threading.Lock()

_MISS = object()


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def get_for(email: str) -> Optional[str]:
    """The user's stored choice, or None when they never picked one
    (caller falls back to the platform default)."""
    email = _norm_email(email)
    if not email:
        return None
    with _lock:
        hit = _cache.get(email, _MISS)
    if hit is not _MISS:
        return hit
    provider: Optional[str] = None
    try:
        with session_scope() as s:
            user = users_repo.get_by_email(s, email)
            p = (user.llm_provider or "").strip().lower() if user else ""
            provider = p if p in VALID_PROVIDERS else None
    except Exception as e:  # DB hiccup — don't take LLM calls down with it
        log.warning("could not read llm preference for %s: %s", email, e)
        return None
    with _lock:
        _cache[email] = provider
    return provider


def set_for(email: str, provider: Optional[str]) -> bool:
    """Persist a choice (or None to clear). Returns False when the user
    row doesn't exist or the provider id is unknown."""
    email = _norm_email(email)
    if not email:
        return False
    if provider is not None and provider not in VALID_PROVIDERS:
        return False
    with session_scope() as s:
        if not users_repo.set_llm_provider(s, email, provider):
            return False
    with _lock:
        _cache[email] = provider
    log.info("llm preference for %s → %s", email, provider or "<default>")
    return True
