"""
Application factory — AI Discovery Canvas backend.

    from app import create_app
    app = create_app()

`create_app()` builds the Flask instance, modeled on frd-generator's
factory (`backend/app/__init__.py`) but wiring only the lean subset this
product needs:

  * env-driven config (app.core.config) + structured logging
  * secret manager preload (Neo4j password — AWS Bedrock credentials for
    both chat and embeddings are resolved by boto3's own default chain,
    not through Key Vault, so they're not preloaded here)
  * auth gate + /auth/* + /login routes (app.auth)
  * Neo4j boot + schema (app.database)
  * Postgres boot + schema (app.postgres) — BA -> Projects -> Workshops
  * Privacy & Guardrails REST surface (app.routes.guardrails)
  * Source connections — the generic OAuth "Connect <provider>" pattern
    (app.routes.connections)
  * Health/readiness probes (app.routes.health)
  * A tiny agent-backbone proof route (app.routes.agents /api/agents/ping)

Deliberately NOT wired (out of scope for this product; see root README):
People, Project-Intelligence-Model/Intelligence, Modernize, Disrupt, the
legacy Workflow-Builder routes/pages, Salesforce. None of those subsystems'
source files were copied into this project, so there is nothing here to
import even if we wanted to.

Importing this module is side-effect-free; the factory is only invoked
from `main.py` (dev) or a WSGI server (prod).
"""

from __future__ import annotations

from flask import Flask
from flask_cors import CORS

from app.core import config as app_config  # noqa: F401  — loads env at import
from app.core.logging import configure_logging, log


