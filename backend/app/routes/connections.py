"""
Source-connection OAuth routes — the user-facing "Connect <provider>" flow.

    POST /connections/<provider>/start      (auth-gated)  -> { authorize_url, connection_id }
    GET  /connections/<provider>/callback   (public)      -> closes popup, postMessage result
    GET  /connections                        (auth-gated)  -> user's connections (non-secret)

Security model:
  * tenant_id + user_id come from the authenticated session (g.current_user).
  * The token is handled ONLY transiently in the callback: exchanged
    server-side, written straight to Key Vault by reference
    (secret_manager.set_secret_by_name), and only the kv_secret_name +
    non-secret metadata land on the connection record. The token is
    NEVER logged, never stored in Neo4j/workflow JSON.
  * PKCE verifier + OAuth state live in the short-TTL in-process credential
    store keyed by state (the connection record's oauth_state is the match).

ADAPTATION NOTE (ai-discovery-canvas scaffold): upstream frd-generator
persisted the connection registry itself in Postgres
(`app.postgres.repositories.source_connections`). This project doesn't carry
the Postgres subsystem, so the registry is `app.services.connections_store`
— an in-process dict with the same method names/shapes (see that module's
docstring). Everything else (OAuth flow, Key Vault token storage) is
unchanged.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import uuid
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

from app.auth.middleware import current_user
from app.services import source_oauth
from app.services import credential_store
from app.services import connections_store as repo

log = logging.getLogger('app.connections')

bp = Blueprint('connections', __name__)

_KV_OK = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-')


def _identity():
    """(tenant_id, user_id) from the session, or (None, None) if anonymous.
    tenant = Entra `tid` claim ('' for single-tenant/dev); user = hashed id."""
    u = current_user()
    if not u:
        return (None, None)
    tenant = ((u.get('claims') or {}).get('tenant_id') or '').strip()
    return (tenant, u.get('id') or '')


def _kv_sanitize(s: str) -> str:
    """Key Vault names allow [0-9A-Za-z-] only. Map anything else to '-'."""
    return ''.join(ch if ch in _KV_OK else '-' for ch in (s or '')) or 'x'


def kv_secret_name(tenant_id: str, user_id: str, provider: str) -> str:
    """src-cred-{tenant}-{user}-{provider} — the reference, never the token.
    tenant '' becomes 'default' so the segment is never empty."""
    return 'src-cred-{}-{}-{}'.format(
        _kv_sanitize(tenant_id or 'default'),
        _kv_sanitize(user_id),
        _kv_sanitize(provider),
    )


def _redirect_uri(provider: str) -> str:
    """The exact callback URI registered with the provider. Override base via
    CONNECTIONS_REDIRECT_BASE (e.g. https://app.example.com); else derive from
    the request (works for localhost dev)."""
    import os
    base = (os.environ.get('CONNECTIONS_REDIRECT_BASE', '') or '').strip().rstrip('/')
    if not base:
        base = request.url_root.rstrip('/')
    return f'{base}/connections/{provider}/callback'


@bp.route('/connections/<provider>/start', methods=['POST'])
def start(provider: str):
    provider = (provider or '').lower()
    tenant, user_id = _identity()
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401          # gate-bypassed prefix; explicit check
    if not source_oauth.supported(provider):
        return jsonify({'error': f'unsupported provider: {provider}'}), 400
    if not source_oauth.is_configured(provider):
        return jsonify({'error': 'provider_not_configured',
                        'message': f'{provider} OAuth app is not configured on this deployment.'}), 503

    cid = 'conn_' + uuid.uuid4().hex
    kvname = kv_secret_name(tenant, user_id, provider)
    state = _secrets.token_urlsafe(24)
    verifier, challenge = source_oauth.new_pkce()
    redirect_uri = _redirect_uri(provider)

    # Persist the pending connection (reuses the (tenant,user,provider) row).
    row = repo.upsert_pending(
        id=cid, tenant_id=tenant or '', user_id=user_id, provider=provider,
        kv_secret_name=kvname, oauth_state=state, scopes=source_oauth.PROVIDERS[provider]['scope'])
    if row is None:
        return jsonify({'error': 'registry_unavailable',
                        'message': 'connection registry is unavailable.'}), 503
    connection_id = row.id

    # Stash the PKCE verifier + redirect (NOT the token) keyed by state.
    credential_store.register('oauth_pkce',
                              fields={'auth_state': state, 'provider': provider,
                                      'connection_id': connection_id,
                                      'redirect_uri': redirect_uri},
                              secrets={'verifier': verifier})

    url = source_oauth.build_auth_url(provider, redirect_uri=redirect_uri,
                                      state=state, code_challenge=challenge)
    log.info('[CONNECTIONS] start provider=%s connection=%s tenant=%s',
             provider, connection_id, (tenant or 'default')[:8])
    return jsonify({'authorize_url': url, 'connection_id': connection_id})


@bp.route('/connections/<provider>/callback', methods=['GET'])
def callback(provider: str):
    provider = (provider or '').lower()
    err = request.args.get('error')
    code = request.args.get('code', '')
    state = request.args.get('state', '')
    if err:
        return _popup_close(provider, '', 'error', f'provider error: {err}')
    rec = credential_store.find_by_state(state, provider='oauth_pkce')
    if not rec or not code:
        return _popup_close(provider, '', 'error', 'invalid or expired state')

    conn = repo.get_by_state(state)
    if conn is None:
        return _popup_close(provider, '', 'error', 'connection not found')
    connection_id = conn.id
    kvname = conn.kv_secret_name

    verifier = (rec.get('secrets') or {}).get('verifier', '')
    redirect_uri = (rec.get('fields') or {}).get('redirect_uri') or _redirect_uri(provider)

    # 1. Exchange the code server-side (client_secret from KV). Token in memory only.
    try:
        token_json = source_oauth.exchange_code(
            provider, code=code, redirect_uri=redirect_uri, code_verifier=verifier)
    except Exception as e:
        log.warning('[CONNECTIONS] exchange failed provider=%s connection=%s (%s)',
                    provider, connection_id, e.__class__.__name__)
        repo.mark_status(connection_id, 'auth_required')
        return _popup_close(provider, connection_id, 'error', 'token exchange failed')

    # 2. Best-effort display label (never logs the token).
    label = source_oauth.fetch_account_label(provider, token_json.get('access_token') or '')

    # 3. WRITE the token to Key Vault BY REFERENCE — the only place it lands.
    import json as _json
    from app.services import secret_manager
    if not secret_manager.set_secret_by_name(kvname, _json.dumps(token_json)):
        repo.mark_status(connection_id, 'auth_required')
        return _popup_close(provider, connection_id, 'error', 'secret store unavailable')

    # 4. Persist only the reference + non-secret metadata.
    exp = token_json.get('expires_at')
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None
    repo.mark_connected(connection_id, account_label=label,
                        scopes=token_json.get('scope'), expires_at=expires_at)
    credential_store.delete(rec['id'])     # consume the one-time PKCE/state scratch

    # Log NON-secret fields only.
    log.info('[CONNECTIONS] connected provider=%s connection=%s account=%s',
             provider, connection_id, label or '<unknown>')
    return _popup_close(provider, connection_id, 'connected', label)


@bp.route('/connections', methods=['GET'])
def list_connections():
    tenant, user_id = _identity()
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401
    rows = repo.list_for_user(tenant or '', user_id) or []
    return jsonify({'connections': rows})


def _popup_close(provider: str, connection_id: str, status: str, detail: str) -> Response:
    """Tiny HTML that hands the result to the opener (the source-node UI) via
    postMessage and closes the popup. Carries NO token — only ids/labels."""
    import json as _json
    payload = _json.dumps({'type': 'navicore-oauth', 'provider': provider,
                           'connection_id': connection_id, 'status': status,
                           'detail': detail})
    html = (
        '<!doctype html><meta charset="utf-8"><body style="font-family:system-ui;'
        'background:#07090d;color:#e6edf3;padding:32px">'
        f'<p>{"Connected" if status == "connected" else "Connection failed"}. '
        'You can close this window.</p><script>'
        f'try{{window.opener&&window.opener.postMessage({payload},"*");}}catch(e){{}}'
        'setTimeout(function(){window.close();},400);</script></body>'
    )
    return Response(html, mimetype='text/html')


def install(app) -> None:
    app.register_blueprint(bp)
