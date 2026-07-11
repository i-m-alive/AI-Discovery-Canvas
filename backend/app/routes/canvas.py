"""
Canvas board persistence.

The frontend canvas (frontend/app/canvas/canvasApp.js) debounce-saves its
whole board state — nodes, edges, id counters, artifact-library items —
as one JSON document. A board IS a Workshop row's `board_data` JSONB
column (app/postgres/models/workshop.py) — this route no longer owns any
storage of its own, it's a thin read/write over app.postgres.repositories.workshops.

    GET /api/canvas/board?workshop_id=<id>    -> { ok, board|null }
    PUT /api/canvas/board?workshop_id=<id>    body = the board document.
                                               -> { ok: true }

Ownership is enforced the same way routes/projects.py does: a workshop's
board is only readable/writable by its project's owner.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.auth import auth_required, current_user
from app.core.logging import log, log_exc
from app.postgres import is_configured, session_scope
from app.postgres.repositories import projects as projects_repo
from app.postgres.repositories import workshops as workshops_repo
from app.postgres.services import user_sync

bp = Blueprint('canvas', __name__)

# Size guard: a board is a few hundred KB at most in practice; refuse
# anything wildly larger so a bug can't fill the disk.
_MAX_BOARD_BYTES = 5 * 1024 * 1024


def _owned_workshop(s, workshop_id: int, owner_id: int):
    w = workshops_repo.get(s, workshop_id)
    if w is None:
        return None
    p = projects_repo.get(s, w.project_id)
    if p is None or p.owner_user_id != owner_id:
        return None
    return w


@bp.route('/api/canvas/board', methods=['GET'])
@auth_required
def get_board():
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    if not is_configured():
        return jsonify({'ok': False, 'error': 'database not configured'}), 200
    user = current_user() or {}
    owner_id = user_sync.resolve_owner_user_id(user.get('email') or '', name=user.get('name'))
    if owner_id is None:
        return jsonify({'ok': False, 'error': 'database not configured'}), 200
    try:
        with session_scope() as s:
            if s is None:
                return jsonify({'ok': False, 'error': 'database not configured'}), 200
            w = _owned_workshop(s, workshop_id, owner_id)
            if w is None:
                return jsonify({'ok': False, 'error': 'workshop not found'}), 404
            return jsonify({'ok': True, 'board': w.board_data})
    except Exception as e:
        log_exc('[CANVAS/GET]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/canvas/board', methods=['PUT'])
@auth_required
def put_board():
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    raw = request.get_data()
    if len(raw) > _MAX_BOARD_BYTES:
        return jsonify({'ok': False, 'error': 'board too large'}), 413
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'ok': False, 'error': 'body must be a JSON object'}), 400
    if not is_configured():
        return jsonify({'ok': False, 'error': 'database not configured'}), 200
    user = current_user() or {}
    owner_id = user_sync.resolve_owner_user_id(user.get('email') or '', name=user.get('name'))
    if owner_id is None:
        return jsonify({'ok': False, 'error': 'database not configured'}), 200
    try:
        with session_scope() as s:
            if s is None:
                return jsonify({'ok': False, 'error': 'database not configured'}), 200
            if _owned_workshop(s, workshop_id, owner_id) is None:
                return jsonify({'ok': False, 'error': 'workshop not found'}), 404
            workshops_repo.save_board(s, workshop_id, body)
            return jsonify({'ok': True})
    except Exception as e:
        log_exc('[CANVAS/PUT]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


def install(app) -> None:
    app.register_blueprint(bp)
