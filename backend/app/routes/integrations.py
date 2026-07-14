"""
Third-party integration routes — Phase 3/4.

Microsoft Teams (see services/graph_teams.py):
    GET  /api/integrations/teams/status        -> {configured, connected, account}
    GET  /api/integrations/teams/meetings       -> {ok, meetings:[{subject,start,
                                                  end,join_url,organizer}]} — calendar
                                                  meetings with a Teams join link.
                                                  Query params: start, end (ISO —
                                                  window navigation), q (subject
                                                  search). `organizer` (email) feeds
                                                  the organizer-fallback lookup below.
    POST /api/integrations/teams/meetings/availability  body {meetings:[{join_url,
                                               organizer?}]} -> {ok, results:
                                               {join_url:{has_transcript}}}
                                               — checked only for the ids the
                                               caller sends (the visible page).
                                               No "has recording" filter: it
                                               would need OnlineMeetingRecording
                                               .Read.All, which Microsoft
                                               classifies Admin-only (verified
                                               live) — dropped rather than
                                               ship a filter this app's users
                                               can never self-approve.
    POST /api/integrations/teams/connect-token  body {access_token, expires_in?}
                                               PRIMARY path — the frontend already
                                               has a Graph-scoped access token from
                                               its OWN MSAL session (same Microsoft
                                               sign-in used to log into the app);
                                               this just verifies + adopts it.
                                               -> {ok, account} | {ok:false, error}
    POST /api/integrations/connect (device-code fallback, for mock-auth
    POST /api/integrations/teams/poll           sessions with no Microsoft account
                                               to reuse) — unchanged from before.
    POST /api/integrations/teams/transcript  body {join_url, organizer?}
                                             -> {ok, lines[], meeting_subject} | {ok:false,error}
                                             `organizer` (email, from the calendar
                                             listing above) enables an app-only
                                             fallback lookup when the signed-in
                                             user was invited, not the organizer —
                                             see services/graph_teams.py's module
                                             docstring for the Azure Portal steps
                                             (client secret + Application
                                             permissions) that fallback needs
                                             before it does anything; until then
                                             it's a no-op and behavior is unchanged.

Meeting scheduling and Outlook mail import were built and then removed:
both need Graph scopes (Calendars.ReadWrite/People.Read, Mail.Read) that
Microsoft classifies as self-consentable, but this app's very first
(bundled) incremental-consent attempt included an Admin-only scope
(OnlineMeetingRecording.Read.All), and Entra ID's consent negotiation for
this user+app pair now shows "Approval required" for any new scope
regardless of how narrowly later requests are split — confirmed live,
not fixable from this app's code without an actual tenant admin action.

Granola (meeting notes) — FLOW ONLY, deliberately not live:
    GET  /api/integrations/granola/status    -> {configured: bool}
    POST /api/integrations/granola/notes     -> {ok:false, error} until GRANOLA_API_KEY
                                                is set (requires their paid Business
                                                plan; no key available today — per
                                                explicit product decision the UI flow
                                                exists but the API call is stubbed).

All routes auth-gated; functional failures return ok:false with HTTP 200
(same convention as /api/agents/*).
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

from app.auth import auth_required, current_user
from app.postgres.services import user_sync
from app.services import graph_teams

bp = Blueprint('integrations', __name__)


def _owner_user_id() -> int:
    """Resolve the Postgres user id for the signed-in user — this is what
    keys graph_teams' per-user token/connection state (and the persisted
    teams_connections row), so two different BAs signed into this app
    never share (or clobber) each other's Teams connection. Falls back to
    0 (a connection that's in-memory-only for this process, matching the
    single-user behaviour this app originally had) when Postgres isn't
    configured/reachable — Teams still works, it just won't survive a
    restart, exactly like before this feature existed."""
    user = current_user() or {}
    owner_id = user_sync.resolve_owner_user_id(user.get('email') or '', name=user.get('name'))
    return owner_id if owner_id is not None else 0


# ── Microsoft Teams ───────────────────────────────────────────────────
@bp.route('/api/integrations/teams/status', methods=['GET'])
@auth_required
def teams_status():
    return jsonify(graph_teams.connection_status(_owner_user_id()))


@bp.route('/api/integrations/teams/connect-token', methods=['POST'])
@auth_required
def teams_connect_token():
    """Primary connection path: adopt a Graph-scoped access token the
    frontend already acquired via its OWN MSAL session (same Microsoft
    sign-in used to log into the app) — automatic, no device-code UI."""
    body = request.get_json(silent=True) or {}
    out = graph_teams.set_token(_owner_user_id(), body.get('access_token') or '',
                                expires_in=body.get('expires_in') or 3300)
    if 'error' in out:
        return jsonify({'ok': False, 'error': out['error']}), 200
    return jsonify({'ok': True, 'account': out.get('account', '')})


@bp.route('/api/integrations/teams/connect', methods=['POST'])
@auth_required
def teams_connect():
    """Fallback device-code flow — only reached when the frontend has no
    Microsoft session to reuse (mock auth) or the automatic path failed."""
    out = graph_teams.start_device_flow(_owner_user_id())
    ok = 'error' not in out
    return jsonify({'ok': ok, **out}), 200


@bp.route('/api/integrations/teams/poll', methods=['POST'])
@auth_required
def teams_poll():
    return jsonify(graph_teams.poll_device_flow(_owner_user_id()))


@bp.route('/api/integrations/teams/meetings', methods=['GET'])
@auth_required
def teams_meetings():
    """List the signed-in user's calendar meetings so the facilitator can
    browse and pick one, instead of pasting a join URL. Optional query
    params: start, end (ISO — window navigation), q (subject search)."""
    out = graph_teams.list_recent_meetings(
        _owner_user_id(), start=request.args.get('start'), end=request.args.get('end'),
        q=request.args.get('q'))
    if 'error' in out:
        return jsonify({'ok': False, 'error': out['error']}), 200
    return jsonify({'ok': True, **out})


@bp.route('/api/integrations/teams/meetings/availability', methods=['POST'])
@auth_required
def teams_meetings_availability():
    """For the "has transcript" filter — checked only for the meetings
    the caller sends (the currently-visible page of results), never a
    whole calendar window at once. body: {meetings: [{join_url,
    organizer?}]}."""
    body = request.get_json(silent=True) or {}
    entries = [m for m in (body.get('meetings') or [])
              if isinstance(m, dict) and isinstance(m.get('join_url'), str)][:50]
    owner_id = _owner_user_id()
    results = {}
    for m in entries:
        out = graph_teams.check_meeting_availability(owner_id, m['join_url'], m.get('organizer'))
        results[m['join_url']] = out
    return jsonify({'ok': True, 'results': results})


@bp.route('/api/integrations/teams/transcript', methods=['POST'])
@auth_required
def teams_transcript():
    body = request.get_json(silent=True) or {}
    out = graph_teams.fetch_transcript(_owner_user_id(), body.get('join_url') or '', body.get('organizer'))
    if 'error' in out:
        return jsonify({'ok': False, 'error': out['error']}), 200
    return jsonify({'ok': True, **out})


# ── Granola (stubbed flow) ────────────────────────────────────────────
@bp.route('/api/integrations/granola/status', methods=['GET'])
@auth_required
def granola_status():
    return jsonify({'configured': bool(os.environ.get('GRANOLA_API_KEY', '').strip())})


@bp.route('/api/integrations/granola/notes', methods=['POST'])
@auth_required
def granola_notes():
    if not os.environ.get('GRANOLA_API_KEY', '').strip():
        return jsonify({'ok': False, 'error':
                        'Granola is not connected — it needs a Granola Business-plan API key '
                        '(GRANOLA_API_KEY in backend/.env). The integration flow is in place; '
                        'add the key when the subscription decision is made.'}), 200
    # Key present but the client is deliberately not implemented yet —
    # Granola's API surface should be wired against a real account, not
    # guessed. This is the single TODO seam for that work.
    return jsonify({'ok': False, 'error':
                    'GRANOLA_API_KEY is set, but the Granola client is not implemented yet — '
                    'wire app/routes/integrations.py::granola_notes against the real account.'}), 200


def install(app) -> None:
    app.register_blueprint(bp)
