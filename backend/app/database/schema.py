"""Graph schema, constraints, and indexes for the NaviCore Neo4j layer.

This file is the single source of truth for:

  * Which labels exist (Project, Technology, Document, Capability, Person,
    Application, Workflow, WorkflowNode, Agent, OutputTemplate, AgentOutput,
    GeneratedDoc).
  * Which relationship types are allowed and what they mean.
  * The unique constraints and lookup indexes the persistence layer relies
    on. Without these the dedup-by-canonical-name logic in
    `KnowledgeGraph.add_or_get_node` would silently break.

`initialize_schema()` is idempotent — it issues `CREATE CONSTRAINT … IF NOT
EXISTS` and `CREATE INDEX … IF NOT EXISTS` so it's safe to call on every
server start. The caller (migrate_json_to_neo4j.py / server.py startup hook)
just runs it unconditionally.

Design notes
============

* Entity nodes (Project, Technology, etc.) carry both a stable `id` (uuid hex)
  AND a canonical name. The (label, canonical_name) pair is the dedup key
  used by `add_or_get_node`. A separate UNIQUE constraint on `id` lets the
  rest of the system (links, deletes, edge endpoints) address nodes by id
  without re-resolving names.

* Workflow nodes are first-class graph entities, not JSON blobs. A workflow
  is `(:Workflow)-[:HAS_NODE]->(:WorkflowNode)-[:CONNECTS_TO]->(:WorkflowNode)`.
  The per-node component config stays as a JSON property on `:WorkflowNode`
  (`config_json`) — that part of the data is opaque to the graph engine but
  keeps writes cheap and round-trips lossless.

* Lineage: when /structure or /run produces a final document the writer creates
      (:Project)-[:HAS_DOCUMENT]->(:GeneratedDoc)
      (:GeneratedDoc)-[:PRODUCED_BY]->(:Workflow)
      (:GeneratedDoc)-[:CONTRIBUTED_BY]->(:Agent)   (when known)
  This is the "workflow lineage" / "AI reasoning traversal" foundation the
  PRD called out.
"""

from __future__ import annotations

import logging

from app.database.neo4j_client import session

log = logging.getLogger('rd.schema')


# ---------------------------------------------------------------------------
# Labels and relationship types — referenced from KnowledgeGraph and the
# definitions stores. Keeping them here means a code grep for "Neo4j labels"
# lands in one file.
# ---------------------------------------------------------------------------

# Knowledge-Atlas entity labels (existing behaviour, plus :Entity for
# polymorphic lookup).
ENTITY_LABELS = (
    'Project', 'Technology', 'Person', 'Document',
    'Capability', 'Application',
)
ENTITY_LABEL_BY_TYPE = {
    'project':     'Project',
    'technology':  'Technology',
    'person':      'Person',
    'document':    'Document',
    'capability':  'Capability',
    'application': 'Application',
}
ENTITY_TYPE_BY_LABEL = {v: k for k, v in ENTITY_LABEL_BY_TYPE.items()}


# Workflow / agent / output / lineage labels.
PLATFORM_LABELS = (
    'Workflow', 'WorkflowNode',
    'Agent', 'OutputTemplate',
    'AgentOutput', 'GeneratedDoc',
    # CapabilityMap: per-project business-architecture view. Each
    # regeneration creates a NEW node; the latest is the one with
    # `is_latest=true` (only one per project). Prior versions stay so we
    # can show a history.
    'CapabilityMap',
)


# Project Intelligence Model labels (Wave 0). Additive — these are the
# application-structure + process + intelligence sub-model. Only :Repository
# and :Source are populated in Wave 1; the rest are schema-only placeholders
# for later waves. Every node carries an `id` (uuid hex) under a UNIQUE
# constraint via `_ID_UNIQUE_LABELS` below.
INTELLIGENCE_LABELS = (
    'Repository', 'Module', 'Service', 'API', 'Function',
    'TechnologyLifecycle', 'BusinessProcess', 'DecisionPoint',
    'UserStory', 'AgentRecommendation',
)


