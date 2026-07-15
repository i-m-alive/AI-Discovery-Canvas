"""
Proposal & Planning routes — phase 4's read/edit surface.

    GET   /api/proposal?workshop_id=   -> {ok, sow, roi, risk, team}
                                          — the latest persisted
                                          proposal_json per agent (each
                                          {doc_id, name, created_at,
                                          ...payload} or null). One read
                                          drives every panel and all
                                          four hero stat tiles.
    PATCH /api/proposal/<doc_id>       body {workshop_id, milestones? |
                                          engagement_weeks? | roles?}
                                          — the dashboard's inline edits
                                          (milestone weeks/titles, team
                                          counts/allocations) before the
                                          proposal goes out. Validated
                                          against the owning agent's
                                          shape; ROI/risk stay
                                          generate-only by design (v1
                                          decision: numbers a client
                                          sees are either generated-and-
                                          grounded or absent, never
                                          hand-tuned silently).

GENERATION has no route here — 'sow'/'roi'/'risk'/'team' run through
the existing POST /api/agents/run like every other generator; Word
export reuses GET /api/agents/document/<id>/word.

All routes auth-gated; functional failures return ok:false with HTTP
200 except missing/invalid ids (400/404) — same convention as
/api/backlog/*.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.auth import auth_required

bp = Blueprint('proposal', __name__)

_PROPOSAL_AGENTS = ('sow', 'roi', 'risk', 'team')


@bp.route('/api/proposal', methods=['GET'])
@auth_required
def get_proposal():
    workshop_id = request.args.get('workshop_id', type=int)
    if not workshop_id:
        return jsonify({'ok': False, 'error': 'workshop_id query param is required'}), 400
    from app.services import generated_docs
    return jsonify({'ok': True, **{
        agent: generated_docs.latest_proposal(workshop_id, agent)
        for agent in _PROPOSAL_AGENTS
    }})


@bp.route('/api/proposal/<doc_id>', methods=['PATCH'])
@auth_required
def update_proposal(doc_id):
    body = request.get_json(silent=True) or {}
    try:
        workshop_id = int(body.get('workshop_id'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'workshop_id is required'}), 400
    from app.services import generated_docs
    doc = generated_docs.get(workshop_id, doc_id)
    if doc is None:
        return jsonify({'ok': False, 'error': 'document not found'}), 404
    agent = doc.get('agent_id')
    current = generated_docs.latest_proposal(workshop_id, agent) or {}
    if agent not in _PROPOSAL_AGENTS or not current:
        return jsonify({'ok': False, 'error': 'this document has no editable proposal payload'}), 404

    # Rebuild the payload from the stored one + the whitelisted edits,
    # re-clamped through the same coercers generation uses — a PATCH can
    # never produce a shape the panels can't render.
    from app.services import agent_catalog as ac
    stored = {k: v for k, v in current.items() if k not in ('doc_id', 'name', 'created_at')}
    if agent == 'sow':
        candidate = dict(stored)
        if 'milestones' in body:
            candidate['milestones'] = body['milestones']
        if 'engagement_weeks' in body:
            candidate['engagement_weeks'] = body['engagement_weeks']
        payload = ac._coerce_sow(candidate)
    elif agent == 'team':
        candidate = dict(stored)
        if 'roles' in body:
            candidate['roles'] = body['roles']
        payload = ac._coerce_team(candidate)
    else:
        return jsonify({'ok': False, 'error': f'{agent} artifacts are generate-only — '
                        'regenerate with a sharper prompt instead of hand-editing'}), 200
    if payload is None:
        return jsonify({'ok': False, 'error': 'the edit left no valid entries — nothing saved'}), 200
    saved = generated_docs.update_proposal_json(workshop_id, doc_id, payload)
    if saved is None:
        return jsonify({'ok': False, 'error': 'could not save (document changed or database unavailable)'}), 200
    return jsonify({'ok': True, 'proposal': saved})


def install(app) -> None:
    app.register_blueprint(bp)
