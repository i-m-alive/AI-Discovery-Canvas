"""Repository for the `teams_connections` table."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.postgres.models.teams_connection import TeamsConnection


def upsert(session: Session, *, owner_user_id: int, refresh_token: str,
          account: Optional[str] = None) -> TeamsConnection:
    stmt = insert(TeamsConnection).values(
        owner_user_id=owner_user_id, refresh_token=refresh_token, account=account,
    ).on_conflict_do_update(
        index_elements=[TeamsConnection.owner_user_id],
        set_={'refresh_token': refresh_token, 'account': account},
    ).returning(TeamsConnection)
    return session.execute(stmt).scalar_one()


def get(session: Session, owner_user_id: int) -> Optional[TeamsConnection]:
    return session.get(TeamsConnection, owner_user_id)


def delete(session: Session, owner_user_id: int) -> bool:
    row = session.get(TeamsConnection, owner_user_id)
    if row is None:
        return False
    session.delete(row)
    return True
