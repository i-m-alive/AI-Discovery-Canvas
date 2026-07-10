# RAG subsystem — FAISS + Azure OpenAI `text-embedding-3-large`

Scalable, token-efficient retrieval so growth in projects/documents
doesn't translate into ever-larger LLM prompts. Embed once, store in
FAISS, retrieve only the most relevant chunks before calling the model.

## Layout

| File | Responsibility |
|---|---|
| `config.py` | Env-driven settings: endpoint, deployment, api_version, dim, quotas, chunk sizes, paths. |
| `embedder.py` | Azure OpenAI embeddings — batched, parallel (bounded pool), rate-limited (250k TPM / 1500 RPM), retried, L2-normalised. Key from Key Vault. |
| `cache.py` | Content-hash embedding cache (`sha256(model|text)`), disk-backed `.npz`. Skips re-embedding identical text across runs. |
| `chunking.py` | Structure-aware, token-bounded semantic chunking with overlap. HTML→text for generated docs. |
| `store.py` | FAISS namespaces (`IndexIDMap2(IndexFlatIP)`), incremental upsert/delete **by document**, atomic persistence, scoped metadata filtering. |
| `service.py` | Facade: `index_document`, `index_documents`, `retrieve`, `retrieve_context`, `delete_document`, `stats`. |
| `indexer.py` | Neo4j → embeddings ingestion (generated docs, source summaries, project meta); incremental + bulk `reindex_all`; background `kickoff_*`. |

## Configuration

Non-secret values are env vars (defaults shown):

```
AZURE_EMBEDDING_ENDPOINT     = https://navicoreinst.openai.azure.com/
AZURE_EMBEDDING_DEPLOYMENT   = text-embedding-3-large
AZURE_EMBEDDING_API_VERSION  = 2024-02-01
AZURE_EMBEDDING_DIM          = 3072
AZURE_EMBEDDING_MAX_TPM      = 250000
AZURE_EMBEDDING_MAX_RPM      = 1500
AZURE_EMBEDDING_BATCH_SIZE   = 128
AZURE_EMBEDDING_MAX_WORKERS  = min(8, cpu_count)
RAG_CHUNK_TOKENS             = 512
RAG_CHUNK_OVERLAP            = 64
RAG_TOP_K                    = 8
RAG_DATA_DIR                 = backend/data/rag   (override for a volume)
```

**Secret** — the API key is **never** in code or env-committed. It is
read from Azure Key Vault, secret name **`embedding-api-key`**
(`https://navicore.vault.azure.net/`), via `secret_manager.get_secret`.
Local-dev fallback only: `EMBEDDING_API_KEY` env var.

> Note on the requested client snippet: `AzureKeyCredential` belongs to
> the `azure-ai-*` SDKs, not the `openai` SDK this repo uses. The embedder
> constructs `openai.AzureOpenAI(azure_endpoint=…, api_key=…,
> api_version=…)` — the working, repo-consistent form — with the key
> sourced from Key Vault.

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
- **Best-effort everywhere** — if FAISS/numpy/openai/the key are missing,
  `is_enabled()` is False and every call no-ops; the graph-context paths
  keep working unchanged.
- **Cold start** — `POST /rag/reindex` (no body) builds the whole index;
  run once after deploying with the key in place.
- **Model/dim change** — changing `AZURE_EMBEDDING_DIM` or the model means
  a full reindex (vector widths must match). Delete `data/rag/*` and
  reindex.
