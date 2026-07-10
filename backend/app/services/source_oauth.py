"""
Source-provider OAuth connectors (Authorization Code + PKCE).

Reference provider: GitHub. The shared shape is built so GitLab / Atlassian /
Azure DevOps slot in as more entries in PROVIDERS without touching the routes,
the connection registry, or the resolver.

Contracts:
  * client_id is NON-secret config (env GITHUB_OAUTH_CLIENT_ID).
  * client_secret is read from Key Vault via secret_manager (logical
    GITHUB_OAUTH_CLIENT_SECRET -> KV 'oauth-client-github-secret').
  * NO token value is ever logged. The token JSON returned by exchange_code /
    refresh is handed straight to the callback, which writes it to Key Vault by
    reference (secret_manager.set_secret_by_name) and persists only the
    reference on the connection row.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger('app.source_oauth')


# Per-provider OAuth + clone wiring. auth_kind drives git_auth_env:
#   'bearer' -> Authorization: Bearer <token>   (OAuth access tokens)
#   'basic'  -> Authorization: Basic base64(user:token)  (PAT-style)
PROVIDERS: dict[str, dict] = {
    'github': {
        'authorize':       'https://github.com/login/oauth/authorize',
        'token':           'https://github.com/login/oauth/access_token',
        'user_api':        'https://api.github.com/user',
        'scope':           'repo',                 # private-repo read (OAuth App)
        'client_id_env':   'GITHUB_OAUTH_CLIENT_ID',
        'client_secret':   'GITHUB_OAUTH_CLIENT_SECRET',   # secret_manager logical name
        'auth_kind':       'bearer',
        'clone_username':  'x-access-token',        # GitHub convention for token-as-Basic fallback
        'expires':         False,                   # OAuth App tokens don't expire by default
        'accept':          'application/vnd.github+json',
    },
    'azure_devops': {
        # Microsoft Entra v2.0, multi-tenant (work/school accounts). NOT the
        # User.Read app-login app — this is a SEPARATE confidential app whose
        # only job is delegated Azure DevOps access.
        'authorize':       'https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize',
        'token':           'https://login.microsoftonline.com/organizations/oauth2/v2.0/token',
        # ADO Profile API for a human-readable account label.
        'user_api':        'https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.0',
        # Azure DevOps resource (499b84ac…) + offline_access for a refresh token.
        'scope':           '499b84ac-1321-427f-aa17-267ca6975798/.default offline_access',
        'client_id_env':   'AZURE_DEVOPS_OAUTH_CLIENT_ID',
        'client_secret':   'AZURE_DEVOPS_OAUTH_CLIENT_SECRET',
        'auth_kind':       'bearer',                # ADO git accepts OAuth Bearer
        'clone_username':  'x',                     # ignored for Bearer
        'expires':         True,                    # Entra access tokens ~1h; refresh below
        'refresh_scope':   True,                    # Entra refresh requires the scope param
        'accept':          'application/json',
    },
}


def supported(provider: str) -> bool:
    return (provider or '').lower() in PROVIDERS


def _cfg(provider: str) -> dict:
    return PROVIDERS[(provider or '').lower()]


def client_id(provider: str) -> str:
    return (os.environ.get(_cfg(provider)['client_id_env'], '') or '').strip()


def _client_secret(provider: str) -> str:
    from app.services.secret_manager import get_secret
    return get_secret(_cfg(provider)['client_secret']) or ''


def is_configured(provider: str) -> bool:
    """True when both client_id (config) and client_secret (KV) are present."""
    return bool(supported(provider) and client_id(provider) and _client_secret(provider))


# ── PKCE ──────────────────────────────────────────────────────────────
def new_pkce() -> tuple[str, str]:
    """(verifier, challenge) — S256. Forward-compatible: providers that support
    PKCE (GitLab/Atlassian/Entra) use it; GitHub OAuth Apps ignore it and rely
    on client_secret + state."""
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b'=').decode('ascii')
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return verifier, challenge


# ── flow ──────────────────────────────────────────────────────────────
def build_auth_url(provider: str, *, redirect_uri: str, state: str,
                   code_challenge: str) -> str:
    c = _cfg(provider)
    params = {
        'client_id':     client_id(provider),
        'redirect_uri':  redirect_uri,
        'scope':         c['scope'],
        'state':         state,
        'response_type': 'code',
        'code_challenge':        code_challenge,
        'code_challenge_method': 'S256',
    }
    return c['authorize'] + '?' + urllib.parse.urlencode(params)


def _post_form(url: str, data: dict, *, headers: dict | None = None,
               timeout: int = 20) -> dict:
    body = urllib.parse.urlencode(data).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Accept', 'application/json')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8') or '{}')


def exchange_code(provider: str, *, code: str, redirect_uri: str,
                  code_verifier: str) -> dict:
    """Exchange an auth code for tokens. Returns the token JSON to STORE:
    {access_token, refresh_token?, expires_at?, scope, token_type, provider}.
    Raises on provider error. The token value is NEVER logged."""
    c = _cfg(provider)
    payload = {
        'client_id':     client_id(provider),
        'client_secret': _client_secret(provider),
        'code':          code,
        'redirect_uri':  redirect_uri,
        'code_verifier': code_verifier,
        'grant_type':    'authorization_code',
    }
    resp = _post_form(c['token'], payload)
    if 'access_token' not in resp:
        # Log the ERROR fields only (never any token material).
        raise RuntimeError(f"token exchange failed: {resp.get('error', 'no access_token')}")
    return _normalize_token(provider, resp)


def refresh(provider: str, refresh_token: str) -> dict:
    """Exchange a refresh token for a fresh access token. For providers whose
    access tokens don't expire (GitHub OAuth App) this is unused. Raises on
    failure so the caller marks the connection auth_required."""
    c = _cfg(provider)
    if not c.get('expires'):
        raise RuntimeError(f'{provider} tokens do not expire; refresh not applicable')
    payload = {
        'client_id':     client_id(provider),
        'client_secret': _client_secret(provider),
        'refresh_token': refresh_token,
        'grant_type':    'refresh_token',
    }
    # Microsoft Entra requires the scope on a refresh; GitHub-style providers
    # don't (and ignore it).
    if c.get('refresh_scope'):
        payload['scope'] = c['scope']
    resp = _post_form(c['token'], payload)
    if 'access_token' not in resp:
        raise RuntimeError(f"refresh failed: {resp.get('error', 'no access_token')}")
    return _normalize_token(provider, resp)


def _normalize_token(provider: str, resp: dict) -> dict:
    """Provider token response -> the JSON we persist in Key Vault."""
    out = {
        'provider':      provider,
        'access_token':  resp.get('access_token'),
        'refresh_token': resp.get('refresh_token'),     # may be None (GitHub)
        'token_type':    resp.get('token_type') or 'bearer',
        'scope':         resp.get('scope') or _cfg(provider)['scope'],
    }
    exp = resp.get('expires_in')
    out['expires_at'] = int(time.time()) + int(exp) if exp else None
    return out


def fetch_account_label(provider: str, access_token: str) -> str:
    """Best-effort display label (e.g. the GitHub login). Never raises; never
    logs the token. Returns '' on any failure."""
    c = _cfg(provider)
    api = c.get('user_api')
    if not api:
        return ''
    try:
        req = urllib.request.Request(api)
        req.add_header('Authorization', f'Bearer {access_token}')
        req.add_header('Accept', c.get('accept') or 'application/json')
        req.add_header('User-Agent', 'NaviCORE')
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8') or '{}')
        # GitHub: login/name; Azure DevOps profile: displayName/emailAddress.
        return (data.get('login') or data.get('username') or data.get('name')
                or data.get('displayName') or data.get('emailAddress') or '')[:120]
    except Exception as e:
        log.info('[OAUTH/%s] account label fetch skipped (%s)', provider, e.__class__.__name__)
        return ''


def clone_credential(provider: str, token_json: dict) -> tuple[str, str, str]:
    """Map a stored token JSON to (username, secret, auth_kind) for the clone
    path. auth_kind in {'bearer','basic'} tells git_auth_env which header."""
    c = _cfg(provider)
    return (c.get('clone_username') or 'x',
            token_json.get('access_token') or '',
            c.get('auth_kind') or 'basic')
