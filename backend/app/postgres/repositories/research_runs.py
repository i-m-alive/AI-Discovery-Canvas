"""Repository for the `research_runs` table."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.postgres.models.research_run import ResearchRun


def create(session: Session, *, run_id: str, workshop_id: int) -> ResearchRun:
    row = ResearchRun(run_id=run_id, workshop_id=workshop_id, status='running', steps=[], insights=[])
    session.add(row)
    session.flush()
    return row


def get(session: Session, run_id: str) -> Optional[ResearchRun]:
    return session.get(ResearchRun, run_id)


def get_latest_for_workshop(session: Session, workshop_id: int) -> Optional[ResearchRun]:
    return session.execute(
        select(ResearchRun).where(ResearchRun.workshop_id == workshop_id)
        .order_by(ResearchRun.created_at.desc()).limit(1)
    ).scalars().first()


def set_counts(session: Session, run_id: str, *, doc_count: Optional[int] = None,
               web_count: Optional[int] = None) -> Optional[ResearchRun]:
    row = session.get(ResearchRun, run_id)
    if row is None:
        return None
    if doc_count is not None:
        row.doc_count = doc_count
    if web_count is not None:
        row.web_count = web_count
    session.flush()
    return row


def append_step(session: Session, run_id: str, step: dict) -> Optional[ResearchRun]:
    row = session.get(ResearchRun, run_id)
    if row is None:
        return None
    # Reassign (not .append()) so SQLAlchemy's change-tracking on the
    # JSONB column actually notices the mutation — mutating the list
    # in place is silently NOT detected as a dirty attribute.
    row.steps = [*row.steps, step]
    session.flush()
    return row


def set_result(session: Session, run_id: str, *, status: str,
               insights: Optional[list] = None, confidence: Optional[int] = None,
               diagram: Optional[dict] = None, next_steps: Optional[list] = None) -> Optional[ResearchRun]:
    row = session.get(ResearchRun, run_id)
    if row is None:
        return None
    row.status = status
    if insights is not None:
        row.insights = insights
    if confidence is not None:
        row.confidence = confidence
    if diagram is not None:
        row.diagram = diagram
    if next_steps is not None:
        row.next_steps = next_steps
    session.flush()
    return row
