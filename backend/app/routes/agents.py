"""
Agent-backbone proof routes — NEW for ai-discovery-canvas (no frd-generator
equivalent; this is the "does the whole chain actually work" smoke test the
project's first milestone is built around).

    GET  /api/agents/ping    PUBLIC.  { ok: true, service: "..." } — lets the
                             frontend confirm the Flask backend is reachable
                             before the user has even logged in.

    POST /api/agents/ping    AUTH-GATED (uses the same session cookie/bearer
                             token every other protected route uses; see
                             app.auth.middleware). Body: { "prompt": "..." }
                             (optional — defaults to a trivial fixed prompt).
                             Calls app.services.llm_service.complete(...) and
                             returns { ok: true, reply: "..." }.

                             On ANY failure (most commonly: Azure OpenAI
                             credentials not configured yet) this returns
                             HTTP 200 with { ok: false, error: "<message>" }
                             rather than a 500 — the point is to let the
                             frontend render a clear "backbone reachable,
                             LLM not configured yet" state instead of
                             crashing on an unhandled server error. The
                             route IS reached, auth DID pass, only the LLM
                             call itself failed — that distinction matters
                             for debugging, so we keep the response 200 and
                             let the JSON body carry the failure.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.auth import auth_required
from app.core.logging import log
from app.services import llm_service

bp = Blueprint('agents', __name__)

_DEFAULT_PROMPT = 'Reply with the single word: ready'


@bp.route('/api/agents/ping', methods=['GET'])
def ping_public():
    """No auth required — a trivial reachability check the frontend can
    call before the user has signed in (e.g. to render a connectivity
    banner on the login page)."""
    return jsonify({'ok': True, 'service': 'ai-discovery-canvas-backend'})


@bp.route('/api/agents/ping', methods=['POST'])
@auth_required
def ping_llm():
    """Auth-gated. Exercises the real backbone: Next.js -> rewrite ->
    Flask -> auth -> llm_service -> Azure OpenAI."""
    body = request.get_json(silent=True) or {}
    prompt = (body.get('prompt') or '').strip() or _DEFAULT_PROMPT
    try:
        reply = llm_service.complete(prompt, tag='[AGENTS/PING]')
        return jsonify({'ok': True, 'reply': reply})
    except Exception as e:
        log.warning('[AGENTS/PING] llm_service.complete failed (%s): %s',
                    e.__class__.__name__, e)
        return jsonify({'ok': False, 'error': str(e)}), 200


def install(app) -> None:
    app.register_blueprint(bp)