# Asset labels — concrete uploaded inputs and AI-derived artefacts that
# carry on-disk file paths. The user-facing rule is that the BYTES live on
# disk; only metadata + relationships + (small) generated summaries land
# in Neo4j.
#
# Uploaded files are multi-labeled `:Source` PLUS one of `:UploadedDoc`,
# `:Image`, `:Video` so callers can either query the polymorphic surface
# (`MATCH (s:Source) WHERE …`) or the specific kind (`MATCH (v:Video)`).
# `:UploadedDoc` is deliberately NOT `:Document` to avoid colliding with
# the Atlas entity label of the same name (which represents the abstract
# entity-extracted "this project has a document" concept, not a file).
ASSET_LABELS = (
    'Source', 'UploadedDoc', 'Image', 'Video',
    'Frame', 'OcrOutput', 'Summary',
    'WorkflowRun',
)


# Knowledge-Atlas relationship types (mirrors `EDGE_TYPES` in the legacy
# knowledge_graph.py).
ATLAS_REL_TYPES = (
    'USES', 'DEPENDS_ON', 'WORKED_ON', 'SIMILAR_TO',
    'FAILED_DUE_TO', 'DERIVED_FROM', 'PRODUCES', 'RELATED_TO',
)


# Workflow / lineage relationship types.
PLATFORM_REL_TYPES = (
    'HAS_NODE',                # (:Workflow)-[:HAS_NODE]->(:WorkflowNode)
    'CONNECTS_TO',             # (:WorkflowNode)-[:CONNECTS_TO]->(:WorkflowNode)
    'BELONGS_TO',              # (:Workflow)-[:BELONGS_TO]->(:Project)
    'USES_AGENT',              # (:WorkflowNode)-[:USES_AGENT]->(:Agent)
    'USES_OUTPUT',             # (:WorkflowNode)-[:USES_OUTPUT]->(:OutputTemplate)
    'HAS_DOCUMENT',            # (:Project)-[:HAS_DOCUMENT]->(:GeneratedDoc)
    'PRODUCED_BY',             # (:GeneratedDoc)-[:PRODUCED_BY]->(:Workflow)
    'CONTRIBUTED_BY',          # (:GeneratedDoc)-[:CONTRIBUTED_BY]->(:Agent)
    'EXECUTED_AT',             # (:AgentOutput)-[:EXECUTED_AT]->(:WorkflowNode)
    'RUN_OF',                  # (:AgentOutput)-[:RUN_OF]->(:Agent)
    'IN_WORKFLOW',             # (:AgentOutput)-[:IN_WORKFLOW]->(:Workflow)
    'IN_PROJECT',              # (:AgentOutput)-[:IN_PROJECT]->(:Project)
    # ── Asset / processing-lineage edges (Phase 2) ──
    'PROJECT_HAS_WORKFLOW',    # (:Project)-[:PROJECT_HAS_WORKFLOW]->(:Workflow)
    'WORKFLOW_USED_SOURCE',    # (:Workflow)-[:WORKFLOW_USED_SOURCE]->(:Source)
    'DOCUMENT_BELONGS_TO_PROJECT',  # (:UploadedDoc)-[:DOCUMENT_BELONGS_TO_PROJECT]->(:Project)
    'VIDEO_HAS_FRAME',         # (:Video)-[:VIDEO_HAS_FRAME]->(:Frame)
    'FRAME_GENERATED_SUMMARY', # (:Frame)-[:FRAME_GENERATED_SUMMARY]->(:Summary)
    'FRAME_HAS_OCR',           # (:Frame)-[:FRAME_HAS_OCR]->(:OcrOutput)
    'AGENT_GENERATED_OUTPUT',  # (:Agent)-[:AGENT_GENERATED_OUTPUT]->(:GeneratedDoc)
    'PROJECT_HAS_ARTIFACT',    # (:Project)-[:PROJECT_HAS_ARTIFACT]->(:GeneratedDoc)
    # ── Workflow run lineage ──
    'RUN_OF_WORKFLOW',         # (:WorkflowRun)-[:RUN_OF_WORKFLOW]->(:Workflow)
    'RUN_FOR_PROJECT',         # (:WorkflowRun)-[:RUN_FOR_PROJECT]->(:Project)
    'RUN_HAS_SOURCE',          # (:WorkflowRun)-[:RUN_HAS_SOURCE]->(:Source)
    'RUN_PRODUCED_ARTIFACT',   # (:WorkflowRun)-[:RUN_PRODUCED_ARTIFACT]->(:GeneratedDoc)
    'RUN_INVOKED_AGENT',       # (:WorkflowRun)-[:RUN_INVOKED_AGENT]->(:Agent)
    'SOURCE_PRODUCED_SUMMARY', # (:Source)-[:SOURCE_PRODUCED_SUMMARY]->(:Summary)
    'PROCESSED_IN_RUN',        # (:Source|:Frame|:Summary)-[:PROCESSED_IN_RUN]->(:WorkflowRun)
    # ── Capability map ──
    'HAS_CAPABILITY_MAP',      # (:Project)-[:HAS_CAPABILITY_MAP {is_latest}]->(:CapabilityMap)
    'CAPABILITY_MAP_REPLACES', # (:CapabilityMap)-[:CAPABILITY_MAP_REPLACES]->(:CapabilityMap)
    # ── Project Intelligence Model (Wave 0/1) ──
    'HAS_REPOSITORY',          # (:Project)-[:HAS_REPOSITORY]->(:Repository)
    'HAS_MODULE',              # (:Repository)-[:HAS_MODULE]->(:Module)
    'CONTAINS_MODULE',         # (:Module)-[:CONTAINS_MODULE]->(:Module)
    'IMPLEMENTS_SERVICE',      # (:Module)-[:IMPLEMENTS_SERVICE]->(:Service)
    'EXPOSES_API',             # (:Service)-[:EXPOSES_API]->(:API)
    'CONSUMES_API',            # (:Service)-[:CONSUMES_API]->(:API)
    'REALIZES',                # (:Service|:BusinessProcess)-[:REALIZES]->(:Capability)
    'HAS_PROCESS',             # (:Project)-[:HAS_PROCESS]->(:BusinessProcess)
    'HAS_USER_STORY',          # (:Project)-[:HAS_USER_STORY]->(:UserStory)
    'HAS_AGENT_RECOMMENDATION',# (:Project)-[:HAS_AGENT_RECOMMENDATION]->(:AgentRecommendation)
    'HAS_LIFECYCLE',           # (:Technology)-[:HAS_LIFECYCLE]->(:TechnologyLifecycle)
    'REPLACED_BY',             # (:TechnologyLifecycle)-[:REPLACED_BY]->(:Technology)
    'DERIVED_FROM_SOURCE',     # (:<derived>)-[:DERIVED_FROM_SOURCE {extractor,confidence}]->(:Source)
    # ── Process / business intelligence (process_extractor_v1) ──
    'HAS_DECISION_POINT',      # (:BusinessProcess)-[:HAS_DECISION_POINT]->(:DecisionPoint)
    'SUPPORTED_BY',            # (:BusinessProcess)-[:SUPPORTED_BY]->(:Service)
    'INVOLVES_API',            # (:BusinessProcess)-[:INVOLVES_API]->(:API)
    'DESCRIBES',               # (:UserStory)-[:DESCRIBES]->(:BusinessProcess)
    'IMPLEMENTED_BY',          # (:UserStory)-[:IMPLEMENTED_BY]->(:Service)
    # ── Function-level intelligence (function_extractor_v1) ──
    'DEFINES_FUNCTION',        # (:Module)-[:DEFINES_FUNCTION]->(:Function)
    'HAS_FUNCTION',            # (:Service)-[:HAS_FUNCTION]->(:Function)
    'CALLS',                   # (:Function)-[:CALLS]->(:Function)
    'HANDLES_API',             # (:Function)-[:HANDLES_API]->(:API)
    # ── Agent layer (agent_recommender_mvp_v1) ──
    'TARGETS',                 # (:AgentRecommendation)-[:TARGETS]->(:Function|:Service|:BusinessProcess)
)


