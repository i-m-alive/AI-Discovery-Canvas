"""
workshops — one client workshop/meeting within a Project.

A Workshop IS the canvas board that used to be the single hardcoded
`board_id='default'` blob (`routes/canvas.py`'s
{nodes, edges, nid, eid, artifacts, transcript} shape, confirmed against
the real `backend/data/canvas_boards.json` on disk) — `board_data` holds
that exact structure as JSONB. `name` is the display name (mirrors the
blob's own `board_name` field so project/workshop LIST pages don't need
to load the full board just to show a title).

Indexes
~~~~~~~
    project_id   per-project workshop list
    created_at   recency listings
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import JSONBColumn, updated_at_column, utc_now_column


class Workshop(Base):
    __tablename__ = 'workshops'

    id:               Mapped[int]           = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id:       Mapped[int]           = mapped_column(
        BigInteger, ForeignKey('projects.id', ondelete='CASCADE'), nullable=False,
    )
    name:             Mapped[str]           = mapped_column(String(255), nullable=False, default='Untitled Engagement')

    created_by_name:  Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_by_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    # {nodes, edges, nid, eid, artifacts, transcript, board_name} — the
    # exact shape the canvas frontend already saves/loads today.
    board_data:       Mapped[Optional[dict]] = mapped_column(JSONBColumn, nullable=True)

    created_at:       Mapped[datetime]      = utc_now_column()
    updated_at:       Mapped[datetime]      = updated_at_column()

    __table_args__ = (
        Index('ix_workshops_project', 'project_id'),
        Index('ix_workshops_created_at', 'created_at'),
    )

    def __repr__(self) -> str:    # pragma: no cover
        return f'<Workshop id={self.id} name={self.name!r} project_id={self.project_id}>'
