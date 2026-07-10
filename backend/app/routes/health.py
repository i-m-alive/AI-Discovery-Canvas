"""
Health & readiness probes for container orchestration.

Two endpoints, both PUBLIC (no auth) and cheap, so Azure Container Apps /
Docker / a load balancer can poll them without a session:

  GET /healthz   Liveness. Always 200 while the process is up — never
                 touches a database. ACA restarts the replica if this
                 fails, so it must NOT depend on Neo4j/Postgres being up.

  GET /readyz    Readiness. Reports dependency reachability. Returns 200
                 when ready, 503 when a hard dependency (Neo4j — the
                 system of record) is down, so ACA stops routing traffic
                 to a replica that can't serve graph requests yet.
                 Postgres is additive: reported, but only fails readiness
                 when it is CONFIGURED yet unreachable (disabled = fine).

Neither response ever includes a secret — only boolean reachability and
coarse status strings.
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify

bp = Blueprint('health', __name__)


# Whether a Neo4j outage should mark the replica not-ready. Default on —
# this is a knowledge-graph app, so Neo4j down means "don't route here".
_REQUIRE_NEO4J = os.environ.get('HEALTH_REQUIRE_NEO4J', '1').lower() in ('1', 'true', 'yes')


@bp.route('/healthz', methods=['GET'])
def healthz():
    """Liveness — process is up. No dependency checks by design."""
    return jsonify({'status': 'ok'}), 200


@bp.route('/readyz', methods=['GET'])
def readyz():
    """Readiness — dependencies reachable."""
    checks: dict[str, str] = {}
    ready = True

    # Neo4j — system of record.
    neo_ok = False
    try:
        from app.database import neo4j_store
        neo_ok = neo4j_store.is_ready()
    except Exception:
        neo_ok = False
    checks['neo4j'] = 'up' if neo_ok else 'down'
    if _REQUIRE_NEO4J and not neo_ok:
        ready = False

    # Postgres — additive analytics/metadata layer.
    try:
        from app.postgres import is_configured, is_ready as pg_ready
        if is_configured():
            pg_ok = pg_ready()
            checks['postgres'] = 'up' if pg_ok else 'down'
            if not pg_ok:
                ready = False
        else:
            checks['postgres'] = 'disabled'
    except Exception:
        checks['postgres'] = 'error'

    return jsonify({
        'status': 'ready' if ready else 'degraded',
        'checks': checks,
    }), (200 if ready else 503)


def install_health(app) -> None:
    """Mount the health blueprint. The host app must also list /healthz
    and /readyz as public paths in the auth gate (see app/__init__.py)."""
    app.register_blueprint(bp)
