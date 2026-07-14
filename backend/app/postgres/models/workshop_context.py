"""
workshop_contexts — the persisted, incrementally-maintained distillation
of a workshop's uploaded source documents (see
app/services/workshop_context.py).

Before this table, every consumer that needed "what do the uploaded
documents say" re-analysed the full corpus from scratch: deepresearch ran
one LLM call per document per run, intent detection re-read everything,
and Copilot had no corpus-level awareness at all. This caches that work:

  doc_summaries  {doc_id: {"name": ..., "summary": ...}} — one distilled
                 summary per uploaded document. A new upload only costs
                 ONE new distillation call; existing entries are reused
                 verbatim, and entries whose document was deleted are
                 dropped without any LLM work.
  intent         the corpus-level "what is this engagement actually
                 about" line (was re-detected per deepresearch run) —
                 recomputed only when the document set changes.
  status         'ready' | 'building' — advisory only; a stale/failed
                 build degrades to consumers re-deriving on demand,
                 never to wrong answers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import JSONBColumn, updated_at_column, utc_now_column


class WorkshopContext(Base):
    __tablename__ = 'workshop_contexts'

    workshop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), primary_key=True,
    )
    intent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    doc_summaries: Mapped[dict] = mapped_column(JSONBColumn, nullable=False, default=dict, server_default='{}')
    # 3 short, corpus-specific questions a BA would plausibly ask next —
    # CopilotPanel's suggestion chips. Generated alongside intent (only
    # when the document set changes), never per chat turn.
    suggestions: Mapped[list] = mapped_column(JSONBColumn, nullable=False, default=list, server_default='[]')
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='ready', server_default='ready')

    created_at: Mapped[datetime] = utc_now_column()
    updated_at: Mapped[datetime] = updated_at_column()
