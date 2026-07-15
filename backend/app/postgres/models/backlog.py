"""
backlog_epics / backlog_features / backlog_stories — the Post-Workshop
Product Backlog tree (Epic → Feature → Story, each story carrying its
own Given-When-Then acceptance criteria).

Rows come from the 'backlog' agent (see agent_catalog.py), which reads
the captured requirements table + capability map and REPLACES the whole
tree per run (regenerate semantics — the tree is a derived artifact,
unlike `requirements` rows which are the captured source of truth and
only ever accumulate). String ids (`EPIC-01`/`FEAT-01`/`US-01`) follow
the requirements table's `REQ-NN` convention: server-assigned, unique
per workshop, shown in the UI.

`backlog_sync_links` tracks what was pushed to an external board
(Azure DevOps today; provider-keyed so Jira can slot in later) — one
row per pushed item per provider, carrying the external work-item id
and a content hash of the fields last sent, so "Push N items" can count
only what's new/changed and re-pushes stay idempotent. Polymorphic
(item_type + item_row_id) so it doesn't need three link tables; rows
are cleaned up by the backlog service when the tree is replaced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.postgres.base import Base
from app.postgres.models._mixins import JSONBColumn, utc_now_column


class BacklogEpic(Base):
    __tablename__ = 'backlog_epics'

    id:          Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workshop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), nullable=False,
    )
    epic_id:     Mapped[str] = mapped_column(String(16), nullable=False)   # 'EPIC-01'
    title:       Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default='', server_default='')
    status:      Mapped[str] = mapped_column(String(20), nullable=False, default='draft', server_default='draft')  # draft|approved
    sort:        Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    created_at:  Mapped[datetime] = utc_now_column()

    __table_args__ = (
        UniqueConstraint('workshop_id', 'epic_id', name='uq_backlog_epics_workshop_epicid'),
        Index('ix_backlog_epics_workshop', 'workshop_id'),
    )


class BacklogFeature(Base):
    __tablename__ = 'backlog_features'

    id:          Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workshop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), nullable=False,
    )
    epic_row_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('backlog_epics.id', ondelete='CASCADE'), nullable=False,
    )
    feature_id:  Mapped[str] = mapped_column(String(16), nullable=False)   # 'FEAT-01'
    title:       Mapped[str] = mapped_column(String(200), nullable=False)
    sort:        Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    created_at:  Mapped[datetime] = utc_now_column()

    __table_args__ = (
        UniqueConstraint('workshop_id', 'feature_id', name='uq_backlog_features_workshop_featid'),
        Index('ix_backlog_features_workshop', 'workshop_id'),
        Index('ix_backlog_features_epic', 'epic_row_id'),
    )


class BacklogStory(Base):
    __tablename__ = 'backlog_stories'

    id:             Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workshop_id:    Mapped[int] = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), nullable=False,
    )
    feature_row_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('backlog_features.id', ondelete='CASCADE'), nullable=False,
    )
    story_id:       Mapped[str] = mapped_column(String(16), nullable=False)   # 'US-01'
    text:           Mapped[str] = mapped_column(Text, nullable=False)         # "As a <persona>, I want ..."
    # [{given, when, then}, ...] — the story's own acceptance criteria,
    # generated WITH the story (not a disconnected bdd draft).
    acceptance_criteria: Mapped[list] = mapped_column(JSONBColumn, nullable=False, default=list, server_default='[]')
    # ['REQ-01', ...] — provenance back to the requirements table.
    source_req_ids: Mapped[list] = mapped_column(JSONBColumn, nullable=False, default=list, server_default='[]')
    status:         Mapped[str] = mapped_column(String(20), nullable=False, default='draft', server_default='draft')  # draft|approved
    sort:           Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    created_at:     Mapped[datetime] = utc_now_column()

    __table_args__ = (
        UniqueConstraint('workshop_id', 'story_id', name='uq_backlog_stories_workshop_storyid'),
        Index('ix_backlog_stories_workshop', 'workshop_id'),
        Index('ix_backlog_stories_feature', 'feature_row_id'),
    )


class BacklogSyncLink(Base):
    __tablename__ = 'backlog_sync_links'

    id:           Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workshop_id:  Mapped[int] = mapped_column(
        BigInteger, ForeignKey('workshops.id', ondelete='CASCADE'), nullable=False,
    )
    item_type:    Mapped[str] = mapped_column(String(10), nullable=False)   # epic|feature|story
    # Polymorphic pointer into the matching backlog_* table — no FK by
    # design; the backlog service deletes links when the tree is replaced.
    item_row_id:  Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider:     Mapped[str] = mapped_column(String(20), nullable=False, default='azure_devops', server_default='azure_devops')
    external_id:  Mapped[str] = mapped_column(String(40), nullable=False)   # ADO work item id
    external_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # sha256 of the fields last pushed — "Push N items" counts only items
    # whose current hash differs (or that have no link yet).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default='', server_default='')
    last_synced_at: Mapped[datetime] = utc_now_column()

    __table_args__ = (
        UniqueConstraint('workshop_id', 'item_type', 'item_row_id', 'provider',
                         name='uq_backlog_sync_links_item'),
        Index('ix_backlog_sync_links_workshop', 'workshop_id'),
    )
