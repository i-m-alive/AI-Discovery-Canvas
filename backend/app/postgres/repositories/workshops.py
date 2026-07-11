"""Repository for the `workshops` table — a Workshop IS a canvas board."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.postgres.models.workshop import Workshop

_DEFAULT_BOARD: dict = {
    'nodes': [], 'edges': [], 'nid': 0, 'eid': 0,
    'artifacts': [], 'transcript': [], 'board_name': 'Untitled Engagement',
}


def create(session: Session, *, project_id: int, name: str = 'Untitled Engagement',
          created_by_name: Optional[str] = None,
          created_by_email: Optional[str] = None,
          board_data: Optional[dict] = None) -> Workshop:
    row = Workshop(
        project_id=project_id, name=name,
        created_by_name=created_by_name, created_by_email=created_by_email,
        board_data=board_data or {**_DEFAULT_BOARD, 'board_name': name},
    )
    session.add(row)
    session.flush()
    return row


def list_for_project(session: Session, project_id: int) -> list[Workshop]:
    return list(session.execute(
        select(Workshop).where(Workshop.project_id == project_id)
        .order_by(Workshop.created_at.desc())
    ).scalars())


def get(session: Session, workshop_id: int) -> Optional[Workshop]:
    return session.get(Workshop, workshop_id)


def update_name(session: Session, workshop_id: int, name: str) -> Optional[Workshop]:
    row = session.get(Workshop, workshop_id)
    if row is None:
        return None
    row.name = name
    session.flush()
    return row


def save_board(session: Session, workshop_id: int, board_data: dict) -> bool:
    """Persist the whole board blob. Also mirrors `board_data['board_name']`
    into the `name` column so project/workshop list pages don't need to
    load the full JSONB blob just to show a title."""
    row = session.get(Workshop, workshop_id)
    if row is None:
        return False
    row.board_data = board_data
    name = (board_data or {}).get('board_name')
    if isinstance(name, str) and name.strip():
        row.name = name.strip()
    session.flush()
    return True


def delete(session: Session, workshop_id: int) -> bool:
    row = session.get(Workshop, workshop_id)
    if row is None:
        return False
    session.delete(row)
    return True
