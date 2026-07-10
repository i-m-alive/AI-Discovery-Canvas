"""
Configuration for the retrieval-augmented (RAG) subsystem.

Everything here is env-driven so the same code runs in App Service /
Container Apps / a dev box without edits. The embedding API KEY is the
one value that is NEVER read here — it is fetched lazily from Azure Key
Vault (secret name ``embedding-api-key``) by ``embedder.py`` via the
centralised secret manager, so it can't land in a module constant.

Azure OpenAI embedding deployment (defaults match the provisioned
resource):

    endpoint    https://navicoreinst.openai.azure.com/
    deployment  text-embedding-3-large
    api_version 2024-02-01
    dimensions  3072  (text-embedding-3-large native size)

Rate limits (the deployment quota the pipeline must respect):

    tokens / minute   250,000
    requests / minute 1,500
"""

from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip() or default)
    except (TypeError, ValueError):
        return default


# ── Azure OpenAI embedding deployment ────────────────────────────────
EMBED_ENDPOINT    = os.environ.get('AZURE_EMBEDDING_ENDPOINT',
                                   'https://navicoreinst.openai.azure.com/').strip()
EMBED_DEPLOYMENT  = os.environ.get('AZURE_EMBEDDING_DEPLOYMENT', 'text-embedding-3-large').strip()
EMBED_MODEL       = os.environ.get('AZURE_EMBEDDING_MODEL', 'text-embedding-3-large').strip()
EMBED_API_VERSION = os.environ.get('AZURE_EMBEDDING_API_VERSION', '2024-02-01').strip()

# Native embedding width for text-embedding-3-large. The model also
# supports the `dimensions` parameter for shortened vectors; keep the
# native size unless an operator deliberately reduces it (the FAISS index
# width must match, so changing this means a full reindex).
EMBED_DIM = _int('AZURE_EMBEDDING_DIM', 3072)

# ── Deployment quota (the pipeline throttles to stay under these) ─────
EMBED_MAX_TPM = _int('AZURE_EMBEDDING_MAX_TPM', 250_000)
EMBED_MAX_RPM = _int('AZURE_EMBEDDING_MAX_RPM', 1_500)

# ── Batching / parallelism ───────────────────────────────────────────
# Inputs packed into a single embeddings request. Azure accepts up to
# 2048 inputs per call; 128 keeps each request well under the per-request
# token ceiling while amortising HTTP overhead.
EMBED_BATCH_SIZE = _int('AZURE_EMBEDDING_BATCH_SIZE', 128)

# Concurrent in-flight requests. Defaults to the CPU count (capped) so
# the local embedding/normalisation work parallelises without starving
# the box; the sliding-window limiter still enforces the RPM/TPM quota
# regardless of how many workers push at once.
EMBED_MAX_WORKERS = _int('AZURE_EMBEDDING_MAX_WORKERS', min(8, (os.cpu_count() or 4)))

# Hard per-input token cap. text-embedding-3-large accepts 8192 tokens;
# we trim a touch below to leave headroom for the tokenizer estimate
# being slightly off.
EMBED_MAX_INPUT_TOKENS = _int('AZURE_EMBEDDING_MAX_INPUT_TOKENS', 8000)

EMBED_MAX_RETRIES = _int('AZURE_EMBEDDING_MAX_RETRIES', 6)

# ── Semantic chunking ────────────────────────────────────────────────
CHUNK_TARGET_TOKENS  = _int('RAG_CHUNK_TOKENS', 512)
CHUNK_OVERLAP_TOKENS = _int('RAG_CHUNK_OVERLAP', 64)
CHUNK_MIN_TOKENS     = _int('RAG_CHUNK_MIN_TOKENS', 24)

# ── Retrieval defaults ───────────────────────────────────────────────
RETRIEVE_TOP_K   = _int('RAG_TOP_K', 8)
# Inner-product (cosine, vectors are L2-normalised) floor. Hits below
# this are dropped so a low-signal corpus doesn't inject noise.
RETRIEVE_MIN_SCORE = float(os.environ.get('RAG_MIN_SCORE', '0.18') or 0.18)


def data_dir() -> Path:
    """`backend/data/rag/` by default — sibling of the existing Neo4j JSON
    backups. Override with RAG_DATA_DIR for a mounted volume in prod."""
    override = os.environ.get('RAG_DATA_DIR', '').strip()
    if override:
        return Path(override)
    # this file: app/services/rag/config.py → backend/ is parents[3]
    return Path(__file__).resolve().parents[3] / 'data' / 'rag'


def is_configured() -> bool:
    """Non-secret readiness: an endpoint + deployment are set. The key
    itself is probed separately by the embedder so a missing key surfaces
    a precise error rather than silently disabling retrieval."""
    return bool(EMBED_ENDPOINT and EMBED_DEPLOYMENT)
