"""
Post-authentication user sync.

Called from `app/auth/routes.py` immediately after a session is minted
(mock and Entra ID login paths both converge through the same helper
here). Deliberately additive: if Postgres is disabled/unreachable, it
logs and returns — the login already succeeded before this runs, so the
user gets in either way.

`owner_user_id` (the Postgres id a Project/Workshop is owned by) is
resolved on demand from `current_user()['email']` at request time (see
`resolve_owner_user_id` and `app/routes/projects.py`) rather than
threaded through the JWT/session record — that keeps this hook fully
decoupled from the session/token shape, and means a session minted
before this subsystem existed still resolves an owner id correctly on
its very next request.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from app.core.logging import log, log_exc
from app.postgres import is_configured, session_scope
from app.postgres.repositories import users as users_repo


def handle_login(user: Mapping[str, Any], *, auth_provider: Optional[str] = None) -> None:
    """Upsert the freshly-authenticated user into the `users` table.
    Silent no-op when Postgres is not configured."""
    if not is_configured():
        return
    if not isinstance(user, Mapping):
        return
    email = (user.get('email') or '').strip().lower()
    if not email:
        return

    name         = (user.get('name') or '').strip() or None
    role         = (user.get('role') or '').strip() or None
    provider     = (auth_provider or user.get('auth_provider') or '').strip().lower() or None
    claims       = user.get('claims') if isinstance(user.get('claims'), Mapping) else {}
    microsoft_id = (claims.get('home_account_id') or '').strip() or None
    tenant_id    = (claims.get('tenant_id') or '').strip() or None

    try:
        with session_scope() as s:
            if s is None:
                return
            row = users_repo.upsert_on_login(
                s, email=email, name=name, microsoft_id=microsoft_id,
                auth_provider=provider, role=role, tenant_id=tenant_id,
            )
            if row is not None:
                log.info('[POSTGRES] user upsert: %s (login_count=%d, provider=%s)',
                         row.email, row.login_count, provider or 'unknown')
    except Exception as e:
        # Login already succeeded — degrade silently.
        log_exc('[POSTGRES/USER_SYNC]', e)


def resolve_owner_user_id(email: str, *, name: Optional[str] = None) -> Optional[int]:
    """Return the Postgres `users.id` for `email`, upserting a row if
    none exists yet (defensive — the login hook should already have
    created it). Returns None if Postgres isn't configured/reachable."""
    email = (email or '').strip().lower()
    if not email or not is_configured():
        return None
    try:
        with session_scope() as s:
            if s is None:
                return None
            row = users_repo.get_by_email(s, email)
            if row is None:
                row = users_repo.upsert_on_login(s, email=email, name=name)
            return row.id if row is not None else None
    except Exception as e:
        log_exc('[POSTGRES/RESOLVE_OWNER]', e)
        return None
