"""
Session management for the Postgres application database.

Two access shapes are exposed:

`SessionLocal()`
    A sessionmaker bound to the lazy engine. Returns a brand-new
    `Session`. Callers must `.close()` it (or use the `session_scope()`
    context manager).

`session_scope()`
    Recommended for repository code. Yields a Session inside a
    try/commit/rollback/close block.

`with_session(fn)`
    Higher-order helper for route code: wraps a callable that needs a
    Session, runs it inside `session_scope`, and returns its result.

All helpers return None / no-op when Postgres is not configured. The
entire Postgres surface must degrade gracefully so the rest of the app
(Neo4j, RAG, local-file document storage) keeps working without
operator intervention.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator, Optional, TypeVar

from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import log_exc
from app.postgres import connection as _conn


_factory: Optional[sessionmaker] = None


def _get_factory() -> Optional[sessionmaker]:
    global _factory
    eng = _conn.get_engine()
    if eng is None:
        return None
    if _factory is None:
        _factory = sessionmaker(
            bind=eng,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _factory


def SessionLocal() -> Optional[Session]:                  # noqa: N802
    """Mint a new Session (or None if Postgres isn't configured)."""
    factory = _get_factory()
    if factory is None:
        return None
    return factory()


@contextmanager
def session_scope() -> Iterator[Optional[Session]]:
    """Yield a session inside a commit/rollback/close envelope.

    Yields None when Postgres isn't configured — callers must check
    before issuing queries:

        with session_scope() as s:
            if s is None:
                return None
            ...
    """
    session = SessionLocal()
    if session is None:
        yield None
        return
    try:
        yield session
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception as rb_err:
            log_exc('[POSTGRES/ROLLBACK]', rb_err)
        raise
    finally:
        try:
            session.close()
        except Exception as cls_err:
            log_exc('[POSTGRES/SESSION_CLOSE]', cls_err)


R = TypeVar('R')


def with_session(fn: Callable[[Session], R]) -> Optional[R]:
    """Run `fn(session)` inside a managed scope. Returns None when
    Postgres isn't configured OR on any exception during fn (the
    exception is logged but not re-raised)."""
    try:
        with session_scope() as s:
            if s is None:
                return None
            return fn(s)
    except Exception as e:
        log_exc('[POSTGRES/WITH_SESSION]', e)
        return None


def dispose_factory() -> None:
    """Reset the sessionmaker (used by connection.dispose_engine's
    teardown path so the factory doesn't keep a stale engine handle)."""
    global _factory
    _factory = None
