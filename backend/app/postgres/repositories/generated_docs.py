"""Repository for the `generated_docs` table (Postgres side of
app.services.generated_docs — see that module for the object_store
HTML storage this metadata index points at)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.postgres.models.generated_doc import GeneratedDoc


def create(session: Session, *, doc_id: str, workshop_id: int, name: str,
          agent_id: str = '', chars: int = 0, status: str = 'draft',
          completion_pct: int = 0, author: Optional[str] = None,
          description: Optional[str] = None, category: Optional[str] = None,
          tags: Optional[list] = None, diagram_xml: Optional[str] = None,
          diagram_json: Optional[list] = None, next_steps: Optional[list] = None,
          analysis_json: Optional[dict] = None, capmap_json: Optional[dict] = None) -> GeneratedDoc:
    row = GeneratedDoc(doc_id=doc_id, workshop_id=workshop_id, name=name,
                       agent_id=agent_id, chars=chars, status=status,
                       completion_pct=completion_pct, author=author,
                       description=description, category=category, tags=tags or [],
                       diagram_xml=diagram_xml, diagram_json=diagram_json, next_steps=next_steps,
                       analysis_json=analysis_json, capmap_json=capmap_json)
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
