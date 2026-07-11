"""
PostgreSQL application database layer.

Separate from `app/database/` (Neo4j — knowledge-graph entities for
graph_rag) and from `app/services/rag/` (FAISS vector index). This
layer holds the relational/tabular data the app's own UI is built
around: BA user accounts, Projects, Workshops (each Workshop's board
JSON), and the Prepare-zone/generated-document metadata that used to
live in flat JSON index files.

Public surface
~~~~~~~~~~~~~~
    bootstrap_postgres()   -- apply schema (Base.metadata.create_all)
                              at app-factory time.
    is_ready() / is_configured() / get_engine() / last_failure() /
    SessionLocal / session_scope / with_session
                           -- re-exported from connection.py / session.py.
    Base                   -- SQLAlchemy declarative base.
    models                 -- model package (User, Project, Workshop, ...).

Boot semantics
~~~~~~~~~~~~~~
`bootstrap_postgres()` MUST be tolerant of Postgres being unreachable
or not configured — every other subsystem (Neo4j, RAG, local-file
document storage) is independent of this one and keeps working
regardless. Schema management is `Base.metadata.create_all()` only (no
Alembic) — appropriate for this project's single-developer, low-
ceremony scaffold; see the sibling frd-generator project for the
Alembic-based pattern if/when this needs real migration history.
"""

from __future__ import annotations

from app.postgres.base import Base                                # noqa: F401
from app.postgres.connection import (                              # noqa: F401
    get_engine, is_configured, is_ready, last_failure, dispose_engine,
)
from app.postgres.session import (                                 # noqa: F401
    SessionLocal, session_scope, with_session, dispose_factory,
)

# Eagerly import the models package so every Base.metadata-bound class
# is registered before create_all() sees Base.metadata.
from app.postgres import models                                    # noqa: F401,E402


def bootstrap_postgres() -> None:
    """Wire Postgres on app boot — connect, create any missing tables.

    Every error is caught + logged; the rest of the app keeps running
    regardless (matches the Neo4j/RAG degraded-boot precedent)."""
    from app.core.logging import log, log_exc
    from sqlalchemy import inspect

    log.info('[POSTGRES] bootstrap: starting')

    if not is_configured():
        log.info('[POSTGRES] not configured (POSTGRES_HOST / POSTGRES_USER '
                 'are blank) — skipping bootstrap. Set them in backend/.env '
                 'to activate Projects/Workshops.')
        return

    eng = get_engine()
    if eng is None:
        log.error('[POSTGRES] engine could not be constructed — Projects/'
                  'Workshops disabled (last error: %s).', last_failure() or 'unknown')
        return

    if not is_ready():
        log.error('[POSTGRES] connection probe failed — Projects/Workshops '
                  'disabled until the DB is reachable. Last error: %s',
                  last_failure() or 'unknown')
        return

    try:
        Base.metadata.create_all(eng)
    except Exception as e:
        log_exc('[POSTGRES/CREATE_ALL]', e)
        log.error('[POSTGRES] schema initialisation FAILED — Projects/'
                  'Workshops disabled until fixed.')
        return

    try:
        insp = inspect(eng)
        present = set(insp.get_table_names(schema='public'))
        expected = list(Base.metadata.tables.keys())
        missing = [t for t in expected if t not in present]
        if missing:
            log.error('[POSTGRES] MISSING tables after create_all: %s', ', '.join(missing))
        else:
            log.info('[POSTGRES] all %d expected tables present: %s',
                     len(expected), ', '.join(sorted(present & set(expected))))
    except Exception as e:
        log_exc('[POSTGRES/INSPECT]', e)

    log.info('[POSTGRES] bootstrap: complete — Projects/Workshops are live')


__all__ = [
    'Base',
    'SessionLocal',
    'bootstrap_postgres',
    'dispose_engine',
    'dispose_factory',
    'get_engine',
    'is_configured',
    'is_ready',
    'last_failure',
    'session_scope',
    'with_session',
]
