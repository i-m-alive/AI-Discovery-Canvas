"""
Backend configuration.

All env vars consumed by the backend are surfaced here. Sub-systems
(auth, database) have their own narrower config modules that read the
same env vars — this module is the canonical reference, kept
intentionally small so an operator can audit "what does this app read?"
in one place.

Loading is at import time. If python-dotenv is installed and `.env` is
present, it is loaded BEFORE we read os.environ.

ADAPTATION NOTE (ai-discovery-canvas scaffold): this was originally a
TRIMMED copy of frd-generator's `backend/app/core/config.py` with every
POSTGRES_* var/helper dropped (Postgres was out of scope for the initial
lean backbone). POSTGRES_* is now back — see the "Postgres" section below
— to back the BA -> Projects -> Workshops hierarchy with a real local
database (app/postgres/), same shape as frd-generator's vars but defaulted
for THIS project's own local instance (db `ai_discovery_canvas`, peer auth,
sslmode disable) rather than an Azure-hosted one. The PIM_* feature flags
remain dropped (still out of scope). Everything else (server, CORS, Neo4j,
AWS Bedrock comments, object storage) is unchanged in shape.
Two defaults were changed for this project: PORT 5004 -> 5101 (frd-generator
uses 5004) and CORS_ORIGINS now defaults to :5173. That's deliberately the
SAME port frd-generator's own Vite frontend uses — this project's Next.js
dev server is pinned to it too (see frontend/package.json) so the browser
sign-in flow can reuse NaviCore's already-registered Entra redirect URI
(http://localhost:5173) with zero Azure Portal changes (see
frontend/app/lib/msalConfig.js).
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
# The frontend is a Next.js app pinned to :5173 (see the module docstring
# for why — it's the port registered for Microsoft sign-in, not a Next.js
# default). In practice next.config.mjs rewrites proxy the API through
# :5173 itself (same-origin from the browser's point of view), so CORS
# mostly matters for direct API calls made without the rewrite. Production
# deploys override via env (comma-separated list).
CORS_ORIGINS: list[str] = _env_list('CORS_ORIGINS', [
    'http://localhost:5173',
    'http://127.0.0.1:5173',
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

# ── AWS Bedrock (LLM) -------------------------------------------------
# Surfaced here for visibility only; the actual values are consumed by
# `app.services.llm_service` at import time. Swapped from Azure OpenAI to
# AWS Bedrock per project decision — llm_service.py now calls Bedrock's
# `converse` API via boto3.
AWS_REGION:       str = _env('AWS_REGION', 'us-east-1')
BEDROCK_MODEL_ID: str = _env('BEDROCK_MODEL_ID', '')
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN are
# intentionally NOT surfaced here — they're standard boto3 env-var names
# that the AWS SDK's own credential chain resolves directly from
# os.environ (or ~/.aws/credentials, or an IAM role). Paste real values
# into backend/.env; never hold them at module scope here.

# `app/services/rag/config.py` + `rag/embedder.py` also call AWS Bedrock
# now (Titan/Cohere embedding models, picked via BEDROCK_EMBEDDING_MODEL_ID)
# — no Azure OpenAI dependency remains anywhere in this project.

# ── Postgres (app/postgres/ — Projects/Workshops application database) ──
# Defaulted for THIS project's own local instance (created via `createdb
# ai_discovery_canvas`; peer auth under the current OS user, no password).
# POSTGRES_PASSWORD is intentionally NOT surfaced here — resolved on demand
# by `app.services.secret_manager` (env fallback only; there's no Key Vault
# secret expected for a purely local dev database).
POSTGRES_HOST: str = _env('POSTGRES_HOST', 'localhost')
POSTGRES_PORT: int = _env_int('POSTGRES_PORT', 5432)
POSTGRES_DB:   str = _env('POSTGRES_DB',   'ai_discovery_canvas')
POSTGRES_USER: str = _env('POSTGRES_USER', '')
# 'disable' by default — this is a local peer-auth Postgres, not a TLS-only
# hosted one. connection.py forces 'require' regardless for an Azure host.
POSTGRES_SSLMODE: str = _env('POSTGRES_SSLMODE', 'disable')

POSTGRES_POOL_SIZE:         int = _env_int('POSTGRES_POOL_SIZE', 5)
POSTGRES_POOL_MAX_OVERFLOW: int = _env_int('POSTGRES_POOL_MAX_OVERFLOW', 5)
POSTGRES_POOL_RECYCLE_S:    int = _env_int('POSTGRES_POOL_RECYCLE_S', 1800)
POSTGRES_POOL_TIMEOUT_S:    int = _env_int('POSTGRES_POOL_TIMEOUT_S', 10)
POSTGRES_STATEMENT_TIMEOUT_MS: int = _env_int('POSTGRES_STATEMENT_TIMEOUT_MS', 5000)


def postgres_configured() -> bool:
    """True iff POSTGRES_HOST + POSTGRES_USER are set. POSTGRES_PASSWORD
    is allowed to be empty (local peer auth)."""
    return bool(POSTGRES_HOST and POSTGRES_USER)


# ── Object storage -----------------------------------------------------
# Durable BYTES store for raw repo snapshots, documents/images, and generated
# outputs. LOCAL-only for now: content-addressed files under OBJECT_STORE_DIR
# (defaults to backend/data/object_store). The backend is selected via
# OBJECT_STORE_BACKEND so a remote/object backend can be added later WITHOUT
# changing any call site — but `local` is the only implemented backend today.
OBJECT_STORE_BACKEND: str = _env('OBJECT_STORE_BACKEND', 'local').lower()
# Empty default → resolved at use time to backend/data/object_store.
OBJECT_STORE_DIR:     str = _env('OBJECT_STORE_DIR', '')
