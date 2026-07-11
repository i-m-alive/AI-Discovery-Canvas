"""
Eagerly import every model so it registers on Base.metadata before
`Base.metadata.create_all()` runs (see app/postgres/__init__.py).
"""

from __future__ import annotations

from app.postgres.models.generated_doc import GeneratedDoc  # noqa: F401
from app.postgres.models.prepare_doc import PrepareDoc      # noqa: F401
from app.postgres.models.project import Project             # noqa: F401
from app.postgres.models.user import User                   # noqa: F401
from app.postgres.models.workshop import Workshop            # noqa: F401

__all__ = ['GeneratedDoc', 'PrepareDoc', 'Project', 'User', 'Workshop']
