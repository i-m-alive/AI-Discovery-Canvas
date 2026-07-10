"""
MockAuthProvider — local dev / CI login.

Accepts (name, email) and mints a session immediately. No password, no
external service, no real identity check. Form validation is minimal
on purpose: any non-empty username + email handle is accepted (the dev
login form ships with placeholders `username` / `xyz`). Real identity
validation happens in AzureADAuthProvider, not here.

The user id is derived from the email (lowercased + hashed prefix) so
the same handle logging in twice gets the same `user.id` — that's what
makes mock-mode useful for testing audit-log / ownership features.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from app.auth import sessions
from app.auth.config import AuthSettings
from app.auth.providers.base import AuthProvider


class MockAuthProvider(AuthProvider):
    name = 'mock'

    def __init__(self, settings: AuthSettings):
        self.settings = settings

    # -- public surface ----------------------------------------------------

    def login(self, *, name: str = '', email: str = '',
              role: Optional[str] = None, **_: Any) -> dict:
        # Mock dev login: accept any non-empty username + email handle.
        # The previous build required an RFC-5322-ish email shape; we
        # relaxed that here so operators can sign in with short stub
        # handles ("xyz", "qa-1", etc.) without the form rejecting them.
        # Real authentication (AzureADAuthProvider) ignores this method
        # entirely -- the AAD id_token carries its own validated email
        # claim -- so this relaxation is scoped to mock mode only.
        name = (name or '').strip()
        email = (email or '').strip().lower()

        if not name:
            return {'error': 'username is required'}
        if not email:
            return {'error': 'email is required'}

        user = self._mint_user(name=name, email=email, role=role)
        session = sessions.create(user=user, auth_provider=self.name)
        return {'user': user, 'session': session}

    def logout(self, token: str) -> dict:
        rec = sessions.validate(token)
        if rec is None:
            return {'ok': True}    # idempotent — already gone
        sessions.revoke(rec['jti'])
        return {'ok': True}

    def validate_token(self, token: str) -> Optional[dict]:
        return sessions.validate(token)

    def get_current_user(self, token: str) -> Optional[dict]:
        rec = self.validate_token(token)
        return rec['user'] if rec else None

    # -- internals ---------------------------------------------------------

    def _mint_user(self, *, name: str, email: str,
                   role: Optional[str]) -> dict:
        # Stable per-email id so re-logins land on the same user across
        # server restarts (useful when audit logs survive in Neo4j but
        # the in-memory session store does not).
        digest = hashlib.sha1(email.encode('utf-8')).hexdigest()[:12]
        assigned_role = 'developer' if role == 'developer' else 'user'
        return {
            'id':            f'mock-{digest}',
            'name':          name,
            'email':         email,
            'role':          role or self.settings.default_role,
            'assigned_role': assigned_role,
            'active_mode':   assigned_role,
            'auth_provider': self.name,
            'claims':        {},
        }
