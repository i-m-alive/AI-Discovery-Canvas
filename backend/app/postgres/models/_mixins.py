"""
Reusable column / type primitives for the application models.

`utc_now_column`/`updated_at_column` inject server-defaulted timestamp
columns. `JSONBColumn` is the native PostgreSQL JSONB type — this app
always runs against real PostgreSQL, so there's no need for a portable
JSON-type shim.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


def utc_now_column() -> Mapped[datetime]:
    """A timezone-aware `created_at` column with a server-side default.

    Using server_default=func.now() rather than a Python-side default
    means inserts that bypass the ORM still get a timestamp."""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


JSONBColumn = JSONB
