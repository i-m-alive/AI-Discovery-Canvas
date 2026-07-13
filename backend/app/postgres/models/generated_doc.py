"""
generated_docs — metadata for every agent-generated draft that was
persisted so its canvas card gets a real "open document" affordance
(see `app.services.generated_docs`). 1:1 port of that module's JSON
record shape, scoped by `workshop_id` instead of the hardcoded board_id
string 'default'. The HTML body itself still lives in
`app.services.object_store` — only this metadata index moved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import JSONBColumn, utc_now_column


class GeneratedDoc(Base):
    __tablename__ = 'generated_docs'

    doc_id:      Mapped[str]      = mapped_column(String(32), primary_key=True)
    workshop_id: Mapped[int]      = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), nullable=False,
    )
    name:        Mapped[str]      = mapped_column(String(200), nullable=False)
    agent_id:    Mapped[str]      = mapped_column(String(64), nullable=False, default='')
    chars:       Mapped[int]      = mapped_column(Integer, nullable=False, default=0)
    created_at:  Mapped[datetime] = utc_now_column()

    # Metadata for the Pre-Workshop "Artifacts" card grid — added
    # alongside the Pre-Workshop dashboard; previously this table only
    # existed to give a canvas card an "open document" fetch target, with
    # nothing describing its review state, provenance, or completeness.
    status:         Mapped[str]           = mapped_column(String(20), nullable=False, default='draft', server_default='draft')  # draft|in_review|final
    completion_pct: Mapped[int]           = mapped_column(Integer, nullable=False, default=0, server_default='0')
    author:         Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description:    Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    category:       Mapped[Optional[str]] = mapped_column(String(60), nullable=True)  # mirrors AGENT_SPECS[...]['folder']
    tags:           Mapped[list]          = mapped_column(JSONBColumn, nullable=False, default=list, server_default='[]')

    __table_args__ = (
        Index('ix_generated_docs_workshop', 'workshop_id'),
    )
