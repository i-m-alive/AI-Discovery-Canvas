"""
Projects & Workshops routes.

    POST   /api/projects                    -> { ok, project }
    GET    /api/projects                     -> { ok, projects: [...] }  (current user's own)
    GET    /api/projects/<id>                -> { ok, project }
    PATCH  /api/projects/<id>                -> { ok, project }
    DELETE /api/projects/<id>                -> { ok }   (cascades workshops + their docs)

    POST   /api/projects/<id>/workshops      -> { ok, workshop }
    GET    /api/projects/<id>/workshops      -> { ok, workshops: [...] }

    GET    /api/workshops/<id>               -> { ok, workshop }   (includes project_id/name)
    PATCH  /api/workshops/<id>               -> { ok, workshop }   (rename)
    DELETE /api/workshops/<id>               -> { ok }             (cascades docs; best-effort
                                                                     cleans up its Neo4j entities)

A Workshop IS a canvas board — its board JSON (nodes/edges/artifacts/...)
is read/written via the existing `/api/canvas/board?workshop_id=<id>`
route (see routes/canvas.py), not here.

Ownership: every Project is owned by the Postgres user id resolved from
the caller's email (see app.postgres.services.user_sync). Only the
owner can read/modify their own projects and workshops — this app has
exactly one real tenant today but the model is right from the start.

These routes require Postgres to be configured/reachable — unlike the
rest of the app (Neo4j, RAG, document storage) which degrades
gracefully, Projects/Workshops have no local-file fallback.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask import Blueprint, jsonify, request

from app.auth import auth_required, current_user
from app.core.logging import log, log_exc
from app.postgres import is_configured, session_scope
from app.postgres.repositories import generated_docs as generated_docs_repo
from app.postgres.repositories import prepare_docs as prepare_docs_repo
from app.postgres.repositories import projects as projects_repo
from app.postgres.repositories import workshops as workshops_repo
from app.postgres.services import user_sync

bp = Blueprint('projects', __name__)


# ── helpers ────────────────────────────────────────────────────────────
def _owner_id() -> Optional[int]:
    user = current_user() or {}
    email = user.get('email') or ''
    if not email:
        return None
    return user_sync.resolve_owner_user_id(email, name=user.get('name'))


def _iso(dt: datetime) -> str:
    return dt.isoformat() if dt else ''


def _project_json(p) -> dict:
    return {
        'id': p.id, 'name': p.name, 'description': p.description,
        'created_by_name': p.created_by_name, 'created_by_email': p.created_by_email,
        'created_at': _iso(p.created_at), 'updated_at': _iso(p.updated_at),
    }


def _workshop_json(w, *, project_name: Optional[str] = None) -> dict:
    out = {
        'id': w.id, 'project_id': w.project_id, 'name': w.name,
        'created_by_name': w.created_by_name, 'created_by_email': w.created_by_email,
        'created_at': _iso(w.created_at), 'updated_at': _iso(w.updated_at),
    }
    if project_name is not None:
        out['project_name'] = project_name
    return out


def _db_unavailable():
    return jsonify({'ok': False, 'error': 'the Projects/Workshops database is not '
                                          'configured or unreachable — see backend logs'}), 200


# ── Projects ───────────────────────────────────────────────────────────
@bp.route('/api/projects', methods=['POST'])
@auth_required
def create_project():
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name is required'}), 400
    user = current_user() or {}
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            p = projects_repo.create(
                s, name=name, owner_user_id=owner_id,
                description=(body.get('description') or '').strip() or None,
                created_by_name=user.get('name'), created_by_email=user.get('email'),
            )
            return jsonify({'ok': True, 'project': _project_json(p)})
    except Exception as e:
        log_exc('[PROJECTS/CREATE]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/projects', methods=['GET'])
@auth_required
def list_projects():
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            rows = projects_repo.list_for_owner(s, owner_id)
            return jsonify({'ok': True, 'projects': [_project_json(p) for p in rows]})
    except Exception as e:
        log_exc('[PROJECTS/LIST]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


def _get_owned_project(s, project_id: int, owner_id: int):
    p = projects_repo.get(s, project_id)
    if p is None or p.owner_user_id != owner_id:
        return None
    return p


@bp.route('/api/projects/<int:project_id>', methods=['GET'])
@auth_required
def get_project(project_id):
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            p = _get_owned_project(s, project_id, owner_id)
            if p is None:
                return jsonify({'ok': False, 'error': 'project not found'}), 404
            return jsonify({'ok': True, 'project': _project_json(p)})
    except Exception as e:
        log_exc('[PROJECTS/GET]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/projects/<int:project_id>', methods=['PATCH'])
@auth_required
def update_project(project_id):
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    body = request.get_json(silent=True) or {}
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            if _get_owned_project(s, project_id, owner_id) is None:
                return jsonify({'ok': False, 'error': 'project not found'}), 404
            name = body.get('name')
            description = body.get('description')
            p = projects_repo.update(
                s, project_id,
                name=(name.strip() if isinstance(name, str) and name.strip() else None),
                description=(description.strip() if isinstance(description, str) else None),
            )
            return jsonify({'ok': True, 'project': _project_json(p)})
    except Exception as e:
        log_exc('[PROJECTS/UPDATE]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/projects/<int:project_id>', methods=['DELETE'])
@auth_required
def delete_project(project_id):
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            if _get_owned_project(s, project_id, owner_id) is None:
                return jsonify({'ok': False, 'error': 'project not found'}), 404
            ok = projects_repo.delete(s, project_id)
            return jsonify({'ok': ok})
    except Exception as e:
        log_exc('[PROJECTS/DELETE]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


# ── Workshops ──────────────────────────────────────────────────────────
@bp.route('/api/projects/<int:project_id>/workshops', methods=['POST'])
@auth_required
def create_workshop(project_id):
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip() or 'Untitled Engagement'
    user = current_user() or {}
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            if _get_owned_project(s, project_id, owner_id) is None:
                return jsonify({'ok': False, 'error': 'project not found'}), 404
            w = workshops_repo.create(
                s, project_id=project_id, name=name,
                created_by_name=user.get('name'), created_by_email=user.get('email'),
            )
            return jsonify({'ok': True, 'workshop': _workshop_json(w)})
    except Exception as e:
        log_exc('[WORKSHOPS/CREATE]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/projects/<int:project_id>/workshops', methods=['GET'])
@auth_required
def list_workshops(project_id):
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            if _get_owned_project(s, project_id, owner_id) is None:
                return jsonify({'ok': False, 'error': 'project not found'}), 404
            rows = workshops_repo.list_for_project(s, project_id)
            return jsonify({'ok': True, 'workshops': [_workshop_json(w) for w in rows]})
    except Exception as e:
        log_exc('[WORKSHOPS/LIST]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/workshops/<int:workshop_id>', methods=['GET'])
@auth_required
def get_workshop(workshop_id):
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            w = workshops_repo.get(s, workshop_id)
            if w is None:
                return jsonify({'ok': False, 'error': 'workshop not found'}), 404
            p = _get_owned_project(s, w.project_id, owner_id)
            if p is None:
                return jsonify({'ok': False, 'error': 'workshop not found'}), 404
            return jsonify({'ok': True, 'workshop': _workshop_json(w, project_name=p.name)})
    except Exception as e:
        log_exc('[WORKSHOPS/GET]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/workshops/<int:workshop_id>', methods=['PATCH'])
@auth_required
def update_workshop(workshop_id):
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name is required'}), 400
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            w = workshops_repo.get(s, workshop_id)
            if w is None or _get_owned_project(s, w.project_id, owner_id) is None:
                return jsonify({'ok': False, 'error': 'workshop not found'}), 404
            w = workshops_repo.update_name(s, workshop_id, name)
            return jsonify({'ok': True, 'workshop': _workshop_json(w)})
    except Exception as e:
        log_exc('[WORKSHOPS/UPDATE]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200


@bp.route('/api/workshops/<int:workshop_id>', methods=['DELETE'])
@auth_required
def delete_workshop(workshop_id):
    if not is_configured():
        return _db_unavailable()
    owner_id = _owner_id()
    if owner_id is None:
        return _db_unavailable()
    doc_ids: list[str] = []
    try:
        with session_scope() as s:
            if s is None:
                return _db_unavailable()
            w = workshops_repo.get(s, workshop_id)
            if w is None or _get_owned_project(s, w.project_id, owner_id) is None:
                return jsonify({'ok': False, 'error': 'workshop not found'}), 404
            # Collect doc ids BEFORE the cascade delete removes these rows,
            # so the Neo4j cleanup below (keyed by doc_id) has something to
            # act on.
            doc_ids = [d.doc_id for d in prepare_docs_repo.list_for_workshop(s, workshop_id)]
            doc_ids += [d.doc_id for d in generated_docs_repo.list_for_workshop(s, workshop_id)]
            ok = workshops_repo.delete(s, workshop_id)
    except Exception as e:
        log_exc('[WORKSHOPS/DELETE]', e)
        return jsonify({'ok': False, 'error': str(e)}), 200

    # Best-effort cross-store cleanup — a workshop's Neo4j entities are
    # keyed per-document, not per-workshop, so clean up one doc at a time.
    # Never blocks the delete response on Neo4j being down.
    if doc_ids:
        try:
            from app.services import graph_rag
            for doc_id in doc_ids:
                graph_rag.delete_document(board_id=str(workshop_id), doc_id=doc_id)
        except Exception as e:
            log.info('[WORKSHOPS/DELETE] graph cleanup skipped (%s)', e.__class__.__name__)
    return jsonify({'ok': ok})


def install(app) -> None:
    app.register_blueprint(bp)