def create_app() -> Flask:
    """Build the Flask app. Idempotent under repeated calls only at the
    process boundary — multiple calls in one process would re-register
    blueprints and is not supported."""
    flask_app = Flask(__name__)
    configure_logging()
    CORS(flask_app, supports_credentials=True,
         origins=app_config.CORS_ORIGINS)

    # ---- Health / readiness probes ----------------------------------
    # Mounted first and kept dependency-light so a container orchestrator
    # or `docker compose`'s healthcheck can poll /healthz and /readyz
    # without auth. They're added to the auth gate's public list below.
    from app.routes.health import install_health
    install_health(flask_app)

    # ---- Secret manager preload -------------------------------------
    # Resolve every KEY-VAULT-BACKED credential once at boot so a
    # misconfigured Key Vault (or env) is visible in the startup log
    # instead of failing on the first request. NEO4J_PASSWORD is the only
    # one left — AWS Bedrock (chat AND embeddings, app/services/llm_service.py
    # + app/services/rag/embedder.py) is authenticated via boto3's own
    # default credential chain (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/
    # AWS_SESSION_TOKEN env vars, ~/.aws/credentials, or an IAM role), not
    # through this Key-Vault-oriented secret manager, so there's nothing
    # LLM/embedding-related to preload here anymore.
    from app.services import secret_manager
    try:
        sources = secret_manager.preload(['NEO4J_PASSWORD'])
        # `sources` maps logical name -> 'keyvault' | 'env' | 'default' |
        # 'missing'. The VALUES themselves are never echoed.
        log.info("[STARTUP] Secret manager ready - vault=%s sources=%s",
                 secret_manager.vault_url(), sources)
        missing = [n for n, s in sources.items() if s in ('missing', 'error')]
        if missing:
            log.warning("[STARTUP] Secrets not resolved at boot: %s - dependent "
                        "subsystems will degrade until configured", ', '.join(missing))
    except Exception as e:
        # Catch-all so a bad credential chain can't kill the whole boot.
        log.warning("[STARTUP] Secret manager preload skipped (%s) - using "
                    "per-call lookups", e.__class__.__name__)

    # ---- Auth (Microsoft Entra ID + mock) ---------------------------
    # Public prefixes:
    #   /healthz     -- container liveness probe
    #   /readyz      -- container readiness probe
    #   /connections/-- OAuth redirect target for "Connect <provider>": the
    #                    browser arrives from the provider WITHOUT our gate
    #                    applying. /connections/<p>/start is still gated by
    #                    its own explicit current_user check despite this
    #                    prefix being public.
    # NOTE: no '/assets/' and no bare '/' here — those were frd-generator's
    # server-rendered Vite/React-Router shell. This project's frontend is a
    # separate Next.js process (its own dev server on :3000, proxied via
    # next.config.js rewrites), so Flask never serves any frontend asset or
    # page itself.
    from app.auth import install_auth, AUTH_MODE
    install_auth(flask_app, extra_public_prefixes=(
        '/healthz', '/readyz',
        '/connections/',
        # GET /api/agents/ping is the pre-login reachability probe (see
        # app/routes/agents.py). Whitelisting the exact path here only
        # bypasses the GLOBAL gate — POST on the same path stays protected
        # by its own explicit @auth_required decorator.
        '/api/agents/ping',
    ))
    log.info("[STARTUP] Auth subsystem loaded - mode=%s", AUTH_MODE)

    # ---- Database boot ----------------------------------------------
    # Neo4j (graph store). See app/database/__init__.py for exactly what
    # this trims relative to frd-generator's bootstrap_neo4j().
    from app.database import bootstrap_neo4j
    bootstrap_neo4j()

    # Postgres (app/postgres/) — BA -> Projects -> Workshops application
    # database. Tolerant of being unreachable/unconfigured; every other
    # subsystem keeps working regardless (see app/postgres/__init__.py).
    from app.postgres import bootstrap_postgres
    bootstrap_postgres()

    # ---- Privacy & Guardrails REST surface --------------------------
    # Registered as its own blueprint so a runtime issue elsewhere can't
    # take the privacy panel down.
    from app.routes import guardrails as guardrails_routes
    guardrails_routes.install(flask_app)

    # ---- Source connections (OAuth "Connect <provider>") ------------
    # Per-user OAuth; tokens stored in Key Vault by reference (never in
    # Neo4j/workflow JSON). See app/services/connections_store.py for the
    # in-process registry that replaces frd-generator's Postgres-backed one.
    from app.routes import connections as connections_routes
    connections_routes.install(flask_app)
    log.info("[STARTUP] Source-connection OAuth routes loaded")

    # ---- Agent backbone proof route ----------------------------------
    # /api/agents/ping — proves the whole chain (Next.js -> rewrite ->
    # Flask -> auth -> llm_service) is wired end to end. See
    # app/routes/agents.py.
    from app.routes import agents as agents_routes
    agents_routes.install(flask_app)
    log.info("[STARTUP] Agent backbone routes loaded")

    # ---- User settings: LLM backend picker -----------------------------
    # /api/settings/llm — per-user Bedrock vs Azure OpenAI choice, surfaced
    # in the header avatar menu. See app/routes/settings.py.
    from app.routes import settings as settings_routes
    settings_routes.install(flask_app)
    log.info("[STARTUP] Settings routes loaded")

    # ---- Projects & Workshops ------------------------------------------
    # /api/projects, /api/projects/<id>/workshops, /api/workshops/<id> —
    # the BA -> Projects -> Workshops hierarchy (app/postgres/). A Workshop
    # IS a canvas board; see app/routes/projects.py.
    from app.routes import projects as projects_routes
    projects_routes.install(flask_app)
    log.info("[STARTUP] Projects/Workshops routes loaded")

    # ---- Canvas board persistence (Phase 1) ---------------------------
    # /api/canvas/board — save/load the whole-board JSON the frontend
    # canvas engine autosaves. See app/routes/canvas.py.
    from app.routes import canvas as canvas_routes
    canvas_routes.install(flask_app)
    log.info("[STARTUP] Canvas routes loaded")

    # ---- Integrations (Phase 3/4): Teams + Granola stub ---------------
    from app.routes import integrations as integrations_routes
    integrations_routes.install(flask_app)
    log.info("[STARTUP] Integration routes loaded (teams, granola-stub)")

    # ---- Handoff export (Phase 4, FR-11): DOCX / Markdown -------------
    from app.routes import export as export_routes
    export_routes.install(flask_app)
    log.info("[STARTUP] Export routes loaded")

    # ---- Post-Workshop: backlog / opportunities / MoM / ADO sync ------
    from app.routes import backlog as backlog_routes
    backlog_routes.install(flask_app)
    log.info("[STARTUP] Post-Workshop backlog routes loaded")

    return flask_app
