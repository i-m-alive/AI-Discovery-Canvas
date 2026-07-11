"""Repository for the `prepare_docs` table (Postgres side of
app.services.prepare_docs — see that module for the object_store text
storage this metadata index points at)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.postgres.models.prepare_doc import PrepareDoc


def create(session: Session, *, doc_id: str, workshop_id: int, name: str,
          chars: int, uploaded_by: str = '') -> PrepareDoc:
    row = PrepareDoc(doc_id=doc_id, workshop_id=workshop_id, name=name,
                     chars=chars, uploaded_by=uploaded_by)
    session.add(row)
    session.flush()
    return row


def list_for_workshop(session: Session, workshop_id: int) -> list[PrepareDoc]:
    return list(session.execute(
        select(PrepareDoc).where(PrepareDoc.workshop_id == workshop_id)
        .order_by(PrepareDoc.uploaded_at.asc())
    ).scalars())


def get(session: Session, doc_id: str) -> Optional[PrepareDoc]:
    return session.get(PrepareDoc, doc_id)


def delete(session: Session, doc_id: str) -> bool:
    row = session.get(PrepareDoc, doc_id)
    if row is None:
        return False
    session.delete(row)
    return True
