"""
Backend entrypoint — AI Discovery Canvas.

Run for local development:

    cd backend
    python main.py

For production WSGI servers (gunicorn, waitress, uvicorn-asgi-bridge):

    gunicorn -w 4 -b 0.0.0.0:5101 "app:create_app()"

The factory in `app/__init__.py` is the single source of truth for how
the Flask app is assembled; this file's only job is to import it, run a
lightweight dependency check, and `app.run()` for dev.

ADAPTATION NOTE (ai-discovery-canvas scaffold): frd-generator's main.py
`_check_dependencies()` imported `check_dependencies` from
`app.routes.legacy_routes` (a git-clone / GPT-4.1 / Postgres preflight
tied to the legacy monolith). That module doesn't exist in this project,
so `_check_dependencies()` below is a NEW, much smaller check: it verifies
AWS Bedrock config (region + model id + resolvable AWS credentials) is
present (warns, doesn't abort — LLM calls will just fail with a precise
error until configured) and reports whatever `create_app()` already
discovered about Neo4j reachability during `bootstrap_neo4j()` (via
`neo4j_store.is_ready()`), rather than re-cloning a repo to prove git
works.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python main.py` to import `app.*` without setting PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import create_app                        # noqa: E402
from app.core.config import HOST, PORT, DEBUG      # noqa: E402
from app.core.logging import log                   # noqa: E402
from app.auth import AUTH_MODE                      # noqa: E402


# Build the Flask app at module load so WSGI servers can import it as
# `main:app`. Idempotent under repeated imports (Python caches modules).
app = create_app()


def _check_dependencies() -> list[str]:
    """Minimal boot-time sanity check. Returns a list of human-readable
    problems; an empty list means "looks fine, try to run anyway". Unlike
    frd-generator's legacy check, this never aborts the process — Neo4j
    and AWS Bedrock both degrade gracefully at the route level (health
    checks / precise per-call errors), so a warning here is enough."""
    warnings: list[str] = []

    from app.core import config as app_config
    from app.services.llm_service import check_configured
    llm_errors = check_configured()
    warnings.extend(llm_errors)

    from app.database import neo4j_store
    if not neo4j_store.is_ready():
        warnings.append(
            f'Neo4j is not reachable at {app_config.NEO4J_URI} '
            f'(last error: {neo4j_store.last_failure() or "unknown"}). '
            'Start it with `docker compose up neo4j` from the project root.'
        )

    return warnings


def _print_banner() -> None:
    """ASCII-only banner so every console code page can render it."""
    sep = '=' * 56
    print('')
    print(sep, flush=True)
    print('   AI Discovery Canvas -- Backend', flush=True)
    print(sep, flush=True)
    print('')
    print('Checking dependencies...', flush=True)

    warnings = _check_dependencies()
    if warnings:
        print('\n[!!] Startup warnings (server will still start):', flush=True)
        for w in warnings:
            print(f'  [!!] {w}', flush=True)
    else:
        print('  [ok] AWS Bedrock configured', flush=True)
        print('  [ok] Neo4j reachable', flush=True)

    print('')
    print(f'Server running at  http://{HOST}:{PORT}', flush=True)
    print( '  -> Open the URL above in your browser.', flush=True)
    print(f'  -> Authentication: {AUTH_MODE} (sign-in page at /login)', flush=True)
    print( '  -> Frontend dev  : run the Next.js dev server separately --',
          flush=True)
    print( '                     `cd ../frontend && npm run dev` (Next.js on :3000)',
          flush=True)
    print('')


if __name__ == '__main__':
    _print_banner()
    log.info("[STARTUP] Server boot complete - listening on :%d", PORT)
    # threaded=True so a slow LLM/RAG call on one request doesn't block others.
    app.run(debug=DEBUG, host=HOST, port=PORT, threaded=True)
