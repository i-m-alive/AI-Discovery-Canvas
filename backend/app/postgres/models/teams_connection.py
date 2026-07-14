"""
teams_connections — durable Microsoft Teams/Graph OAuth connection, one
row per BA user.

Fixes the real friction hit repeatedly during development: the previous
Teams connection lived ONLY in an in-process dict (app.services.graph_teams
`_state`), so every backend restart (routine in local dev — DEBUG=false
means no auto-reload) silently dropped the connection and forced a full
interactive Microsoft sign-in again before Teams features worked.

`refresh_token` is captured from the device-code flow's token response
(the `_SCOPES` list already includes `offline_access`, which is exactly
what makes Microsoft issue one) and is what survives a restart —
`app.services.graph_teams._token()` redeems it for a fresh access token
on demand instead of requiring the user to reconnect. The MSAL-bridge
path (`set_token`, used when the frontend's own Microsoft sign-in
already has a Graph-scoped token) has no refresh token to capture — the
browser-side MSAL cache handles its own refresh — so this table is only
ever written to from the device-code flow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import updated_at_column, utc_now_column


class TeamsConnection(Base):
    __tablename__ = 'teams_connections'

    owner_user_id: Mapped[int]           = mapped_column(
        BigInteger, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True,
    )
    refresh_token: Mapped[str]           = mapped_column(Text, nullable=False)
    account:       Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    created_at:    Mapped[datetime]      = utc_now_column()
    updated_at:    Mapped[datetime]      = updated_at_column()
