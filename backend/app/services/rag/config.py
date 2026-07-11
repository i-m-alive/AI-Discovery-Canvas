"""
Configuration for the retrieval-augmented (RAG) subsystem.

ADAPTATION NOTE (ai-discovery-canvas): originally Azure OpenAI
``text-embedding-3-large``; REWRITTEN to use AWS Bedrock embeddings — no
Azure dependency remains anywhere in this project (chat/completion via
``llm_service.py`` and embeddings via this package both call Bedrock now).
Credentials are resolved by boto3's standard chain — the SAME
``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN`` /
``AWS_REGION`` env vars used by ``llm_service.py`` for chat — nothing
embedding-specific to configure beyond the model id/dimension below.

Bedrock embedding model families ``embedder.py`` knows how to call today:
Amazon Titan Embed Text (v1 & v2) and Cohere Embed (English/Multilingual
v3), picked by model-id prefix. See ``embedder.py``'s ``_build_request()``
if you need to add another provider's request/response shape.
"""

from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip() or default)
    except (TypeError, ValueError):
        return default


# ── AWS Bedrock embedding model ───────────────────────────────────────
# EMBED_MODEL doubles as (a) the Bedrock model id passed to invoke_model
# and (b) the cache namespace key read by cache.py/service.py/store.py —
# kept as one name so the rest of the RAG package needed zero changes
# beyond this file + embedder.py.
EMBED_MODEL = os.environ.get('BEDROCK_EMBEDDING_MODEL_ID', 'amazon.titan-embed-text-v2:0').strip()

# Native output width for the default model (Titan Embed Text v2 supports
# 256/512/1024, default 1024; Titan v1 is fixed at 1536; Cohere Embed v3 is
# fixed at 1024). The FAISS index width must match whichever model you
# configure exactly — changing this means a full reindex (delete
# data/rag/*).
EMBED_DIM = _int('BEDROCK_EMBEDDING_DIM', 1024)

# ── Quota the pipeline throttles to ──────────────────────────────────
# Bedrock embedding quotas are per-account/per-model and vary a lot;
# these are conservative defaults — raise via env once you know your real
# on-demand throughput for the chosen model.
EMBED_MAX_TPM = _int('BEDROCK_EMBED_MAX_TPM', 100_000)
EMBED_MAX_RPM = _int('BEDROCK_EMBED_MAX_RPM', 60)

# ── Parallelism ───────────────────────────────────────────────────────
# NOTE: unlike Azure's embeddings.create (which accepts a list of inputs
# per call), Bedrock's invoke_model embeds exactly ONE text per call for
# every model family embedder.py supports today — there is no multi-input
# batch request. So there's no batch-size knob here anymore; embed_texts()
# parallelizes across individual texts (bounded by EMBED_MAX_WORKERS)
# instead of grouping them into request batches.
EMBED_MAX_WORKERS = _int('BEDROCK_EMBED_MAX_WORKERS', min(8, (os.cpu_count() or 4)))

# Hard per-input token cap — conservative default; Titan Embed v2 accepts
# up to 8192 tokens.
EMBED_MAX_INPUT_TOKENS = _int('BEDROCK_EMBED_MAX_INPUT_TOKENS', 8000)

EMBED_MAX_RETRIES = _int('BEDROCK_EMBED_MAX_RETRIES', 6)

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
    """`backend/data/rag/` by default. Override with RAG_DATA_DIR for a
    mounted volume in prod."""
    override = os.environ.get('RAG_DATA_DIR', '').strip()
    if override:
        return Path(override)
    # this file: app/services/rag/config.py → backend/ is parents[3]
    return Path(__file__).resolve().parents[3] / 'data' / 'rag'


def is_configured() -> bool:
    """Non-secret readiness: a Bedrock embedding model id is set. AWS
    credentials themselves are probed separately by
    embedder.is_available() so a missing/invalid credential surfaces a
    precise error rather than silently disabling retrieval."""
    return bool(EMBED_MODEL)
