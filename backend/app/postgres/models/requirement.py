"""
requirements — the During-Workshop "Business Requirements — Live" table.

One row per captured business requirement, scoped by workshop. Rows come
from two places with identical shape: the extract_reqs agent (which
upserts what it mined from imported transcripts + pre-workshop context)
and the facilitator's own "+ Add requirement" (see routes/agents.py
requirements CRUD). `req_id` (REQ-01, REQ-02, ...) is assigned
server-side per workshop and is stable across re-runs — re-extracting
after a new transcript import ADDS new rows, it never renumbers or
duplicates existing ones (see services/requirements.py's dedup).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import utc_now_column


class Requirement(Base):
    __tablename__ = 'requirements'

    id:          Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workshop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), nullable=False,
    )
    req_id:      Mapped[str] = mapped_column(String(16), nullable=False)   # 'REQ-01'
    text:        Mapped[str] = mapped_column(Text, nullable=False)
    category:    Mapped[str] = mapped_column(String(60), nullable=False, default='', server_default='')
    moscow:      Mapped[str] = mapped_column(String(10), nullable=False, default='should', server_default='should')  # must|should|could|wont
    # Source trace — which transcript/document this requirement came from,
    # and the short verbatim line that justifies it (the reference UI's
    # "Source: ..." line under each requirement row).
    source_label:  Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    source_doc_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_quote:  Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status:      Mapped[str] = mapped_column(String(20), nullable=False, default='in_review', server_default='in_review')  # in_review|approved
    sort:        Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    created_at:  Mapped[datetime] = utc_now_column()

    __table_args__ = (
        UniqueConstraint('workshop_id', 'req_id', name='uq_requirements_workshop_reqid'),
        Index('ix_requirements_workshop', 'workshop_id'),
    )
