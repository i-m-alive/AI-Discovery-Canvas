"""
copilot_threads — one persisted conversation per (workshop, user) for the
global Copilot panel (see app/services/copilot_thread.py). Originally one
shared thread per workshop; made per-user so each facilitator gets their
own private running conversation (`user_key` is the auth subsystem's
stable user id, falling back to email — see routes/agents.py).

`summary`/`summary_count` back the rolling conversation summary: turns
older than the live replay window get folded into one compact summary by
an LLM call (copilot_thread.maybe_update_summary) instead of being
silently dropped, so long conversations keep continuity without the
prompt growing unbounded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import JSONBColumn, updated_at_column, utc_now_column


class CopilotThread(Base):
    __tablename__ = 'copilot_threads'

    workshop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), primary_key=True,
    )
    user_key: Mapped[str] = mapped_column(String(255), primary_key=True, default='', server_default='')

    # [{role:'user'|'assistant', kind:'text'|'dispatch'|'result', ...}, ...]
    # — same shape CopilotPanel.jsx renders, persisted verbatim so
    # reloading the panel restores exactly what was on screen.
    messages: Mapped[list] = mapped_column(JSONBColumn, nullable=False, default=list, server_default='[]')

    # Rolling summary of messages[0:summary_count] — everything older than
    # the live replay window, compacted once instead of re-sent raw.
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')

    created_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = updated_at_column()
