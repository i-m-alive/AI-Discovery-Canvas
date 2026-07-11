"""
Repository for the `users` table.

`upsert_on_login` is the post-authentication entry point: takes the
freshly-minted user dict (same shape both the mock and azure providers
return) and merges it into `users` — incrementing login_count,
refreshing last_login, and persisting first_login on the very first
row. Single atomic round trip via Postgres's native
``INSERT … ON CONFLICT DO UPDATE`` regardless of new-vs-returning.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.postgres.models.user import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upsert_on_login(session: Session, *,
                    email: str,
                    name: Optional[str] = None,
                    microsoft_id: Optional[str] = None,
                    auth_provider: Optional[str] = None,
                    role: Optional[str] = None,
                    tenant_id: Optional[str] = None) -> Optional[User]:
    """Insert-or-update a user row on every successful login. Returns
    the persisted User, or None when email is empty (no anonymous rows)."""
    email = (email or '').strip().lower()
    if not email:
        return None

    now = _now()
    payload = {
        'email':         email,
        'name':          name,
        'microsoft_id':  microsoft_id,
        'auth_provider': auth_provider,
        'role':          role,
        'tenant_id':     tenant_id,
        'first_login':   now,
        'last_login':    now,
        'login_count':   1,
    }

    stmt = insert(User).values(**payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.email],
        set_={
            'name':          stmt.excluded.name,
            'microsoft_id':  func.coalesce(stmt.excluded.microsoft_id, User.microsoft_id),
            'auth_provider': stmt.excluded.auth_provider,
            'role':          stmt.excluded.role,
            'tenant_id':     func.coalesce(stmt.excluded.tenant_id, User.tenant_id),
            'last_login':    stmt.excluded.last_login,
            'login_count':   User.login_count + 1,
        },
    ).returning(User)

    return session.execute(stmt).scalar_one_or_none()


def get_by_email(session: Session, email: str) -> Optional[User]:
    email = (email or '').strip().lower()
    if not email:
        return None
    return session.execute(select(User).where(User.email == email)).scalar_one_or_none()


def get_by_id(session: Session, user_id: int) -> Optional[User]:
    return session.get(User, user_id)
