"""
Flask blueprint mounting the auth endpoints.

    GET  /login                  → static login page (HTML)

    GET  /auth/me                → { mode, user|null }
    GET  /auth/config            → { mode, login_methods, azure_configured }
                                    (used by the login page to pick the UI)

    POST /auth/login/mock        → { user, session }
                                    body: { name, email }
                                    only valid when AUTH_MODE=mock

    POST /auth/login/azure       → { redirect } or { error }
                                    only valid when AUTH_MODE=azure

    GET  /auth/callback/azure    → 302 to /  (sets session cookie)
                                    Microsoft Entra redirect target.

    POST /auth/logout            → { ok: true, redirect?: <azure logout url> }

The cookie name is `navicore_session`, HttpOnly + SameSite=Lax. We don't
set Secure because dev runs on http://localhost — flip Secure on once
the app is behind TLS in prod (read it off `request.is_secure`).
"""

from __future__ import annotations

from flask import (
    Blueprint, g, jsonify, make_response, redirect, request,
)

from app.auth import sessions
from app.auth.config import settings
from app.auth.providers import get_provider
from app.auth.providers.azure_ad import AzureNotConfigured

# Postgres user-sync hook (app/postgres/services/user_sync.py): best-effort,
# upserts a `users` row (login_count/last_login) after every successful
# login so Projects have a stable owner id to resolve later — see the three
# call sites below. Silent no-op if Postgres isn't configured/reachable;
# the login itself has already succeeded by the time this runs.
from app.postgres.services import user_sync


auth_bp = Blueprint('auth', __name__)


# --- helpers --------------------------------------------------------------

_COOKIE_NAME = 'navicore_session'


def _set_session_cookie(resp, token: str, exp: int):
    resp.set_cookie(
        _COOKIE_NAME,
        token,
        httponly=True,
        samesite='Lax',
        secure=request.is_secure,
        # max_age is seconds-from-now, not absolute. Compute from exp.
        max_age=max(60, exp - int(__import__('time').time())),
        path='/',
    )
    return resp


def _clear_session_cookie(resp):
    resp.delete_cookie(_COOKIE_NAME, path='/')
    return resp


def _public_session_view(session_rec: dict) -> dict:
    """What the browser is allowed to see about its own session."""
    return {
        'token':  session_rec.get('token'),
        'exp':    session_rec.get('exp'),
        'user':   session_rec.get('user'),
    }


# --- routes ---------------------------------------------------------------

@auth_bp.route('/login', methods=['GET'])
def login_page():
    # ADAPTATION NOTE (ai-discovery-canvas scaffold): upstream frd-generator
    # served a built Vite/React-Router SPA from frontend/dist here. This
    # project's frontend is a separate Next.js process with its own
    # app/login/page.js — Flask never renders a login page itself. This
    # route is kept only as a harmless fallback for anyone hitting the
    # bare backend URL directly; it just bounces to the real Next.js login
    # page. Nothing in this project's own frontend calls this route.
    return redirect('http://localhost:3000/login', code=302)


@auth_bp.route('/auth/config', methods=['GET'])
def auth_config():
    # Azure is "configured" for SPA-driven login as soon as the
    # frontend's MSAL client knows the tenant + client id. The backend
    # doesn't need AZURE_CLIENT_SECRET because the SPA flow uses PKCE,
    # not the confidential-client code exchange. We still expose the
    # backend's preferred AUTH_MODE so the login page can label its
    # mode badge correctly.
    return jsonify({
        'mode': settings.mode,
        'azure_configured': bool(
            settings.azure_tenant_id and settings.azure_client_id
        ),
        # Both endpoints are kept active regardless of AUTH_MODE so a
        # dev login still works during early Azure rollout. The login
        # page decides what's primary based on `mode` + on whether the
        # frontend's MSAL config is populated.
        'login_methods': ['mock', 'azure'],
    })


@auth_bp.route('/auth/me', methods=['GET'])
def auth_me():
    user = getattr(g, 'current_user', None)
    auth_session = getattr(g, 'auth_session', None)
    if user is not None:
        active_mode = (auth_session or {}).get('active_mode', user.get('active_mode', user.get('assigned_role', 'user')))
        user_data = {**user, 'active_mode': active_mode}
    else:
        user_data = None
    return jsonify({
        'mode': settings.mode,
        'authenticated': user is not None,
        'user': user_data,
    })


