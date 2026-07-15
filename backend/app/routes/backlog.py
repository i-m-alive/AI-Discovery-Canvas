"""
Post-Workshop routes — Product Backlog, Future Opportunities Register,
Minutes of Meeting, Azure DevOps sync.

    GET    /api/backlog?workshop_id=            -> {ok, epics:[nested tree], counts}
    PATCH  /api/backlog/epic/<id>               body {workshop_id, title?, description?, status?}
    PATCH  /api/backlog/feature/<id>            body {workshop_id, title?}
    PATCH  /api/backlog/story/<id>              body {workshop_id, text?, acceptance_criteria?, status?}
    DELETE /api/backlog/{epic|feature|story}/<id>?workshop_id=

    GET    /api/opportunities?workshop_id=      -> {ok, opportunities:[...]}
    PATCH  /api/opportunities/<id>              body {workshop_id, status?/title?/…}
    DELETE /api/opportunities/<id>?workshop_id=

    GET    /api/backlog/mom?workshop_id=        -> {ok, mom: {doc_id, decisions,
                                                   actions, open_questions,
                                                   confidence, created_at} | null}

    GET    /api/backlog/sync/status?workshop_id= -> {ok, configured, missing,
                                                    org_url, project, story_type,
                                                    total, pending, synced, last_synced_at}
    POST   /api/backlog/sync/azure-devops        body {workshop_id}
                                                 -> {ok, results:[...], created,
                                                    updated, skipped, failed}

GENERATION has no route here by design — the 'backlog'/'opportunities'/
'mom' agents run through the existing POST /api/agents/run exactly like
every other generator (the Post-Workshop dashboard's Synthesis Canvas
fires them the same way During-Workshop fires extract_reqs/capmap/brd).

All routes auth-gated; functional failures return ok:false with HTTP
200 except missing/invalid ids (400/404) — same convention as
/api/agents/*.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.auth import auth_required
from app.core.logging import log_exc

bp = Blueprint('backlog', __name__)


def _workshop_id_from_query():
    return request.args.get('workshop_id', type=int)


def _workshop_id_from_body(body: dict):
    try:
        return int(body.get('workshop_id'))
    except (TypeError, ValueError):
        return None


# ── Product Backlog ───────────────────────────────────────────────────
@bp.route('/api/backlog', methods=['GET'])
@auth_required
def get_backlog():
    workshop_id = _workshop_id_from_query()
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import backlog_service
    tree = backlog_service.get_tree(workshop_id)
    return jsonify({'ok': True, **tree})


@bp.route('/api/backlog/epic/<int:row_id>', methods=['PATCH'])
@auth_required
def update_epic(row_id):
    body = request.get_json(silent=True) or {}
    workshop_id = _workshop_id_from_body(body)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id is required'}), 400
    from app.services import backlog_service
    rec = backlog_service.update_epic(workshop_id, row_id, body)
    if not rec:
        return jsonify({'ok': False, 'error': 'epic not found'}), 404
    return jsonify({'ok': True, 'epic': rec})


@bp.route('/api/backlog/feature/<int:row_id>', methods=['PATCH'])
@auth_required
def update_feature(row_id):
    body = request.get_json(silent=True) or {}
    workshop_id = _workshop_id_from_body(body)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id is required'}), 400
    from app.services import backlog_service
    rec = backlog_service.update_feature(workshop_id, row_id, body)
    if not rec:
        return jsonify({'ok': False, 'error': 'feature not found'}), 404
    return jsonify({'ok': True, 'feature': rec})


@bp.route('/api/backlog/story/<int:row_id>', methods=['PATCH'])
@auth_required
def update_story(row_id):
    """Inline story edit before sync — text, acceptance criteria
    ([{given,when,then}]), or approve/draft status."""
    body = request.get_json(silent=True) or {}
    workshop_id = _workshop_id_from_body(body)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id is required'}), 400
    from app.services import backlog_service
    rec = backlog_service.update_story(workshop_id, row_id, body)
    if not rec:
        return jsonify({'ok': False, 'error': 'story not found'}), 404
    return jsonify({'ok': True, 'story': rec})


@bp.route('/api/backlog/<item_type>/<int:row_id>', methods=['DELETE'])
@auth_required
def delete_backlog_item(item_type, row_id):
    if item_type not in ('epic', 'feature', 'story'):
        return jsonify({'ok': False, 'error': f'unknown item type: {item_type}'}), 400
    workshop_id = _workshop_id_from_query()
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import backlog_service
    if not backlog_service.delete_item(workshop_id, item_type, row_id):
        return jsonify({'ok': False, 'error': f'{item_type} not found'}), 404
    return jsonify({'ok': True})


# ── Future Opportunities Register ─────────────────────────────────────
@bp.route('/api/opportunities', methods=['GET'])
@auth_required
def list_opportunities():
    workshop_id = _workshop_id_from_query()
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import opportunities_service
    return jsonify({'ok': True,
                    'opportunities': opportunities_service.list_opportunities(workshop_id)})


@bp.route('/api/opportunities/<int:row_id>', methods=['PATCH'])
@auth_required
def update_opportunity(row_id):
    """Triage (accept / flag for pruning / reject) and inline edits."""
    body = request.get_json(silent=True) or {}
    workshop_id = _workshop_id_from_body(body)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id is required'}), 400
    from app.services import opportunities_service
    rec = opportunities_service.update(workshop_id, row_id, body)
    if not rec:
        return jsonify({'ok': False, 'error': 'opportunity not found'}), 404
    return jsonify({'ok': True, 'opportunity': rec})


@bp.route('/api/opportunities/<int:row_id>', methods=['DELETE'])
@auth_required
def delete_opportunity(row_id):
    workshop_id = _workshop_id_from_query()
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import opportunities_service
    if not opportunities_service.delete(workshop_id, row_id):
        return jsonify({'ok': False, 'error': 'opportunity not found'}), 404
    return jsonify({'ok': True})


# ── Minutes of Meeting ────────────────────────────────────────────────
@bp.route('/api/backlog/mom', methods=['GET'])
@auth_required
def latest_mom():
    """The newest persisted Minutes of Meeting — what the Post-Workshop
    MoM card renders on load. {ok, mom: {...} | null}."""
    workshop_id = _workshop_id_from_query()
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import generated_docs
    return jsonify({'ok': True, 'mom': generated_docs.latest_mom(workshop_id)})


# ── Azure DevOps sync ─────────────────────────────────────────────────
@bp.route('/api/backlog/sync/status', methods=['GET'])
@auth_required
def sync_status():
    """Pending/synced counts + configuration state — drives the "Push N
    items" button. No Azure DevOps calls are made here."""
    workshop_id = _workshop_id_from_query()
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import ado_workitems
    try:
        return jsonify({'ok': True, **ado_workitems.sync_status(workshop_id)})
    except Exception as e:
        log_exc('[BACKLOG/SYNC_STATUS]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/backlog/sync/azure-devops', methods=['POST'])
@auth_required
def sync_azure_devops():
    """One-way push: create/update the backlog tree as ADO work items,
    idempotently (see services/ado_workitems.py)."""
    body = request.get_json(silent=True) or {}
    workshop_id = _workshop_id_from_body(body)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id is required'}), 400
    from app.services import ado_workitems
    try:
        return jsonify(ado_workitems.push_backlog(workshop_id)), 200
    except Exception as e:
        log_exc('[BACKLOG/SYNC_ADO]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


def install(app) -> None:
    app.register_blueprint(bp)
