"""Repository for the `workshop_contexts` table."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.postgres.models.workshop_context import WorkshopContext


def get(session: Session, workshop_id: int) -> Optional[WorkshopContext]:
    return session.get(WorkshopContext, workshop_id)


def upsert(session: Session, workshop_id: int, *,
           intent: Optional[str] = None,
           doc_summaries: Optional[dict] = None,
           suggestions: Optional[list] = None,
           status: Optional[str] = None) -> WorkshopContext:
    row = session.get(WorkshopContext, workshop_id)
    if row is None:
        row = WorkshopContext(workshop_id=workshop_id,
                              intent=intent, doc_summaries=doc_summaries or {},
                              suggestions=suggestions or [],
                              status=status or 'ready')
        session.add(row)
    else:
        if intent is not None:
            row.intent = intent
        if doc_summaries is not None:
            # Reassign (not mutate) for JSONB change tracking.
            row.doc_summaries = dict(doc_summaries)
        if suggestions is not None:
            row.suggestions = list(suggestions)
        if status is not None:
            row.status = status
    session.flush()
    return row
