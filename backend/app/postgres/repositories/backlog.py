"""Repository for the backlog tables (Post-Workshop Product Backlog —
see services/backlog_service.py for id assignment, tree replacement and
sync-link maintenance, which live above this thin CRUD layer)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.orm import Session

from app.postgres.models.backlog import (
    BacklogEpic, BacklogFeature, BacklogStory, BacklogSyncLink,
)


# ── epics / features / stories ────────────────────────────────────────
def create_epic(session: Session, *, workshop_id: int, epic_id: str, title: str,
                description: str = '', sort: int = 0) -> BacklogEpic:
    row = BacklogEpic(workshop_id=workshop_id, epic_id=epic_id, title=title,
                      description=description, sort=sort)
    session.add(row)
    session.flush()
    return row


def create_feature(session: Session, *, workshop_id: int, epic_row_id: int,
                   feature_id: str, title: str, sort: int = 0) -> BacklogFeature:
    row = BacklogFeature(workshop_id=workshop_id, epic_row_id=epic_row_id,
                         feature_id=feature_id, title=title, sort=sort)
    session.add(row)
    session.flush()
    return row


def create_story(session: Session, *, workshop_id: int, feature_row_id: int,
                 story_id: str, text: str, acceptance_criteria: list | None = None,
                 source_req_ids: list | None = None, sort: int = 0) -> BacklogStory:
    row = BacklogStory(workshop_id=workshop_id, feature_row_id=feature_row_id,
                       story_id=story_id, text=text,
                       acceptance_criteria=acceptance_criteria or [],
                       source_req_ids=source_req_ids or [], sort=sort)
    session.add(row)
    session.flush()
    return row


def list_epics(session: Session, workshop_id: int) -> list[BacklogEpic]:
    return list(session.execute(
        select(BacklogEpic).where(BacklogEpic.workshop_id == workshop_id)
        .order_by(BacklogEpic.sort.asc(), BacklogEpic.id.asc())
    ).scalars())


def list_features(session: Session, workshop_id: int) -> list[BacklogFeature]:
    return list(session.execute(
        select(BacklogFeature).where(BacklogFeature.workshop_id == workshop_id)
        .order_by(BacklogFeature.sort.asc(), BacklogFeature.id.asc())
    ).scalars())


def list_stories(session: Session, workshop_id: int) -> list[BacklogStory]:
    return list(session.execute(
        select(BacklogStory).where(BacklogStory.workshop_id == workshop_id)
        .order_by(BacklogStory.sort.asc(), BacklogStory.id.asc())
    ).scalars())


def get_epic(session: Session, row_id: int) -> Optional[BacklogEpic]:
    return session.get(BacklogEpic, row_id)


def get_feature(session: Session, row_id: int) -> Optional[BacklogFeature]:
    return session.get(BacklogFeature, row_id)


def get_story(session: Session, row_id: int) -> Optional[BacklogStory]:
    return session.get(BacklogStory, row_id)


def delete_tree(session: Session, workshop_id: int) -> None:
    """Wipe the whole backlog tree for a workshop (regenerate semantics).
    Epics cascade to features/stories via FK; sync links are polymorphic
    (no FK) so they're deleted explicitly."""
    session.execute(sa_delete(BacklogSyncLink).where(BacklogSyncLink.workshop_id == workshop_id))
    session.execute(sa_delete(BacklogEpic).where(BacklogEpic.workshop_id == workshop_id))
    session.flush()


# ── sync links ────────────────────────────────────────────────────────
def list_sync_links(session: Session, workshop_id: int,
                    provider: str = 'azure_devops') -> list[BacklogSyncLink]:
    return list(session.execute(
        select(BacklogSyncLink).where(BacklogSyncLink.workshop_id == workshop_id,
                                      BacklogSyncLink.provider == provider)
    ).scalars())


def upsert_sync_link(session: Session, *, workshop_id: int, item_type: str,
                     item_row_id: int, provider: str, external_id: str,
                     external_url: str | None, content_hash: str) -> BacklogSyncLink:
    row = session.execute(
        select(BacklogSyncLink).where(
            BacklogSyncLink.workshop_id == workshop_id,
            BacklogSyncLink.item_type == item_type,
            BacklogSyncLink.item_row_id == item_row_id,
            BacklogSyncLink.provider == provider,
        )
    ).scalar_one_or_none()
    if row is None:
        row = BacklogSyncLink(workshop_id=workshop_id, item_type=item_type,
                              item_row_id=item_row_id, provider=provider,
                              external_id=external_id, external_url=external_url,
                              content_hash=content_hash)
        session.add(row)
    else:
        row.external_id = external_id
        row.external_url = external_url
        row.content_hash = content_hash
        row.last_synced_at = func.now()
    session.flush()
    return row


def delete_sync_links_for_items(session: Session, workshop_id: int,
                                item_type: str, item_row_ids: list[int]) -> None:
    if not item_row_ids:
        return
    session.execute(sa_delete(BacklogSyncLink).where(
        BacklogSyncLink.workshop_id == workshop_id,
        BacklogSyncLink.item_type == item_type,
        BacklogSyncLink.item_row_id.in_(item_row_ids)))
    session.flush()
