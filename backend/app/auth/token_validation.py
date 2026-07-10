"""
Microsoft Entra ID id_token validation (JWKS / RS256).

The SPA delivers an MSAL-issued id_token to /auth/login/azure. The browser is
untrusted, so before ANY claim is believed — especially `tid`, on which the
per-tenant source-credential isolation depends — the token's signature, issuer,
audience, and expiry are verified against Microsoft's published signing keys.

Checks performed (all must pass):
  * signature — RS256 against the JWKS key selected by the token's `kid`
                (forgery without Microsoft's private key fails here)
  * issuer    — exactly https://login.microsoftonline.com/{tid}/v2.0
  * audience  — exactly the configured AZURE_CLIENT_ID
  * expiry    — `exp` (and `nbf`/`iat`) within a small leeway
  * tenant    — pinned to AZURE_TENANT_ID for a single-tenant app, or to the
                AZURE_ALLOWED_TENANTS allow-list for a multi-tenant app
                (empty allow-list = any tenant that passes the above)

Returns the VERIFIED claims dict; callers must read tid/oid/email/name from the
return value, never from the client-supplied request body.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger('app.auth.token')

_LOGIN_HOST = 'https://login.microsoftonline.com'

# One PyJWKClient per key-tenant. PyJWKClient caches the fetched JWKS itself
# (keyed by kid) so we don't refetch on every login.
_JWKS_CLIENTS: dict = {}
_JWKS_LOCK = threading.Lock()


class TokenInvalid(Exception):
    """id_token failed validation. Message names the reason (never the token)."""


def _jwks_client(key_tenant: str):
    import jwt  # PyJWT
    uri = f'{_LOGIN_HOST}/{key_tenant}/discovery/v2.0/keys'
    with _JWKS_LOCK:
        c = _JWKS_CLIENTS.get(key_tenant)
        if c is None:
            c = jwt.PyJWKClient(uri)
            _JWKS_CLIENTS[key_tenant] = c
        return c


def validate_azure_id_token(id_token: str, *, client_id: str,
                            tenant_id: str = '', allowed_tenants=(),
                            leeway: int = 120) -> dict:
    """Verify an Entra id_token and return its VERIFIED claims, or raise
    TokenInvalid. Never logs the token value."""
    import jwt  # PyJWT

    if not id_token:
        raise TokenInvalid('no id_token supplied')
    if not client_id:
        # Without the audience we cannot bind the token to THIS app — refuse
        # rather than validate against an unknown audience.
        raise TokenInvalid('AZURE_CLIENT_ID not configured; cannot verify audience')

    # 1. Unverified peek ONLY to read `tid` (selects issuer + JWKS endpoint).
    #    The signature check in step 3 binds this — a forged tid cannot be
    #    signed by Microsoft, so it fails there.
    try:
        unverified = jwt.decode(id_token, options={'verify_signature': False})
    except Exception as e:
        raise TokenInvalid(f'malformed token ({e.__class__.__name__})')
    tid = (unverified.get('tid') or '').strip()
    if not tid:
        raise TokenInvalid('token missing tid claim')

    # Single-tenant app pins the tenant; multi-tenant uses the token's tid.
    pinned = (tenant_id or '').strip()
    if pinned and pinned.lower() not in ('common', 'organizations', 'consumers'):
        if tid != pinned:
            raise TokenInvalid(f'tenant {tid} rejected (app pinned to {pinned})')
        key_tenant = pinned
    else:
        key_tenant = tid
    expected_issuer = f'{_LOGIN_HOST}/{key_tenant}/v2.0'

    # 2. Signing key by kid (fetched + cached from the tenant JWKS).
    try:
        signing_key = _jwks_client(key_tenant).get_signing_key_from_jwt(id_token)
    except Exception as e:
        raise TokenInvalid(f'no usable signing key ({e.__class__.__name__})')

    # 3. Verify signature + standard claims. PyJWT raises on any failure.
    try:
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=['RS256'],            # reject 'alg: none' / HS* confusion
            audience=client_id,
            issuer=expected_issuer,
            leeway=leeway,
            options={'require': ['exp', 'iat', 'iss', 'aud']},
        )
    except Exception as e:
        raise TokenInvalid(f'{e.__class__.__name__}: {e}')

    # 4. Multi-tenant allow-list (when configured).
    if allowed_tenants and tid not in tuple(allowed_tenants):
        raise TokenInvalid(f'tenant {tid} not in allow-list')

    return claims
