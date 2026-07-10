"""Neo4j driver lifecycle and session helpers.

The rest of the persistence layer (knowledge_graph.py, definitions_store.py,
migrate_json_to_neo4j.py) calls into this module instead of constructing
its own driver so:

  * connection settings live in ONE place (env vars NEO4J_URI / NEO4J_USER
    / NEO4J_PASSWORD / NEO4J_DATABASE),
  * the driver is shared process-wide and cleanly closed at shutdown,
  * a deferred-connection mode (`READY=False` until the DB is reachable)
    means server.py can boot even if Neo4j is still starting up — the
    stores raise a clear error on first use rather than crashing at
    import time.

Public surface used by callers:

    get_driver()        → neo4j.Driver               (raises Neo4jUnavailable
                                                       if not ready)
    run(query, **params)→ list[Record]                (auto-managed session)
    read(query, **params), write(query, **params)
                        → run() variants that take advantage of routing on
                          a future cluster setup (read replicas, etc.)
    transaction(write=True) → context manager yielding a tx
    is_ready()          → bool                        (cheap probe)
    close()             → idempotent driver shutdown
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

from neo4j import GraphDatabase, Driver, Session, Transaction
from neo4j.exceptions import (
    ServiceUnavailable,
    AuthError,
    TransientError,
    Neo4jError,
)


log = logging.getLogger('rd.neo4j')

# The Neo4j Python driver logs every server-side notification at INFO via
# the `neo4j.notifications` logger. The bulk of these are PERFORMANCE
# hints (cartesian-product warnings for our untyped `MATCH (a {id:…}),
# (b {id:…})` patterns) and they completely drown the real server log
# under any non-trivial workload. We silence the notifications channel
# and surface only WARNING+ from the rest of the driver.
logging.getLogger('neo4j.notifications').setLevel(logging.WARNING)
logging.getLogger('neo4j').setLevel(logging.WARNING)


class Neo4jUnavailable(RuntimeError):
    """Raised when callers try to use Neo4j but the driver couldn't connect.

    The server still boots so the operator can read the error in the UI /
    logs and start the database; once it's reachable the next operation
    succeeds without a server restart (see `_ensure_driver`)."""


# ---------------------------------------------------------------------------
# Configuration — env-first so docker-compose / .env / shell exports all
# work without code changes. Defaults match the bundled docker-compose.yml
# so a developer who just runs `docker compose up -d` doesn't have to set
# anything before starting the Flask server.
# ---------------------------------------------------------------------------

def _cfg() -> dict[str, str]:
    # NEO4J_PASSWORD comes from Azure Key Vault (secret: neo4j-password)
    # via the centralised secret manager. Env var is a local-dev
    # fallback. Default `navicore-dev` lines up with docker-compose so
    # the bundled local stack still works without any KV access.
    from app.services.secret_manager import get_secret
    password = get_secret(
        'NEO4J_PASSWORD',
        env_fallback='NEO4J_PASSWORD',
        default='navicore-dev',
    ) or 'navicore-dev'
    return {
        'uri':      os.environ.get('NEO4J_URI',      'bolt://localhost:7687'),
        'user':     os.environ.get('NEO4J_USER',     'neo4j'),
        'password': password,
        'database': os.environ.get('NEO4J_DATABASE', 'neo4j'),
    }


# ---------------------------------------------------------------------------
# Driver lifecycle — a single Driver is shared by every store. The Neo4j
# Python driver is thread-safe; sessions are NOT, so callers must use
# `session()` / `run()` to get a fresh session per unit of work.
# ---------------------------------------------------------------------------

_LOCK    = threading.Lock()
_DRIVER: Driver | None = None
_LAST_FAILURE: tuple[float, str] | None = None  # (timestamp, message)


def _build_driver() -> Driver:
    cfg = _cfg()
    log.info("[NEO4J] connecting to %s as %s (db=%s)",
             cfg['uri'], cfg['user'], cfg['database'])
    driver = GraphDatabase.driver(
        cfg['uri'],
        auth=(cfg['user'], cfg['password']),
        # Defaults are sensible — keep them explicit so an operator
        # looking at this file knows what's tunable.
        max_connection_pool_size=50,
        connection_timeout=10.0,
        max_transaction_retry_time=15.0,
    )
    # Cheap round-trip so we surface bad credentials / unreachable host
    # at construction time rather than on the first real query.
    driver.verify_connectivity()
    return driver


def _ensure_driver() -> Driver:
    """Lazy-init the global driver. Re-tries on each call after a prior
    failure so the server doesn't need a restart once the operator brings
    Neo4j online."""
    global _DRIVER, _LAST_FAILURE
    with _LOCK:
        if _DRIVER is not None:
            return _DRIVER
        try:
            _DRIVER = _build_driver()
            _LAST_FAILURE = None
            return _DRIVER
        except (ServiceUnavailable, AuthError, OSError) as e:
            _LAST_FAILURE = (time.time(), f"{type(e).__name__}: {e}")
            log.error("[NEO4J] connect failed: %s", _LAST_FAILURE[1])
            raise Neo4jUnavailable(_LAST_FAILURE[1]) from e


def get_driver() -> Driver:
    """Return the shared driver, building it on first call. Raises
    `Neo4jUnavailable` with the underlying error message if connection
    fails — callers should catch this and degrade gracefully."""
    return _ensure_driver()


def is_ready() -> bool:
    """Cheap probe used by health endpoints / startup hooks. Does NOT
    raise; returns False when the driver can't reach the DB."""
    try:
        _ensure_driver()
        return True
    except Neo4jUnavailable:
        return False


