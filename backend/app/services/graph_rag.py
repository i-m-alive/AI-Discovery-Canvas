"""
GraphRAG — Neo4j-backed entity/relationship extraction and hybrid retrieval.

Why this exists alongside the plain vector RAG (`app/services/rag/`):
vector search alone answers "what chunks look similar to this query" but
can't do "the SOP mentions Process X, and the regulatory doc constrains
that same Process X" — that needs the two documents' mentions of X linked
in a graph and traversed. This module builds exactly that graph and reads
it back as prompt-ready context.

Pipeline
--------
1. `extract_and_store(board_id, doc_id, name, text)` — ONE LLM call per
   document asks for entities (typed: Process/Requirement/Regulation/
   System/Person/Deadline/Risk) and relationships between them ("X
   RELATES_TO Y because ..."). Written into Neo4j as:

       (:PrepDocument {board_id, doc_id, name})
       (:Entity {board_id, name, type})
       (:PrepDocument)-[:MENTIONS]->(:Entity)
       (:Entity)-[:RELATES_TO {relation}]->(:Entity)

   Entities are deduped per board by (board_id, lower(name)) so the same
   "MedDRA coding" mentioned in two documents is ONE node with two
   MENTIONS edges — that shared node is what makes cross-document
   traversal possible at all.

2. `hybrid_context(board_id, query, seed_names=None)` — the retrieval
   side: seed entities either by name overlap with the query/seed_names,
   or (best-effort) by a simple substring match against extracted entity
   names, then expands 1 hop via RELATES_TO and pulls in which documents
   mention each — formatted as a compact prompt block.

Best-effort everywhere: if Neo4j is unreachable, every function here
degrades to a no-op (empty context / 0 entities) rather than raising —
document upload and agent runs must never fail because the graph is down.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from app.core.logging import log
from app.database import neo4j_client as db
from app.services import llm_service

_ENTITY_TYPES = ('Process', 'Requirement', 'Regulation', 'System', 'Person', 'Deadline', 'Risk')

_SCHEMA_READY = False

_ENTITY_EXTRACT_CHARS = 15000
_ENTITY_FOCUS_FACETS = [
    'named entities: people, systems, processes, regulations, deadlines, risks',
    'relationships and dependencies between entities',
    'the core subject matter and purpose of this document',
]


def _focus_for_entities(text: str) -> str:
    """Chunk + embed + keep the chunks most relevant to entity extraction
    instead of blindly keeping only the document's first 15,000 characters
    — entities described later in a long document are just as real as
    ones on the first page. Falls back to head-truncation when RAG isn't
    enabled or the text is too small for chunking to help."""
    try:
        from app.services import rag
        focused = rag.select_relevant(text, _ENTITY_FOCUS_FACETS,
                                      max_context_chars=_ENTITY_EXTRACT_CHARS,
                                      tag='[GRAPH_RAG/SELECT]')
        if focused:
            return focused
    except Exception as e:
        log.debug('[GRAPH_RAG] select_relevant skipped (%s)', e.__class__.__name__)
    return (text or '')[:_ENTITY_EXTRACT_CHARS]


def _ensure_schema() -> bool:
    """Idempotent constraint setup for the GraphRAG labels, run lazily
    (not from app boot) so a Neo4j outage at import time never affects
    the rest of the app. Returns False (and logs once) if unreachable."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return True
    if not db.is_ready():
        return False
    try:
        with db.session() as s:
            s.run("CREATE CONSTRAINT prepdoc_id IF NOT EXISTS "
                  "FOR (d:PrepDocument) REQUIRE d.doc_id IS UNIQUE").consume()
            s.run("CREATE INDEX entity_board_name IF NOT EXISTS "
                  "FOR (e:Entity) ON (e.board_id, e.name_lower)").consume()
        _SCHEMA_READY = True
        return True
    except Exception as e:
        log.info('[GRAPH_RAG] schema setup skipped (%s)', e.__class__.__name__)
        return False


_EXTRACT_SYSTEM = (
    'Extract entities and relationships from ONE business-analysis source document for a '
    'knowledge graph. Types allowed: Process, Requirement, Regulation, System, Person, '
    'Deadline, Risk. Only extract entities that are actually named/described in the text — '
    'never invent ones. Respond with STRICT JSON only:\n'
    '{"entities": [{"name": "...", "type": "Process|Requirement|Regulation|System|Person|'
    'Deadline|Risk"}], "relationships": [{"from": "...", "to": "...", "relation": "short '
    'verb phrase"}]}\n'
    'Keep names short and consistent (e.g. always "MedDRA coding", not sometimes "MedDRA" '
    'and sometimes "coding of adverse events"). Max 20 entities, max 20 relationships.'
)


def _parse_json_obj(raw: str) -> Optional[dict]:
    if not raw:
        return None
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass
    return None


