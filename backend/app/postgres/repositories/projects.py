"""Repository for the `projects` table."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.postgres.models.project import Project


def create(session: Session, *, name: str, owner_user_id: int,
          description: Optional[str] = None,
          created_by_name: Optional[str] = None,
          created_by_email: Optional[str] = None) -> Project:
    row = Project(
        name=name, description=description, owner_user_id=owner_user_id,
        created_by_name=created_by_name, created_by_email=created_by_email,
    )
    session.add(row)
    session.flush()
    return row


def list_for_owner(session: Session, owner_user_id: int) -> list[Project]:
    return list(session.execute(
        select(Project).where(Project.owner_user_id == owner_user_id)
        .order_by(Project.created_at.desc())
    ).scalars())


def get(session: Session, project_id: int) -> Optional[Project]:
    return session.get(Project, project_id)


def update(session: Session, project_id: int, *,
          name: Optional[str] = None,
          description: Optional[str] = None) -> Optional[Project]:
    row = session.get(Project, project_id)
    if row is None:
        return None
    if name is not None:
        row.name = name
    if description is not None:
        row.description = description
    session.flush()
    return row


def delete(session: Session, project_id: int) -> bool:
    row = session.get(Project, project_id)
    if row is None:
        return False
    session.delete(row)
    return True
