# RAG subsystem — FAISS + AWS Bedrock embeddings

> **ai-discovery-canvas note:** this package originally called Azure
> OpenAI's `text-embedding-3-large`. It has been rewritten to call **AWS
> Bedrock** (Titan Embed or Cohere Embed, picked via
> `BEDROCK_EMBEDDING_MODEL_ID`) — no Azure OpenAI dependency remains. The
> rest of this file (chunking, caching, FAISS store, service facade) is
> unchanged from the original design.

Scalable, token-efficient retrieval so growth in projects/documents
doesn't translate into ever-larger LLM prompts. Embed once, store in
FAISS, retrieve only the most relevant chunks before calling the model.

## Layout

| File | Responsibility |
|---|---|
| `config.py` | Env-driven settings: endpoint, deployment, api_version, dim, quotas, chunk sizes, paths. |
| `embedder.py` | AWS Bedrock embeddings (Titan/Cohere) — one `invoke_model` call per text, parallel (bounded pool), rate-limited, retried, L2-normalised. Credentials via boto3's default chain. |
| `cache.py` | Content-hash embedding cache (`sha256(model|text)`), disk-backed `.npz`. Skips re-embedding identical text across runs. |
| `chunking.py` | Structure-aware, token-bounded semantic chunking with overlap. HTML→text for generated docs. |
| `store.py` | FAISS namespaces (`IndexIDMap2(IndexFlatIP)`), incremental upsert/delete **by document**, atomic persistence, scoped metadata filtering. |
| `service.py` | Facade: `index_document`, `index_documents`, `retrieve`, `retrieve_context`, `delete_document`, `stats`. |
| `indexer.py` | Neo4j → embeddings ingestion (generated docs, source summaries, project meta); incremental + bulk `reindex_all`; background `kickoff_*`. |

## Configuration

Non-secret values are env vars (defaults shown):

```
BEDROCK_EMBEDDING_MODEL_ID   = amazon.titan-embed-text-v2:0
BEDROCK_EMBEDDING_DIM        = 1024
BEDROCK_EMBED_MAX_TPM        = 100000
BEDROCK_EMBED_MAX_RPM        = 60
BEDROCK_EMBED_MAX_WORKERS    = min(8, cpu_count)
BEDROCK_EMBED_MAX_INPUT_TOKENS = 8000
BEDROCK_EMBED_MAX_RETRIES    = 6
RAG_CHUNK_TOKENS             = 512
RAG_CHUNK_OVERLAP            = 64
RAG_TOP_K                    = 8
RAG_DATA_DIR                 = backend/data/rag   (override for a volume)
```

**Credentials** — the same `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
`AWS_SESSION_TOKEN` / `AWS_REGION` used by `llm_service.py` for chat are
reused here; boto3 resolves them via its own default credential chain
(env vars → `~/.aws/credentials` → IAM role). Nothing embedding-specific
to configure beyond the model id/dimension above — there is no separate
API key to manage.

## Already wired

- **NaviCORE Assistant** (`/chat`) — retrieves top-k project-scoped
  excerpts and folds them into the prompt alongside the graph context.
- **Document generation** — every persisted `GeneratedDoc` is indexed
  incrementally (`_persist_generated_doc`).
- **Project ingestion** — project create + rename re-index project
  metadata; `POST /rag/reindex` bulk-builds.
- **Diagnostics** — `GET /rag/stats`, `POST /rag/search`.

## Adoption seam for entity / capability / Atlas pipelines

These build a brief from project text today. To make them
retrieval-augmented, replace "dump all summaries (truncated)" with
faceted retrieval — embed the corpus once (already done by the indexer),
then per facet retrieve the most relevant chunks:

```python
from app.services import rag

if rag.is_enabled():
    techs, _ = rag.retrieve_context(
        "technologies, frameworks, libraries, databases, cloud services used",
        project_id=pid, k=8)
    people, _ = rag.retrieve_context(
        "people, teams, roles, owners, stakeholders involved",
        project_id=pid, k=6)
    caps, _ = rag.retrieve_context(
        "business capabilities and features this project provides",
        project_id=pid, k=8)
    # feed only these focused excerpts to the extraction prompt instead
    # of the whole corpus → fewer tokens, same or better recall.
```

`merge_extraction` (graph write) and the Cypher relationship model are
unchanged — only the *context selection* feeding the extraction LLM call
moves from full-context to retrieval. Keep Neo4j/Cypher for structural
queries; use vectors only for "find the relevant text".

## Operational notes

- **Incremental by design** — re-indexing a `doc_id` replaces its prior
  chunk vectors, so edits/renames/regenerations never leave stale data.
- **Best-effort everywhere** — if FAISS/numpy/boto3/AWS credentials are
  missing, `is_enabled()` is False and every call no-ops; the graph-context
  paths keep working unchanged.
- **Cold start** — `POST /rag/reindex` (no body) builds the whole index;
  run once after deploying with credentials in place.
- **Model/dim change** — changing `BEDROCK_EMBEDDING_DIM` or
  `BEDROCK_EMBEDDING_MODEL_ID` means a full reindex (vector widths must
  match). Delete `data/rag/*` and reindex.
