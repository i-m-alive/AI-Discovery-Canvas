"""Repository for the `generated_docs` table (Postgres side of
app.services.generated_docs — see that module for the object_store
HTML storage this metadata index points at)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.postgres.models.generated_doc import GeneratedDoc


def create(session: Session, *, doc_id: str, workshop_id: int, name: str,
          agent_id: str = '', chars: int = 0) -> GeneratedDoc:
    row = GeneratedDoc(doc_id=doc_id, workshop_id=workshop_id, name=name,
                       agent_id=agent_id, chars=chars)
    session.add(row)
    session.flush()
    return row


def list_for_workshop(session: Session, workshop_id: int) -> list[GeneratedDoc]:
    return list(session.execute(
        select(GeneratedDoc).where(GeneratedDoc.workshop_id == workshop_id)
        .order_by(GeneratedDoc.created_at.asc())
    ).scalars())


def get(session: Session, doc_id: str) -> Optional[GeneratedDoc]:
    return session.get(GeneratedDoc, doc_id)


def delete(session: Session, doc_id: str) -> bool:
    row = session.get(GeneratedDoc, doc_id)
    if row is None:
        return False
    session.delete(row)
    return True
