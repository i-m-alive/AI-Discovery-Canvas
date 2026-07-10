"""
Database layer — Neo4j client + schema.

    neo4j_client       Driver lifecycle, session helpers, retry wrapper.
                       Re-exported as `neo4j_store` for the same reason
                       frd-generator did — a couple of copied modules
                       (routes/health.py) import it by that name.
    schema             Labels, relationship types, constraints, indexes.

`bootstrap_neo4j()` runs at app-factory time: apply schema. Failures are
logged but never abort boot — operators can still hit `/readyz`, see the
error, start Neo4j, and the next request will retry without a server
restart.

ADAPTATION NOTE (ai-discovery-canvas scaffold): this is a TRIMMED rewrite
of frd-generator's `backend/app/database/__init__.py`. The upstream module
also built a `KnowledgeGraph` in-memory mirror (`knowledge_graph.py`),
auto-imported legacy JSON backups via `migrations/json_to_neo4j.py`, and
re-exported `definitions_store.py` / `workflow_assets.py` stores. NONE of
those modules were copied into this project — they carry the Atlas /
Workflow-Builder / PIM domain model, which is out of scope for the
"AI Discovery Canvas" bootstrap (see root README). What remains here is
exactly the generic part: connect the driver, apply the schema. A future
`app/routes/canvas` module builds its own graph shape directly via
`neo4j_client` (or its own schema module) rather than reusing the Atlas
schema in `schema.py` verbatim — `schema.py` was copied over mostly as a
reference/example of the constraint-and-index pattern used here; trim or
replace its statements once the canvas's own node/relationship model is
decided.
"""

from __future__ import annotations

from app.core.logging import log, log_exc

# Re-export under the short name the rest of the copied code expects.
from app.database import neo4j_client as neo4j_store   # noqa: F401
from app.database import schema as neo4j_schema


def bootstrap_neo4j() -> None:
    """Connect the driver and apply the schema. Safe to call once at
    app-factory time. Never raises — a Neo4j outage at boot degrades the
    process to "not ready" (see /readyz) rather than crashing it."""
    try:
        if not neo4j_store.is_ready():
            log.warning("[NEO4J] not reachable at boot - server starting in "
                        "degraded mode (last error: %s)",
                        neo4j_store.last_failure() or 'unknown')
            return
        neo4j_schema.initialize_schema()
    except Exception as e:
        log_exc('[NEO4J/BOOTSTRAP]', e)


__all__ = [
    'bootstrap_neo4j',
    'neo4j_store',
    'neo4j_schema',
]
