"""
Application factory — AI Discovery Canvas backend.

    from app import create_app
    app = create_app()

`create_app()` builds the Flask instance, modeled on frd-generator's
factory (`backend/app/__init__.py`) but wiring only the lean subset this
product needs:

  * env-driven config (app.core.config) + structured logging
  * secret manager preload (Azure OpenAI key, Neo4j password, embedding key)
  * auth gate + /auth/* + /login routes (app.auth)
  * Neo4j boot + schema (app.database)
  * Privacy & Guardrails REST surface (app.routes.guardrails)
  * Source connections — the generic OAuth "Connect <provider>" pattern
    (app.routes.connections)
  * Health/readiness probes (app.routes.health)
  * A tiny agent-backbone proof route (app.routes.agents /api/agents/ping)

Deliberately NOT wired (out of scope for this product; see root README):
Postgres, People, Project-Intelligence-Model/Intelligence, Modernize,
Disrupt, the legacy Workflow-Builder routes/pages, Salesforce. None of
those subsystems' source files were copied into this project, so there is
nothing here to import even if we wanted to.

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
    # Resolve every credential once at boot so a misconfigured Key Vault
    # (or env) is visible in the startup log instead of failing on the
    # first request. The preload NEVER raises — dependent subsystems
    # (Neo4j, LLM, embeddings) surface a precise "missing secret" error
    # if the resolution actually failed. Only the secrets THIS project's
    # subsystems actually consume are preloaded (no POSTGRES_PASSWORD /
    # GPT-5.1 key — those belong to subsystems not carried over here).
    from app.services import secret_manager
    try:
        sources = secret_manager.preload([
            'AZURE_OPENAI_API_KEY', 'NEO4J_PASSWORD', 'EMBEDDING_API_KEY',
        ])
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
    ))
    log.info("[STARTUP] Auth subsystem loaded - mode=%s", AUTH_MODE)

    # ---- Database boot ----------------------------------------------
    # Neo4j (graph store). See app/database/__init__.py for exactly what
    # this trims relative to frd-generator's bootstrap_neo4j().
    from app.database import bootstrap_neo4j
    bootstrap_neo4j()

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

    # TODO: mount app.routes.canvas once the canvas API is built.

    return flask_app
