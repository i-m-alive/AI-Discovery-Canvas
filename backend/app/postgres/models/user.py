"""
users — persistent record of every authenticated identity.

After a successful Microsoft (Entra ID) or mock login, the auth layer
upserts a row here so the application has a durable, stable user id to
hang Project ownership off of — the JWT/session's own `user['id']`
isn't stable across auth providers (it's a home_account_id for Azure,
an ad-hoc string for mock), but `email` is. `microsoft_id` is the stable
home_account_id (oid + tenant) when Entra ID issued the login — null
for mock-mode logins.

Indexes
~~~~~~~
    email          UNIQUE          natural login key, used by every join
    microsoft_id   UNIQUE (sparse) Entra ID stable identifier
    last_login                     activity-window reports
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import utc_now_column


class User(Base):
    __tablename__ = 'users'

    id:            Mapped[int]            = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    microsoft_id:  Mapped[Optional[str]]  = mapped_column(String(128), unique=True, nullable=True)
    name:          Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)
    email:         Mapped[str]            = mapped_column(String(320), unique=True, nullable=False)

    first_login:   Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_login:    Mapped[Optional[datetime]] = mapped_column(nullable=True)
    login_count:   Mapped[int]            = mapped_column(Integer, nullable=False, default=0, server_default='0')

    # 'azure' | 'mock' — which provider minted the most recent session.
    auth_provider: Mapped[Optional[str]]  = mapped_column(String(32), nullable=True)
    role:          Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)
    tenant_id:     Mapped[Optional[str]]  = mapped_column(String(128), nullable=True)

    created_at:    Mapped[datetime]       = utc_now_column()

    __table_args__ = (
        Index('ix_users_last_login', 'last_login'),
    )

    def __repr__(self) -> str:    # pragma: no cover — debug aid only
        return f'<User id={self.id} email={self.email!r}>'
