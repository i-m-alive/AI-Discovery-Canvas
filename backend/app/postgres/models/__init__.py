"""
Eagerly import every model so it registers on Base.metadata before
`Base.metadata.create_all()` runs (see app/postgres/__init__.py).
"""

from __future__ import annotations

from app.postgres.models.copilot_thread import CopilotThread        # noqa: F401
from app.postgres.models.generated_doc import GeneratedDoc          # noqa: F401
from app.postgres.models.prepare_doc import PrepareDoc              # noqa: F401
from app.postgres.models.project import Project                     # noqa: F401
from app.postgres.models.requirement import Requirement             # noqa: F401
from app.postgres.models.research_run import ResearchRun            # noqa: F401
from app.postgres.models.teams_connection import TeamsConnection    # noqa: F401
from app.postgres.models.user import User                           # noqa: F401
from app.postgres.models.workshop import Workshop                   # noqa: F401
from app.postgres.models.workshop_context import WorkshopContext    # noqa: F401

__all__ = ['CopilotThread', 'GeneratedDoc', 'PrepareDoc', 'Project', 'Requirement', 'ResearchRun',
           'TeamsConnection', 'User', 'Workshop', 'WorkshopContext']
