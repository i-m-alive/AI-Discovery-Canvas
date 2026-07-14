"""Repository for the `copilot_threads` table (per workshop+user)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.postgres.models.copilot_thread import CopilotThread


def get(session: Session, workshop_id: int, user_key: str = '') -> Optional[CopilotThread]:
    return session.get(CopilotThread, (workshop_id, user_key))


def append(session: Session, workshop_id: int, user_key: str, message: dict) -> CopilotThread:
    row = session.get(CopilotThread, (workshop_id, user_key))
    if row is None:
        row = CopilotThread(workshop_id=workshop_id, user_key=user_key, messages=[message])
        session.add(row)
    else:
        # Reassign (not .append()) so SQLAlchemy's change-tracking on the
        # JSONB column actually notices the mutation.
        row.messages = [*row.messages, message]
    session.flush()
    return row


def set_summary(session: Session, workshop_id: int, user_key: str,
                summary: str, summary_count: int) -> Optional[CopilotThread]:
    row = session.get(CopilotThread, (workshop_id, user_key))
    if row is None:
        return None
    row.summary = summary
    row.summary_count = summary_count
    session.flush()
    return row


def clear(session: Session, workshop_id: int, user_key: str = '') -> bool:
    row = session.get(CopilotThread, (workshop_id, user_key))
    if row is None:
        return False
    session.delete(row)
    return True