@auth_bp.route('/auth/mode', methods=['POST'])
def set_mode():
    user = getattr(g, 'current_user', None)
    auth_session = getattr(g, 'auth_session', None)
    if not user or not auth_session:
        return jsonify({'error': 'unauthorized'}), 401
    assigned_role = user.get('assigned_role', 'user')
    if assigned_role != 'developer':
        return jsonify({'error': 'forbidden', 'message': 'Only developer accounts can switch modes'}), 403
    body = request.get_json(silent=True) or {}
    active_mode = body.get('active_mode', '')
    if active_mode not in ('developer', 'user'):
        return jsonify({'error': 'invalid_mode', 'message': 'active_mode must be "developer" or "user"'}), 400
    jti = auth_session.get('jti')
    ok = sessions.update_mode(jti, active_mode)
    if not ok:
        return jsonify({'error': 'session_not_found'}), 404
    return jsonify({'success': True, 'active_mode': active_mode, 'assigned_role': assigned_role})


@auth_bp.route('/auth/login/mock', methods=['POST'])
def login_mock():
    # Dev login is intentionally available regardless of AUTH_MODE so
    # the login page's "Developer login" fallback works even when the
    # canonical mode is `azure`. It mints a session through the
    # MockAuthProvider directly (not via the factory), which keeps the
    # `auth_provider='mock'` stamp on the cookie -- useful for
    # filtering audit logs by login type.
    from app.auth.providers.mock import MockAuthProvider
    body = request.get_json(silent=True) or {}
    result = MockAuthProvider(settings).login(
        name=body.get('name', ''),
        email=body.get('email', ''),
        role=body.get('role'),
    )
    if 'error' in result:
        return jsonify(result), 400

    user_sync.handle_login(result['user'], auth_provider='mock')

    session_rec = result['session']
    resp = make_response(jsonify({
        'user':    result['user'],
        'session': _public_session_view(session_rec),
    }))
    _set_session_cookie(resp, session_rec['token'], session_rec['exp'])
    return resp


@auth_bp.route('/auth/login/azure', methods=['POST', 'GET'])
def login_azure():
    """Two callable shapes:

    POST with JSON body `{name, email, id_token, home_account_id,
    tenant_id}`  -- the SPA path. MSAL on the React frontend has
    just completed the popup login; we mint a backend session from
    the resulting profile and return it (also setting the cookie).

    POST/GET without a body or with the legacy `{redirect}` request
    -- the auth-code flow placeholder. Kept for back-compat with
    bookmarks that hit this URL directly; today it returns a clear
    message pointing operators at the SPA path.

    Works regardless of AUTH_MODE: with MSAL doing the real OAuth
    handshake on the client, the backend's job is the same in both
    `mode=mock` and `mode=azure`.
    """
    from app.auth.providers.azure_ad import AzureADAuthProvider

    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        # SPA-driven login: MSAL profile delivery.
        if body.get('email') or body.get('id_token'):
            azure = AzureADAuthProvider(settings)
            result = azure.login(
                name=body.get('name', ''),
                email=body.get('email', ''),
                id_token=body.get('id_token'),
                home_account_id=body.get('home_account_id'),
                tenant_id=body.get('tenant_id'),
                role=body.get('role'),
            )
            if 'error' in result:
                return jsonify(result), 400
            user_sync.handle_login(result['user'], auth_provider='azure')
            session_rec = result['session']
            resp = make_response(jsonify({
                'user':    result['user'],
                'session': _public_session_view(session_rec),
            }))
            _set_session_cookie(resp, session_rec['token'], session_rec['exp'])
            return resp

    # No body / GET: nothing to do server-side (the SPA does the OAuth
    # handshake on the client). Return a 400 with a clear message so an
    # operator hitting this URL directly knows what's expected.
    return jsonify({
        'error': 'msal_profile_required',
        'message': ('Send a POST with JSON {name, email, id_token, '
                    'home_account_id, tenant_id} after completing the '
                    'MSAL popup on the SPA. The legacy server-side '
                    'auth-code flow is not used.'),
    }), 400


@auth_bp.route('/auth/callback/azure', methods=['GET'])
def callback_azure():
    if not settings.is_azure:
        return redirect('/login?error=azure_disabled')
    try:
        result = get_provider().handle_callback(
            code=request.args.get('code', ''),
            state=request.args.get('state', ''),
        )
    except (NotImplementedError, AzureNotConfigured) as e:
        return redirect(f'/login?error=azure_not_ready')

    session_rec = result.get('session') or {}
    if not session_rec.get('token'):
        return redirect('/login?error=azure_callback_failed')
    if result.get('user'):
        user_sync.handle_login(result['user'], auth_provider='azure')
    resp = make_response(redirect('/'))
    _set_session_cookie(resp, session_rec['token'], session_rec['exp'])
    return resp


@auth_bp.route('/auth/logout', methods=['POST'])
def logout():
    token = request.cookies.get(_COOKIE_NAME) or ''
    if not token:
        hdr = request.headers.get('Authorization', '')
        if hdr.lower().startswith('bearer '):
            token = hdr.split(None, 1)[1].strip()
    result = get_provider().logout(token) if token else {'ok': True}
    resp = make_response(jsonify(result))
    _clear_session_cookie(resp)
    return resp