def last_failure() -> str | None:
    """Last connection failure message, if any. Cleared on the first
    successful connect."""
    return _LAST_FAILURE[1] if _LAST_FAILURE else None


def close() -> None:
    """Idempotent shutdown. Call from atexit / Flask teardown."""
    global _DRIVER
    with _LOCK:
        if _DRIVER is not None:
            try:
                _DRIVER.close()
            except Exception as e:                       # pragma: no cover
                log.warning("[NEO4J] close error (ignored): %s", e)
            _DRIVER = None


# ---------------------------------------------------------------------------
# Session / transaction helpers. The driver auto-routes reads to followers
# on a cluster; on a single-instance dev setup both `read` and `write`
# behave identically.
# ---------------------------------------------------------------------------

def _session(**kwargs) -> Session:
    driver = _ensure_driver()
    cfg = _cfg()
    return driver.session(database=cfg['database'], **kwargs)


@contextmanager
def session(**kwargs):
    """Context-managed session. Closes on exit even if the body raised."""
    s = _session(**kwargs)
    try:
        yield s
    finally:
        try:
            s.close()
        except Exception as e:                            # pragma: no cover
            log.warning("[NEO4J] session close error: %s", e)


@contextmanager
def transaction(write: bool = True):
    """Yield a managed transaction. Commits on clean exit, rolls back on
    exception. Use for multi-statement work that must be atomic."""
    with session() as s:
        tx_factory = s.begin_transaction
        tx: Transaction = tx_factory()
        try:
            yield tx
            tx.commit()
        except Exception:
            try:
                tx.rollback()
            except Exception as e:                        # pragma: no cover
                log.warning("[NEO4J] rollback error: %s", e)
            raise


def run(query: str, **params) -> list[dict[str, Any]]:
    """Run a single query in its own auto-managed transaction. Returns a
    list of dicts (one per record, key per RETURN alias). This is the
    workhorse for simple CRUD — multi-step logic should use
    `transaction()` instead so the steps are atomic."""
    with session() as s:
        result = s.run(query, **params)
        return [r.data() for r in result]


def read(query: str, **params) -> list[dict[str, Any]]:
    """Read-side variant. On a cluster this would route to a follower;
    on a single-node deployment it is identical to `run`."""
    with session(default_access_mode='READ') as s:
        result = s.run(query, **params)
        return [r.data() for r in result]


def write(query: str, **params) -> list[dict[str, Any]]:
    """Write-side variant. Same as `run` but documents intent and pairs
    with the read-side helper above."""
    return run(query, **params)


def run_many(statements: Iterable[tuple[str, dict[str, Any]]]) -> None:
    """Apply a batch of (query, params) inside one transaction. Used by
    bulk imports so we don't pay the round-trip per statement."""
    with transaction(write=True) as tx:
        for q, p in statements:
            tx.run(q, **p)


# ---------------------------------------------------------------------------
# Retry wrapper — Neo4j classifies `TransientError` as safely retryable
# (deadlock, lock acquisition timeout, leader election in cluster). The
# stores wrap their multi-step operations in this so a transient blip
# doesn't bubble up to the UI as a 500.
# ---------------------------------------------------------------------------

def retry(fn, *, attempts: int = 3, delay: float = 0.25):
    """Call `fn` up to `attempts` times, sleeping `delay` between tries.
    Re-raises the last exception if all attempts fail. Returns whatever
    `fn` returns on success."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except TransientError as e:
            last = e
            log.warning("[NEO4J] transient (%s/%s): %s", i + 1, attempts, e)
            time.sleep(delay * (2 ** i))
        except Neo4jError as e:
            # Non-transient — no point retrying.
            raise
    assert last is not None
    raise last
