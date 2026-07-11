"""
projects — a BA's client engagement, owning one or more Workshops.

Unlike frd-generator's `projects_metadata` (a Postgres MIRROR of a Neo4j
project node), a Project here has no graph-of-record counterpart — this
table IS the record. `owner_user_id` is a real FK (not just a
denormalized email string) because "list my projects" is a first-class
query this app needs from day one.

Indexes
~~~~~~~
    owner_user_id   per-BA project list
    created_at      recency listings
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import updated_at_column, utc_now_column


class Project(Base):
    __tablename__ = 'projects'

    id:               Mapped[int]           = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name:             Mapped[str]           = mapped_column(String(255), nullable=False)
    description:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    owner_user_id:    Mapped[int]           = mapped_column(BigInteger, ForeignKey('users.id'), nullable=False)
    created_by_name:  Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_by_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    created_at:       Mapped[datetime]      = utc_now_column()
    updated_at:       Mapped[datetime]      = updated_at_column()

    __table_args__ = (
        Index('ix_projects_owner', 'owner_user_id'),
        Index('ix_projects_created_at', 'created_at'),
    )

    def __repr__(self) -> str:    # pragma: no cover
        return f'<Project id={self.id} name={self.name!r}>'
