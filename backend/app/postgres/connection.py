"""
PostgreSQL connection / engine factory.

Builds a single SQLAlchemy `Engine` per process (lazy + idempotent).
Non-secret configuration comes from `app.core.config` (env-driven).
POSTGRES_PASSWORD is resolved via `app.services.secret_manager` at
engine-build time (env fallback only — no Key Vault secret named
'postgres-password' is expected for this project's purely local
Postgres instance, so an empty value here is the normal, expected case
under peer auth).

Degraded-mode boot
~~~~~~~~~~~~~~~~~~
If POSTGRES_HOST or POSTGRES_USER is missing, ``get_engine()`` returns
``None``. Every caller MUST handle that — the projects/workshops routes
degrade to "database not configured" responses rather than crashing;
Neo4j-backed features (graph_rag) and the RAG subsystem are entirely
independent of this module and keep working regardless.
"""

from __future__ import annotations

import threading
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy import event
from sqlalchemy.engine import Engine, create_engine

from app.core import config as app_config
from app.core.logging import log, log_exc


_engine: Optional[Engine] = None
_engine_lock = threading.RLock()
_last_failure: Optional[str] = None


def _is_azure_host(host: str) -> bool:
    return host.lower().endswith('.postgres.database.azure.com')


def _resolve_sslmode(host: str, configured: str) -> str:
    """Force `require` on Azure-hosted Postgres regardless of operator
    config (kept for parity/future-proofing — this project's own
    instance is local, so this branch is inert today). Local DBs honour
    POSTGRES_SSLMODE (defaults to 'disable' for peer-auth localhost)."""
    mode = (configured or '').strip().lower() or 'disable'
    if _is_azure_host(host):
        return 'require'
    return mode


def build_database_url() -> Optional[str]:
    """Compose a SQLAlchemy URL from the env-driven config.

    Returns None if mandatory components are missing — callers must
    treat that as "Postgres disabled" and continue in degraded mode.
    """
    host = (app_config.POSTGRES_HOST or '').strip()
    user = (app_config.POSTGRES_USER or '').strip()
    if not host or not user:
        return None

    from app.services.secret_manager import get_secret
    password = get_secret('POSTGRES_PASSWORD', env_fallback='POSTGRES_PASSWORD') or ''

    db   = (app_config.POSTGRES_DB or 'postgres').strip()
    port = int(app_config.POSTGRES_PORT or 5432)
    sslmode = _resolve_sslmode(host, app_config.POSTGRES_SSLMODE)

    auth = quote_plus(user)
    if password:
        auth = f'{auth}:{quote_plus(password)}'

    return (
        f'postgresql+psycopg2://{auth}@{host}:{port}/{quote_plus(db)}'
        f'?sslmode={sslmode}'
    )


def _attach_statement_timeout(engine: Engine, timeout_ms: int) -> None:
    """Apply a per-connection `statement_timeout` so a runaway query
    can't hold a Flask worker hostage. Fires once per new pooled
    connection."""
    if timeout_ms <= 0:
        return

    @event.listens_for(engine, 'connect')
    def _on_connect(dbapi_conn, _connection_record):
        try:
            with dbapi_conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
        except Exception as e:
            log.warning("[POSTGRES] statement_timeout set failed: %s", e)


def get_engine() -> Optional[Engine]:
    """Return the process-wide SQLAlchemy engine, building it lazily on
    first call. Returns None when Postgres is not configured."""
    global _engine, _last_failure
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        url = build_database_url()
        if url is None:
            _last_failure = (
                'POSTGRES_HOST / POSTGRES_USER not set — Postgres layer disabled.'
            )
            log.info('[POSTGRES] %s', _last_failure)
            return None
        try:
            _engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=app_config.POSTGRES_POOL_SIZE,
                max_overflow=app_config.POSTGRES_POOL_MAX_OVERFLOW,
                pool_recycle=app_config.POSTGRES_POOL_RECYCLE_S,
                pool_timeout=app_config.POSTGRES_POOL_TIMEOUT_S,
                hide_parameters=False,
                future=True,
                connect_args={
                    'connect_timeout': 10,
                    'application_name': 'ai-discovery-canvas',
                },
            )
            _attach_statement_timeout(_engine, app_config.POSTGRES_STATEMENT_TIMEOUT_MS)
            log.info(
                '[POSTGRES] engine ready (host=%s db=%s sslmode=%s pool=%d+%d)',
                app_config.POSTGRES_HOST, app_config.POSTGRES_DB,
                _resolve_sslmode(app_config.POSTGRES_HOST, app_config.POSTGRES_SSLMODE),
                app_config.POSTGRES_POOL_SIZE,
                app_config.POSTGRES_POOL_MAX_OVERFLOW,
            )
            _last_failure = None
            return _engine
        except Exception as e:
            _last_failure = str(e)
            log_exc('[POSTGRES/ENGINE]', e)
            _engine = None
            return None


def dispose_engine() -> None:
    """Tear down the engine + its connection pool."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception as e:
                log_exc('[POSTGRES/DISPOSE]', e)
        _engine = None


def is_ready() -> bool:
    """Probe the engine by checking out a connection and pinging."""
    global _last_failure
    eng = get_engine()
    if eng is None:
        return False
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql('SELECT 1')
        return True
    except Exception as e:
        msg = str(e).split('\n', 1)[0]
        _last_failure = msg or e.__class__.__name__
        log_exc('[POSTGRES/PING]', e)
        return False


def last_failure() -> Optional[str]:
    return _last_failure


def is_configured() -> bool:
    """True iff the env vars necessary to attempt a Postgres connection
    are present. Cheaper than is_ready() — used by callers that want to
    skip Postgres work without paying a round trip."""
    return app_config.postgres_configured()
