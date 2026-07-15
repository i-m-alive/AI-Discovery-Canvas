"""Repository for the `opportunities` table (Post-Workshop Future
Opportunities Register — see services/opportunities_service.py for
opp-id assignment and generation dedup above this thin CRUD layer)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.postgres.models.opportunity import Opportunity


def create(session: Session, *, workshop_id: int, opp_id: str, title: str,
           description: str = '', horizon: str = 'explore', size: str = 'M',
           priority: str = 'med', status: str = 'open',
           source_req_ids: list | None = None, sort: int = 0) -> Opportunity:
    row = Opportunity(workshop_id=workshop_id, opp_id=opp_id, title=title,
                      description=description, horizon=horizon, size=size,
                      priority=priority, status=status,
                      source_req_ids=source_req_ids or [], sort=sort)
    session.add(row)
    session.flush()
    return row


def list_for_workshop(session: Session, workshop_id: int) -> list[Opportunity]:
    return list(session.execute(
        select(Opportunity).where(Opportunity.workshop_id == workshop_id)
        .order_by(Opportunity.sort.asc(), Opportunity.id.asc())
    ).scalars())


def get(session: Session, row_id: int) -> Optional[Opportunity]:
    return session.get(Opportunity, row_id)


def count_for_workshop(session: Session, workshop_id: int) -> int:
    return session.execute(
        select(func.count()).select_from(Opportunity)
        .where(Opportunity.workshop_id == workshop_id)
    ).scalar_one()


def delete(session: Session, row_id: int) -> bool:
    row = session.get(Opportunity, row_id)
    if row is None:
        return False
    session.delete(row)
    return True
