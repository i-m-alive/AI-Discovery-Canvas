"""
Auth configuration loaded from environment.

Centralising every auth-related env var here means routes / providers /
middleware never call `os.environ.get` directly — there is one place to
audit what the system reads, and one place to flip defaults.

Environment variables
---------------------
AUTH_MODE                 'mock' (default) or 'azure'. Picks which
                          AuthProvider implementation is mounted.

AUTH_JWT_SECRET           HMAC secret used to sign the session JWT.
                          Generated per-process if unset — fine for dev,
                          NOT fine for multi-instance prod (sessions
                          minted by one box would be rejected by another).
                          Set explicitly in any non-toy deployment.

AUTH_SESSION_TTL_SECONDS  How long a session is valid. Default 8h.

AUTH_DEFAULT_ROLE         Role assigned to new mock users. Default 'user'.
                          The full role taxonomy is intentionally loose
                          at this stage — we'll harden it once Azure
                          group-claims are wired.

AZURE_TENANT_ID           Microsoft Entra tenant. Required when
AZURE_CLIENT_ID           AUTH_MODE=azure. Empty in mock mode — the
AZURE_CLIENT_SECRET       Azure provider raises a clear configuration
AZURE_REDIRECT_URI        error rather than silently 500ing.
AZURE_AUTHORITY           Optional override; defaults to
                          https://login.microsoftonline.com/<tenant>.
AZURE_SCOPES              Space-separated OAuth scopes. Defaults to
                          'openid profile email User.Read'.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field


def _env(key: str, default: str = '') -> str:
    v = os.environ.get(key)
    return v.strip() if v is not None else default


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AuthSettings:
    mode: str
    jwt_secret: str
    session_ttl_seconds: int
    default_role: str

    azure_tenant_id: str
    azure_client_id: str
    azure_client_secret: str
    azure_redirect_uri: str
    azure_authority: str
    azure_scopes: tuple = field(default_factory=tuple)
    # Optional tenant allow-list for a MULTI-tenant app (empty = accept any
    # Entra tenant that passes signature/issuer/audience checks).
    azure_allowed_tenants: tuple = field(default_factory=tuple)
    # Verify the id_token signature (JWKS) before trusting ANY claim. Default
    # ON — the multi-tenant credential isolation relies on a trustworthy
    # tenant_id claim. Set AZURE_VALIDATE_TOKENS=0 ONLY as a deliberate,
    # documented emergency escape hatch.
    azure_validate_tokens: bool = True

    @property
    def is_mock(self) -> bool:
        return self.mode == 'mock'

    @property
    def is_azure(self) -> bool:
        return self.mode == 'azure'


def _load() -> AuthSettings:
    mode = _env('AUTH_MODE', 'mock').lower()
    if mode not in ('mock', 'azure'):
        # Don't crash boot — bad value falls back to mock with a warning.
        # The login page surfaces the effective mode so a misconfiguration
        # is visible to the operator.
        mode = 'mock'

    tenant = _env('AZURE_TENANT_ID')
    authority = _env('AZURE_AUTHORITY') or (
        f'https://login.microsoftonline.com/{tenant}' if tenant else ''
    )
    scopes_raw = _env('AZURE_SCOPES', 'openid profile email User.Read')
    scopes = tuple(s for s in scopes_raw.split() if s)

    allowed_raw = _env('AZURE_ALLOWED_TENANTS')
    allowed_tenants = tuple(t for t in allowed_raw.replace(',', ' ').split() if t)
    validate_tokens = _env('AZURE_VALIDATE_TOKENS', '1').lower() not in ('0', 'false', 'no')

    return AuthSettings(
        mode=mode,
        jwt_secret=_env('AUTH_JWT_SECRET') or secrets.token_urlsafe(48),
        session_ttl_seconds=_env_int('AUTH_SESSION_TTL_SECONDS', 8 * 3600),
        default_role=_env('AUTH_DEFAULT_ROLE', 'user'),
        azure_tenant_id=tenant,
        azure_client_id=_env('AZURE_CLIENT_ID'),
        azure_client_secret=_env('AZURE_CLIENT_SECRET'),
        azure_redirect_uri=_env('AZURE_REDIRECT_URI',
                                 'http://localhost:5004/auth/callback/azure'),
        azure_authority=authority,
        azure_scopes=scopes,
        azure_allowed_tenants=allowed_tenants,
        azure_validate_tokens=validate_tokens,
    )


settings: AuthSettings = _load()
AUTH_MODE: str = settings.mode
