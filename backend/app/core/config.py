"""
Backend configuration.

All env vars consumed by the backend are surfaced here. Sub-systems
(auth, database) have their own narrower config modules that read the
same env vars — this module is the canonical reference, kept
intentionally small so an operator can audit "what does this app read?"
in one place.

Loading is at import time. If python-dotenv is installed and `.env` is
present, it is loaded BEFORE we read os.environ.

ADAPTATION NOTE (ai-discovery-canvas scaffold): this is a TRIMMED copy of
frd-generator's `backend/app/core/config.py`. Dropped entirely: every
POSTGRES_* var/helper and the PIM_* feature flags — this project does not
carry the Postgres or Project-Intelligence-Model subsystems (out of scope
for the lean backbone; see the root README). Everything else (server,
CORS, Neo4j, Azure OpenAI comments, object storage) is unchanged in shape.
Two defaults were changed for this project: PORT 5004 -> 5101 (frd-generator
uses 5004) and CORS_ORIGINS now defaults to Next.js's dev port :3000 (the
frontend here is a fresh Next.js app, not frd-generator's Vite/React-Router
shell).
"""

from __future__ import annotations

import os
from pathlib import Path


def _maybe_load_dotenv() -> None:
    """Best-effort .env load. Silent no-op if python-dotenv isn't
    installed — we don't add a hard dep just for this."""
    backend_root = Path(__file__).resolve().parent.parent.parent
    env_path = backend_root / '.env'
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv          # type: ignore[import-not-found]
    except ImportError:
        # Manual minimal parser — handles KEY=VALUE lines, ignores comments.
        for line in env_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                continue
            k, v = line.split('=', 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
        return
    load_dotenv(env_path, override=False)


_maybe_load_dotenv()


def _env(key: str, default: str = '') -> str:
    v = os.environ.get(key)
    return v.strip() if v is not None else default


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = _env(key)
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(',') if x.strip()]


# ── Environment ------------------------------------------------------
# Selects the deployment profile: `local` / `development` / `production`.
# It drives prod-safety defaults (DEBUG forced off in production) and lets
# ops branch behaviour without code edits.
APP_ENV: str = _env('APP_ENV', 'development').lower()


def is_production() -> bool:
    return APP_ENV == 'production'


# ── Server -----------------------------------------------------------
HOST: str  = _env('HOST', '0.0.0.0')
# 5101 (not frd-generator's 5004) so both backends can run at once on one
# machine during the reuse/bootstrap period.
PORT: int  = _env_int('PORT', 5101)
# DEBUG defaults off, and is hard-forced off in production regardless of
# any stray env so a misconfig can't ship a debug server.
DEBUG: bool = (_env('DEBUG', 'false').lower() in ('1', 'true', 'yes')) and not is_production()

# ── CORS -------------------------------------------------------------
# The frontend is a Next.js app (default dev port :3000). List explicit
# origins so cookies can travel across the frontend/backend split during
# dev. In practice next.config.js rewrites proxy the API through :3000
# itself (same-origin from the browser's point of view), so CORS mostly
# matters for direct API calls made without the rewrite. Production
# deploys override via env (comma-separated list).
CORS_ORIGINS: list[str] = _env_list('CORS_ORIGINS', [
    'http://localhost:3000',
    'http://127.0.0.1:3000',
])

# ── Neo4j ------------------------------------------------------------
# Kept identical to frd-generator's setup, but this project's bundled
# docker-compose.yml maps DIFFERENT host ports (7475/7688) so it can run
# alongside the frd-generator stack without a collision. NEO4J_URI below
# still points at the container's own internal port (7687) — only the
# HOST-side mapping differs; see docker-compose.yml.
#
# NEO4J_PASSWORD is intentionally NOT surfaced here — secrets are resolved
# on demand by `app.services.secret_manager` (Key Vault first, env
# fallback). Keeping it out of the module namespace stops a stray
# repr/log from echoing it.
NEO4J_URI:      str = _env('NEO4J_URI',      'bolt://localhost:7687')
NEO4J_USER:     str = _env('NEO4J_USER',     'neo4j')
NEO4J_DATABASE: str = _env('NEO4J_DATABASE', 'neo4j')

# ── Azure OpenAI (LLM) ----------------------------------------------
# Surfaced here for visibility only; the actual values are consumed by
# `app.services.llm_service` at import time. Listing them in this
# canonical config module preserves the "one place to audit env reads"
# invariant — even though they aren't re-read here, an operator can see
# them alongside the other backend env vars.
AZURE_OPENAI_ENDPOINT:    str = _env('AZURE_OPENAI_ENDPOINT',    '')
AZURE_OPENAI_API_VERSION: str = _env('AZURE_OPENAI_API_VERSION', '2024-12-01-preview')
AZURE_OPENAI_DEPLOYMENT:  str = _env('AZURE_OPENAI_DEPLOYMENT',  'gpt-4.1')
# AZURE_OPENAI_API_KEY is intentionally not surfaced here — it's a
# secret resolved on demand via `app.services.secret_manager` (Key
# Vault primary, env fallback). Never holds the value at module scope.

# ── Object storage -----------------------------------------------------
# Durable BYTES store for raw repo snapshots, documents/images, and generated
# outputs. LOCAL-only for now: content-addressed files under OBJECT_STORE_DIR
# (defaults to backend/data/object_store). The backend is selected via
# OBJECT_STORE_BACKEND so a remote/object backend can be added later WITHOUT
# changing any call site — but `local` is the only implemented backend today.
OBJECT_STORE_BACKEND: str = _env('OBJECT_STORE_BACKEND', 'local').lower()
# Empty default → resolved at use time to backend/data/object_store.
OBJECT_STORE_DIR:     str = _env('OBJECT_STORE_DIR', '')
