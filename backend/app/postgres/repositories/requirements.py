"""Repository for the `requirements` table (During-Workshop live
requirements — see services/requirements.py for req-id assignment and
extraction dedup, which live above this thin CRUD layer)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.postgres.models.requirement import Requirement


def create(session: Session, *, workshop_id: int, req_id: str, text: str,
          category: str = '', moscow: str = 'should',
          source_label: Optional[str] = None, source_doc_id: Optional[str] = None,
          source_quote: Optional[str] = None, status: str = 'in_review',
          sort: int = 0) -> Requirement:
    row = Requirement(workshop_id=workshop_id, req_id=req_id, text=text,
                      category=category, moscow=moscow, source_label=source_label,
                      source_doc_id=source_doc_id, source_quote=source_quote,
                      status=status, sort=sort)
    session.add(row)
    session.flush()
    return row


def list_for_workshop(session: Session, workshop_id: int) -> list[Requirement]:
    return list(session.execute(
        select(Requirement).where(Requirement.workshop_id == workshop_id)
        .order_by(Requirement.sort.asc(), Requirement.id.asc())
    ).scalars())


def get(session: Session, row_id: int) -> Optional[Requirement]:
    return session.get(Requirement, row_id)


def count_for_workshop(session: Session, workshop_id: int) -> int:
    return session.execute(
        select(func.count()).select_from(Requirement)
        .where(Requirement.workshop_id == workshop_id)
    ).scalar_one()


def delete(session: Session, row_id: int) -> bool:
    row = session.get(Requirement, row_id)
    if row is None:
        return False
    session.delete(row)
    return True