# ---------------------------------------------------------------------------
# Schema DDL — constraints + indexes. Every statement is idempotent.
# ---------------------------------------------------------------------------

# Unique IDs across the whole platform. Every node carries `id` (string).
# We attach the constraint to each label individually because Neo4j 5 does
# not support a constraint spanning multiple labels. The asset labels
# (Source, Video, Frame, …) are multi-labeled in practice (e.g. a video is
# `:Source:Video`), but the id constraint must exist under each so a
# `MATCH (v:Video {id:…})` lookup hits an index.
_ID_UNIQUE_LABELS = ENTITY_LABELS + PLATFORM_LABELS + ASSET_LABELS + INTELLIGENCE_LABELS


# WorkflowNode is the one exception to global-id-uniqueness: the UI mints
# per-workflow ids like 'n1', 'n2', and the same id can legitimately appear
# in two different workflows. The actual identity is the composite
# (workflow_id, id) pair, which is what the rest of the code matches on.
_COMPOSITE_KEY_LABELS = {'WorkflowNode'}


def _id_unique_statements() -> list[str]:
    out: list[str] = []
    for label in _ID_UNIQUE_LABELS:
        if label in _COMPOSITE_KEY_LABELS:
            # Idempotent: drop any prior single-property constraint with the
            # legacy name, then add the composite. `IF EXISTS` makes the
            # drop a no-op on a fresh DB.
            out.append(
                f"DROP CONSTRAINT {label.lower()}_id_unique IF EXISTS"
            )
            out.append(
                f"CREATE CONSTRAINT {label.lower()}_composite_unique "
                f"IF NOT EXISTS "
                f"FOR (n:{label}) "
                f"REQUIRE (n.workflow_id, n.id) IS UNIQUE"
            )
            continue
        out.append(
            f"CREATE CONSTRAINT {label.lower()}_id_unique IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
        )
    return out


