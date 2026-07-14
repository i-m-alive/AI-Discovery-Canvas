"""
prepare_docs — metadata for every document uploaded to a Workshop's
Prepare zone. 1:1 port of the record shape
`app.services.prepare_docs` used to keep in a flat JSON index file,
now scoped by a real `workshop_id` instead of the hardcoded board_id
string 'default'. The extracted TEXT itself still lives in
`app.services.object_store` (content-addressed, unchanged) — only this
metadata index moved.

`doc_id` keeps its existing external shape (a 16-hex-char id, not a
DB-generated integer) so the `/api/agents/document/<doc_id>` route
contract doesn't change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import utc_now_column


class PrepareDoc(Base):
    __tablename__ = 'prepare_docs'

    doc_id:      Mapped[str]           = mapped_column(String(32), primary_key=True)
    workshop_id: Mapped[int]           = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), nullable=False,
    )
    name:        Mapped[str]           = mapped_column(String(200), nullable=False)
    chars:       Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    uploaded_at: Mapped[datetime]      = utc_now_column()

    # Ingestion lifecycle for the Pre-Workshop "Source Artifacts" status
    # pill — queued (registered, indexing not yet started) -> parsing
    # (vector + graph indexing running) -> ingested | failed. Added
    # alongside the Pre-Workshop dashboard; previously nothing tracked
    # this at all (upload was synchronous, background indexing had no
    # persisted state).
    status:        Mapped[str]           = mapped_column(String(20), nullable=False, default='queued', server_default='queued')
    status_detail: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    indexed_at:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index('ix_prepare_docs_workshop', 'workshop_id'),
    )
