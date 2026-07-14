"""
research_runs — one row per `deepresearch` agent execution, giving the
Pre-Workshop dashboard's "Research Chain" timeline something real to
poll. Previously the deepresearch pipeline (agent_catalog.py) ran its
5 internal stages (intent detection, per-document analysis, web search,
synthesis, diagram extraction) with no persisted trace of any of
them — the caller only ever saw the final body_html blob. This table
captures the step-by-step progress AS the pipeline runs, plus the
structured "insights" (cited findings) and self-reported "confidence"
the synthesis step now returns, so the dashboard can show live progress
instead of a single opaque wait.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import JSONBColumn, updated_at_column, utc_now_column


class ResearchRun(Base):
    __tablename__ = 'research_runs'

    run_id:      Mapped[str]           = mapped_column(String(32), primary_key=True)
    workshop_id: Mapped[int]           = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), nullable=False,
    )
    # Which pipeline this run traces — 'deepresearch' (the Research Chain)
    # or 'analyze' (the Pre-Workshop Analysis progress). Same ledger
    # mechanics, different step vocabularies; each UI polls its own.
    agent_id:    Mapped[str]           = mapped_column(String(32), nullable=False, default='deepresearch', server_default='deepresearch')
    status:      Mapped[str]           = mapped_column(String(20), nullable=False, default='running', server_default='running')  # running|done|failed
    steps:       Mapped[list]          = mapped_column(JSONBColumn, nullable=False, default=list, server_default='[]')
    insights:    Mapped[list]          = mapped_column(JSONBColumn, nullable=False, default=list, server_default='[]')
    confidence:  Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Real counts from the pipeline itself (docs actually analysed, web
    # results actually returned by Tavily) — the dashboard's stat chips
    # read these directly instead of re-deriving an approximate count
    # from how many sources the model happened to cite in its insights.
    doc_count:   Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    web_count:   Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Populated only when the facilitator's own instruction signalled real
    # workflow intent (see agent_catalog._classify_research_request) — a
    # single {"diagrams":[...], "xml": "..."} object and an ordered
    # next-steps checklist, same shape the standalone 'workflow' agent
    # produces, folded into deepresearch's own synthesis call instead of a
    # separate agent run.
    diagram:     Mapped[Optional[dict]] = mapped_column(JSONBColumn, nullable=True)
    next_steps:  Mapped[Optional[list]] = mapped_column(JSONBColumn, nullable=True)

    created_at:  Mapped[datetime]      = utc_now_column()
    updated_at:  Mapped[datetime]      = updated_at_column()

    __table_args__ = (
        Index('ix_research_runs_workshop', 'workshop_id'),
    )