# Canonical-name dedup for entity nodes. Same (label, canonical_name) ⇒ same
# node. This is what makes `add_or_get_node` upsert correctly.
def _canonical_name_unique_statements() -> list[str]:
    out: list[str] = []
    for label in ENTITY_LABELS:
        out.append(
            f"CREATE CONSTRAINT {label.lower()}_canonical_unique IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.canonical_name IS UNIQUE"
        )
    return out


# Lookup indexes — the queries that hit them are listed alongside each.
_INDEX_STATEMENTS = [
    # Project lookups by name (Atlas filter / project picker).
    "CREATE INDEX project_name_idx IF NOT EXISTS "
    "FOR (p:Project) ON (p.name)",

    # Workflow listing by project_id (GET /workflows?project_id=…).
    "CREATE INDEX workflow_project_idx IF NOT EXISTS "
    "FOR (w:Workflow) ON (w.project_id)",

    # Workflow listing by updated_at desc (sort path).
    "CREATE INDEX workflow_updated_idx IF NOT EXISTS "
    "FOR (w:Workflow) ON (w.updated_at)",

    # WorkflowNode lookup inside a workflow (id is unique globally, but
    # we also frequently filter by workflow_id alone).
    "CREATE INDEX workflow_node_workflow_idx IF NOT EXISTS "
    "FOR (n:WorkflowNode) ON (n.workflow_id)",

    # Agent / OutputTemplate name lookup.
    "CREATE INDEX agent_name_idx IF NOT EXISTS "
    "FOR (a:Agent) ON (a.name)",
    "CREATE INDEX output_template_name_idx IF NOT EXISTS "
    "FOR (o:OutputTemplate) ON (o.name)",

    # AgentOutput filter axes (the /agent-outputs query supports all of
    # project_id, agent_id, node_id, workflow_id).
    "CREATE INDEX agent_output_project_idx IF NOT EXISTS "
    "FOR (r:AgentOutput) ON (r.project_id)",
    "CREATE INDEX agent_output_agent_idx IF NOT EXISTS "
    "FOR (r:AgentOutput) ON (r.agent_id)",
    "CREATE INDEX agent_output_node_idx IF NOT EXISTS "
    "FOR (r:AgentOutput) ON (r.node_id)",
    "CREATE INDEX agent_output_workflow_idx IF NOT EXISTS "
    "FOR (r:AgentOutput) ON (r.workflow_id)",
    "CREATE INDEX agent_output_updated_idx IF NOT EXISTS "
    "FOR (r:AgentOutput) ON (r.updated_at)",

    # GeneratedDoc filter axes (GET /generated-docs).
    "CREATE INDEX generated_doc_project_idx IF NOT EXISTS "
    "FOR (g:GeneratedDoc) ON (g.project_id)",
    "CREATE INDEX generated_doc_node_idx IF NOT EXISTS "
    "FOR (g:GeneratedDoc) ON (g.node_id)",
    "CREATE INDEX generated_doc_updated_idx IF NOT EXISTS "
    "FOR (g:GeneratedDoc) ON (g.updated_at)",

    # ── Asset / processing-lineage indexes (Phase 2) ──
    # Source filter axes — used by "every artifact for this project" /
    # "every source ingested by this run" queries.
    "CREATE INDEX source_project_idx IF NOT EXISTS "
    "FOR (s:Source) ON (s.project_id)",
    "CREATE INDEX source_run_idx IF NOT EXISTS "
    "FOR (s:Source) ON (s.run_id)",
    "CREATE INDEX source_kind_idx IF NOT EXISTS "
    "FOR (s:Source) ON (s.kind)",
    "CREATE INDEX source_path_idx IF NOT EXISTS "
    "FOR (s:Source) ON (s.file_path)",

    # Video / frame lookup — frames are addressed by their owning video
    # plus index, so the (video_id, frame_index) pair must be fast.
    "CREATE INDEX video_filename_idx IF NOT EXISTS "
    "FOR (v:Video) ON (v.filename)",
    "CREATE INDEX frame_video_idx IF NOT EXISTS "
    "FOR (f:Frame) ON (f.video_id)",

    # WorkflowRun: most reads are "latest runs for a project / workflow".
    "CREATE INDEX workflow_run_project_idx IF NOT EXISTS "
    "FOR (r:WorkflowRun) ON (r.project_id)",
    "CREATE INDEX workflow_run_workflow_idx IF NOT EXISTS "
    "FOR (r:WorkflowRun) ON (r.workflow_id)",
    "CREATE INDEX workflow_run_started_idx IF NOT EXISTS "
    "FOR (r:WorkflowRun) ON (r.started_at)",

    # Summary attribution — `agent_id` filter is the common query.
    "CREATE INDEX summary_agent_idx IF NOT EXISTS "
    "FOR (s:Summary) ON (s.agent_id)",

    # CapabilityMap: list by project_id, sort by version DESC.
    "CREATE INDEX capability_map_project_idx IF NOT EXISTS "
    "FOR (c:CapabilityMap) ON (c.project_id)",
    "CREATE INDEX capability_map_latest_idx IF NOT EXISTS "
    "FOR (c:CapabilityMap) ON (c.is_latest)",
    "CREATE INDEX capability_map_version_idx IF NOT EXISTS "
    "FOR (c:CapabilityMap) ON (c.version)",

    # ── Project Intelligence Model (Wave 0/1) ──
    "CREATE INDEX repository_project_idx IF NOT EXISTS "
    "FOR (r:Repository) ON (r.project_id)",
    "CREATE INDEX module_repository_idx IF NOT EXISTS "
    "FOR (m:Module) ON (m.repository_id)",
    "CREATE INDEX service_project_idx IF NOT EXISTS "
    "FOR (s:Service) ON (s.project_id)",
    "CREATE INDEX api_service_idx IF NOT EXISTS "
    "FOR (a:API) ON (a.service_id)",
    "CREATE INDEX business_process_project_idx IF NOT EXISTS "
    "FOR (b:BusinessProcess) ON (b.project_id)",
    "CREATE INDEX user_story_project_idx IF NOT EXISTS "
    "FOR (u:UserStory) ON (u.project_id)",
    "CREATE INDEX agent_reco_project_idx IF NOT EXISTS "
    "FOR (a:AgentRecommendation) ON (a.project_id)",
    "CREATE INDEX tech_lifecycle_tech_idx IF NOT EXISTS "
    "FOR (t:TechnologyLifecycle) ON (t.technology_id)",
    "CREATE INDEX function_module_idx IF NOT EXISTS "
    "FOR (f:Function) ON (f.module_id)",
    "CREATE INDEX function_project_idx IF NOT EXISTS "
    "FOR (f:Function) ON (f.project_id)",
]


def initialize_schema() -> None:
    """Apply every constraint and index. Idempotent — safe to call on every
    server start. Returns silently on success; logs at INFO so the operator
    can see schema creation in the server log."""
    statements = (
        _id_unique_statements()
        + _canonical_name_unique_statements()
        + _INDEX_STATEMENTS
    )
    log.info("[NEO4J] applying schema (%d statements)", len(statements))
    with session() as s:
        for q in statements:
            try:
                s.run(q).consume()
            except Exception as e:
                # Schema statements that "already exist" return without
                # error thanks to IF NOT EXISTS; anything else is a real
                # problem the operator should see.
                log.error("[NEO4J] schema statement failed: %s\n  %s", e, q)
                raise
    log.info("[NEO4J] schema ready")


def drop_everything() -> None:
    """Nuclear option for testing: removes every node and relationship.
    NEVER called from production paths — invoked manually from a REPL or
    from `migrate_json_to_neo4j.py --reset`."""
    log.warning("[NEO4J] DROPPING all nodes/relationships")
    with session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
