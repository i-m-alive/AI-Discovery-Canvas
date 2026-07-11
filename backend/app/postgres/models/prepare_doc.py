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

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String
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

    __table_args__ = (
        Index('ix_prepare_docs_workshop', 'workshop_id'),
    )