def extract_and_store(board_id: str, doc_id: str, name: str, text: str) -> int:
    """Extract entities/relationships from one document's text and write
    them into Neo4j. Returns the number of entities written (0 on any
    failure — degrades silently, never raises to the upload path)."""
    if not text or not text.strip():
        return 0
    if not _ensure_schema():
        return 0
    try:
        raw = llm_service.complete(
            f'SOURCE DOCUMENT "{name}":\n\n{text[:15000]}',
            system=_EXTRACT_SYSTEM, tag='[GRAPH_RAG/EXTRACT]', max_output_tokens=1000)
        obj = _parse_json_obj(raw)
        if not obj:
            return 0
        entities = [e for e in (obj.get('entities') or [])
                   if isinstance(e, dict) and e.get('name') and e.get('type') in _ENTITY_TYPES][:20]
        relationships = [r for r in (obj.get('relationships') or [])
                         if isinstance(r, dict) and r.get('from') and r.get('to')][:20]
        if not entities:
            return 0

        with db.session() as s:
            s.run(
                "MERGE (d:PrepDocument {doc_id: $doc_id}) "
                "SET d.board_id = $board_id, d.name = $name",
                doc_id=doc_id, board_id=board_id, name=name[:200],
            ).consume()
            for e in entities:
                ename = str(e['name'])[:120]
                s.run(
                    "MERGE (n:Entity {board_id: $board_id, name_lower: toLower($name)}) "
                    "ON CREATE SET n.name = $name, n.type = $type "
                    "WITH n "
                    "MATCH (d:PrepDocument {doc_id: $doc_id}) "
                    "MERGE (d)-[:MENTIONS]->(n)",
                    board_id=board_id, name=ename, type=e['type'], doc_id=doc_id,
                ).consume()
            for r in relationships:
                s.run(
                    "MATCH (a:Entity {board_id: $board_id, name_lower: toLower($from)}) "
                    "MATCH (b:Entity {board_id: $board_id, name_lower: toLower($to)}) "
                    "MERGE (a)-[rel:RELATES_TO {relation: $relation}]->(b)",
                    board_id=board_id, **{'from': str(r['from'])[:120], 'to': str(r['to'])[:120]},
                    relation=str(r.get('relation') or 'relates to')[:80],
                ).consume()
        log.info('[GRAPH_RAG] %s -> %d entities, %d relationships written (doc=%s)',
                 name, len(entities), len(relationships), doc_id)
        return len(entities)
    except Exception as e:
        log.info('[GRAPH_RAG] extraction skipped for %s (%s): %s', name, e.__class__.__name__, e)
        return 0


def delete_document(board_id: str, doc_id: str) -> None:
    """Remove a document node (and any now-orphaned entities it alone
    mentioned) — best-effort, called when its canvas node is deleted."""
    if not db.is_ready():
        return
    try:
        with db.session() as s:
            s.run("MATCH (d:PrepDocument {doc_id: $doc_id}) DETACH DELETE d", doc_id=doc_id).consume()
            s.run(
                "MATCH (n:Entity {board_id: $board_id}) "
                "WHERE NOT (n)<-[:MENTIONS]-(:PrepDocument) DETACH DELETE n",
                board_id=board_id,
            ).consume()
    except Exception as e:
        log.info('[GRAPH_RAG] delete skipped for doc=%s (%s)', doc_id, e.__class__.__name__)


def hybrid_context(board_id: str, query: str, max_chars: int = 2500) -> str:
    """1-hop graph expansion seeded by simple name/keyword overlap with
    `query` — no embedding needed for the seed step, keeping this cheap
    and dependency-free (the vector RAG in `_rag_block` already covers
    pure similarity search; this covers cross-document relationships).
    Returns '' when Neo4j is unreachable or nothing matches."""
    if not query or not db.is_ready():
        return ''
    try:
        words = [w for w in re.findall(r"[A-Za-z0-9()]{3,}", query.lower())][:12]
        if not words:
            return ''
        with db.session(default_access_mode='READ') as s:
            rows = s.run(
                "MATCH (n:Entity {board_id: $board_id}) "
                "WHERE any(w IN $words WHERE toLower(n.name) CONTAINS w) "
                "MATCH (n)<-[:MENTIONS]-(d:PrepDocument) "
                "OPTIONAL MATCH (n)-[rel:RELATES_TO]-(m:Entity) "
                "OPTIONAL MATCH (m)<-[:MENTIONS]-(d2:PrepDocument) "
                "RETURN DISTINCT n.name AS entity, n.type AS etype, "
                "collect(DISTINCT d.name) AS docs, "
                "collect(DISTINCT {rel: rel.relation, other: m.name, other_docs: d2.name}) AS related "
                "LIMIT 15",
                board_id=board_id, words=words,
            ).data()
    except Exception as e:
        log.info('[GRAPH_RAG] hybrid_context skipped (%s)', e.__class__.__name__)
        return ''
    if not rows:
        return ''
    lines = []
    for r in rows:
        docs = ', '.join(d for d in (r.get('docs') or []) if d)
        line = f"- {r['entity']} ({r.get('etype', 'Entity')}) — mentioned in: {docs or 'unknown'}"
        related = [x for x in (r.get('related') or []) if x and x.get('other')]
        if related:
            bits = [f"{x['other']} [{x['rel']}]" for x in related[:4] if x.get('rel')]
            if bits:
                line += f"; related: {', '.join(bits)}"
        lines.append(line)
    out = '\n'.join(lines)
    return out[:max_chars]
