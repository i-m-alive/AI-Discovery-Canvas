"""
AzureADAuthProvider — Microsoft Entra ID (formerly Azure AD).

The SPA-driven flow:

    1. The React frontend runs MSAL's `loginPopup()` against the
       configured tenant. MSAL handles the OAuth2 + PKCE handshake
       end-to-end on the client.
    2. On success MSAL returns `{ account, idToken, accessToken, ... }`.
    3. The frontend POSTs `{ name, email, id_token, home_account_id,
       tenant_id }` to `/auth/login/azure` (see `routes.py`).
    4. This provider's `login()` accepts that profile, mints a server
       session, and returns `{ user, session }` -- identical in shape
       to MockAuthProvider's output. The frontend then stores the
       resulting cookie and proceeds into the app.

Why we don't need a Confidential Client / `AZURE_CLIENT_SECRET`:

    The SPA registration uses public-client + PKCE. There is no
    server-side authorization-code exchange to do; MSAL on the
    browser already exchanged the code for tokens. The backend's
    job is just to (eventually) validate the `id_token` signature
    against Microsoft's JWKS and trust the resulting claims.

Production hardening checklist (TODO before going live publicly):

    [ ] Validate the id_token signature with `python-jose` against
        Microsoft's published JWKS for the tenant. The frontend can
        be tampered with; never trust an unvalidated id_token.
    [ ] Pin `aud` to AZURE_CLIENT_ID and `iss` to
        `https://login.microsoftonline.com/<tenant>/v2.0`.
    [ ] Use `oid` (object-id) -- not `email` -- as the stable user
        identifier; email can change.
    [ ] Cache the JWKS (10-minute TTL) so we don't hammer Microsoft
        on every login.

For dev / proof-of-concept the provider currently trusts the
MSAL-issued profile as-is; flipping the validation on is a localised
edit in this file that doesn't touch the routes or the frontend.

Reference endpoints (kept here for the implementer who picks up the
TODO above):

    Authorize :  {authority}/oauth2/v2.0/authorize
    Token     :  {authority}/oauth2/v2.0/token
    Logout    :  {authority}/oauth2/v2.0/logout
                 ?post_logout_redirect_uri=<app url>
    JWKS      :  {authority}/discovery/v2.0/keys
    Default scopes: openid profile email User.Read
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any, Optional

from app.auth import sessions
from app.auth.config import AuthSettings
from app.auth.providers.base import AuthProvider

log = logging.getLogger('app.auth.azure')


def _extract_roles_from_id_token(id_token: str) -> list:
    """Decode id_token payload (no signature check) to get the roles claim.
    Returns a list of role strings, or empty list on any failure."""
    if not id_token:
        return []
    try:
        parts = id_token.split('.')
        if len(parts) != 3:
            return []
        import base64, json as _json
        pad = '=' * (-len(parts[1]) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode('utf-8'))
        roles = payload.get('roles', [])
        return roles if isinstance(roles, list) else []
    except Exception:
        return []


class AzureNotConfigured(RuntimeError):
    """Raised when an Azure login is attempted without the required
    AZURE_* environment variables present."""


class AzureADAuthProvider(AuthProvider):
    name = 'azure'

    def __init__(self, settings: AuthSettings):
        self.settings = settings
        self._lock = threading.RLock()

    # -- public surface ----------------------------------------------------

    def login(self, *, name: str = '', email: str = '',
              id_token: Optional[str] = None,
              home_account_id: Optional[str] = None,
              tenant_id: Optional[str] = None,
              role: Optional[str] = None,
              **_: Any) -> dict:
        """Mint a server session from an MSAL-issued profile.

        Called by /auth/login/azure POST after the SPA completes the
        Microsoft popup. The arguments mirror the MSAL account object
        with the id_token forwarded for future signature validation.
        """
        name = (name or '').strip()
        email = (email or '').strip().lower()

        # ── Verify the id_token BEFORE trusting any claim ───────────────
        # The browser is untrusted; the per-tenant credential isolation
        # depends on a trustworthy `tid`. Validate signature/issuer/audience/
        # expiry against Microsoft's JWKS, then take identity from the
        # VERIFIED claims — not the client-supplied request body.
        verified: dict = {}
        if self.settings.azure_validate_tokens:
            from app.auth.token_validation import validate_azure_id_token, TokenInvalid
            try:
                verified = validate_azure_id_token(
                    id_token,
                    client_id=self.settings.azure_client_id,
                    tenant_id=self.settings.azure_tenant_id,
                    allowed_tenants=self.settings.azure_allowed_tenants,
                )
            except TokenInvalid as e:
                log.warning('[AUTH/AZURE] id_token rejected: %s', e)
                return {
                    'error': 'invalid_token',
                    'message': 'Microsoft sign-in could not be verified. Please sign in again.',
                }
            # Trust ONLY verified claims for identity + tenant.
            email = (verified.get('preferred_username') or verified.get('email')
                     or verified.get('upn') or email or '').strip().lower()
            name = (verified.get('name') or name or '').strip()
            tenant_id = verified.get('tid') or ''
            home_account_id = (f"{verified.get('oid')}.{verified.get('tid')}"
                               if verified.get('oid') and verified.get('tid')
                               else home_account_id)

        if not email:
            return {
                'error': 'email_required',
                'message': 'Microsoft profile must include an email / UPN.',
            }
        if not name:
            # Fall back to email if Microsoft didn't return a display
            # name -- some guest accounts don't have one.
            name = email

        # Roles from the VERIFIED claims when available; else the legacy
        # unverified decode (validation-disabled / mock paths only).
        token_roles = (verified.get('roles') if isinstance(verified.get('roles'), list)
                       else _extract_roles_from_id_token(id_token))
        assigned_role = 'developer' if 'Navicore.Developer' in (token_roles or []) else 'user'

        user = self._mint_user(
            name=name, email=email, role=role,
            home_account_id=home_account_id,
            tenant_id=tenant_id or self.settings.azure_tenant_id,
            assigned_role=assigned_role,
        )

        # Stash the id_token server-side (sessions.extras is never
        # echoed to the browser). Useful for downstream Microsoft Graph
        # calls or audit trails. NOT used for re-validation today --
        # the server-side session lifecycle is independent of the
        # Microsoft token TTL.
        extras = {}
        if id_token:
            extras['id_token'] = id_token
        if home_account_id:
            extras['home_account_id'] = home_account_id

        session = sessions.create(
            user=user, auth_provider=self.name, extras=extras,
        )
        return {'user': user, 'session': session}

    def handle_callback(self, *, code: str = '', state: str = '',
                        **_: Any) -> dict:
        """Legacy server-side auth-code flow placeholder.

        The SPA does the OAuth handshake on the client (see `login`
        above), so this is only reachable if someone hits
        `/auth/callback/azure` directly -- which today is a dead path.
        Kept for back-compat with the route map.
        """
        raise NotImplementedError(
            'Server-side auth-code flow is not used; the SPA delivers '
            'the MSAL profile to /auth/login/azure (POST) directly.'
        )

    def logout(self, token: str) -> dict:
        # Local revocation always works -- the MSAL client-side logout
        # is initiated by the React shell separately. We don't return
        # a Microsoft post-logout URL because the React `logout()`
        # helper already calls `msalInstance.logoutPopup()` which
        # handles the AAD-side sign-out.
        rec = sessions.validate(token)
        if rec is not None:
            sessions.revoke(rec['jti'])
        return {'ok': True}

    def validate_token(self, token: str) -> Optional[dict]:
        return sessions.validate(token)

    def get_current_user(self, token: str) -> Optional[dict]:
        rec = self.validate_token(token)
        return rec['user'] if rec else None

    # -- internals ---------------------------------------------------------

    def _mint_user(self, *, name: str, email: str,
                   role: Optional[str],
                   home_account_id: Optional[str] = None,
                   tenant_id: Optional[str] = None,
                   assigned_role: str = 'user') -> dict:
        """Canonical user dict shape matching MockAuthProvider so the
        rest of the app (workflows, generated docs, audit logs)
        doesn't need to special-case azure vs mock."""
        # Prefer Microsoft's stable `home_account_id` (= oid + tenant)
        # as the user identifier; it survives email changes. Fall back
        # to a hash of the email so older sessions still validate.
        stable_key = home_account_id or email
        digest = hashlib.sha1(stable_key.encode('utf-8')).hexdigest()[:12]
        return {
            'id':            f'azure-{digest}',
            'name':          name,
            'email':         email,
            'role':          role or self.settings.default_role,
            'assigned_role': assigned_role,
            'active_mode':   assigned_role,
            'auth_provider': self.name,
            'claims': {
                'home_account_id': home_account_id or '',
                'tenant_id':       tenant_id or '',
            },
        }
