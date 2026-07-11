# AI Discovery Canvas

Bootstrap scaffold for a new product, **AI Discovery Canvas**. This is a
brand-new, standalone project (sibling to `NaviCore/`, not inside it) that
reuses the generic, domain-agnostic backbone of `NaviCore/frd-generator`'s
Flask backend — auth, the Neo4j client, the LLM service, RAG/ingestion,
guardrails, and the generic OAuth "connect a source" pattern — behind a
brand-new frontend.

The current priority (per product direction) is **plumbing over polish**:
prove the whole chain (frontend -> backend -> auth -> LLM) works end to
end before investing in any UI design. Accordingly:

* **Frontend is Next.js** (App Router, plain JavaScript, no TypeScript),
  **not** a copy of frd-generator's Vite + React Router shell. It is
  intentionally minimal and unstyled — plain inline styles / a bit of
  global CSS, no Tailwind, no component library, no design tokens ported
  over. Real UI work is deferred; see "Deferred" below.
* **Backend is Flask**, largely reused from frd-generator, trimmed to only
  the subsystems this product needs.

## What was copied vs. newly written (backend)

Copied verbatim from `NaviCore/frd-generator/backend/`:
`app/core/logging.py`; the entire `app/auth/` package (auth routes,
middleware, sessions, token validation, mock + Azure AD providers);
`app/database/neo4j_client.py` + `schema.py`; `app/services/llm_service.py`,
`secret_manager.py`, `object_store.py`, `source_oauth.py`,
`credential_store.py` (a dependency of `connections.py` not called out
explicitly but required for it to run), `code_summarizer.py`; the entire
`app/services/rag/` and `app/services/guardrails/` packages;
`app/routes/health.py`, `connections.py`, `guardrails.py`; `app/utils/`.

Written new / trimmed for this project:
`app/__init__.py` (new, lean `create_app()` wiring only what's listed
above, plus the new agents route below — no Postgres, People,
Intelligence, Modernize, Disrupt, legacy Workflow-Builder routes, or
Salesforce; those subsystems' source wasn't copied at all), `main.py`
(new entrypoint/banner, no `legacy_routes` dependency check),
`app/core/config.py` (trimmed — no `POSTGRES_*`/`PIM_*`, `PORT` defaults
to `5101`, `CORS_ORIGINS` defaults to the Next.js dev port `:3000`),
`app/database/__init__.py` (trimmed `bootstrap_neo4j()` — just driver +
`schema.initialize_schema()`, no `KnowledgeGraph` mirror or JSON
migration import), `app/services/connections_store.py` (new in-process
OAuth-connection registry, replacing frd-generator's Postgres-backed
one — same method names, so `connections.py` needed no logic changes),
`app/routes/agents.py` (new — see below), `requirements.txt` (trimmed to
only what's actually imported), `.env.example` (new).

Not copied at all (out of scope for this product):
`app/postgres/`, `app/people/`, `app/routes/{modernize,disrupt,
intelligence,legacy_routes,modernize_store,pages}.py`, `app/routes/frames/`,
`backend/static/`, `backend/data/`. A handful of copied modules
(`app/routes/health.py`, `app/routes/guardrails.py`,
`app/services/guardrails/{store,audit}.py`,
`app/services/rag/workflow_sources.py`) still contain lazy,
try/except-guarded `from app.postgres import ...` calls for
Postgres-additive features (e.g. persisted org guardrail settings,
audit-log linkage) — these were left as-is because they already degrade
gracefully (Postgres absent -> feature falls back to in-memory /
local-file behavior) exactly as they do in frd-generator when Postgres is
unreachable; no source-level change was needed.

## LLM provider: AWS Bedrock only — no Azure OpenAI dependency remains

Both chat/completion (`app/services/llm_service.py`) and RAG's embedding
step (`app/services/rag/embedder.py` + `config.py`) were rewritten to call
**AWS Bedrock** — Azure OpenAI has been fully removed from this project
(no package, no config, no code path references it anywhere). Public
function signatures (`complete()`, `embed_texts()`, `embed_query()`) are
unchanged, only the provider underneath.

Paste your credentials into `backend/.env` — one set of AWS credentials
covers both chat and embeddings:

```
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=                # only if using temporary/STS credentials
AWS_REGION=us-east-1

# Chat/completion
BEDROCK_MODEL_ID=...              # e.g. anthropic.claude-3-5-sonnet-20241022-v2:0

# RAG embeddings (Titan Embed by default; Cohere Embed also supported)
BEDROCK_EMBEDDING_MODEL_ID=amazon.titan-embed-text-v2:0
```

boto3 reads the `AWS_*` env vars itself via its own default credential
chain — no extra wiring needed.

## The proof route: `/api/agents/ping`

`POST /api/agents/ping` is new, auth-gated, and calls the copied
`llm_service.complete(...)`. It returns `{ ok: true, reply }` on success,
or `{ ok: false, error }` (still HTTP 200) if the LLM call fails for any
reason — most commonly because Bedrock credentials/model id aren't set
yet in `backend/.env`. That's expected out of the box; add real AWS
credentials + a Bedrock model id to get an actual model reply.
`GET /api/agents/ping` is a public, no-auth reachability check.

The Next.js `/canvas` page has a "Test agent backbone" button that calls
this route and renders the raw JSON response, so this one button is the
visible proof that Next.js -> rewrite -> Flask -> auth -> `llm_service`
is wired correctly end to end.

## Deferred (not an oversight)

No `@azure/msal-browser`/`@azure/msal-react`, no ported `Icon.jsx` /
`BrandLogo.jsx` / `BrandMark.jsx` / `ConnectProvider.jsx` / `LensContext.jsx`,
no frd-generator styling. All deferred until real UI/UX work starts.

## Running it — all via plain terminal commands

Nothing here *requires* Docker except Neo4j, and even that's optional for
a first smoke test: `bootstrap_neo4j()` never blocks server startup if
Neo4j isn't reachable (it just logs a warning and keeps the graph routes
degraded) — see `app/database/__init__.py`. So you can run backend +
frontend right now and exercise the `/api/agents/ping` Bedrock proof route
with zero database running at all.

1. **Backend** (Flask, port `5101`) — plain terminal, no container:
   ```
   cd backend
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   # backend/.env already exists (copied from .env.example) — open it and
   # paste your AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION /
   # BEDROCK_MODEL_ID values in directly.
   python main.py
   ```
2. **Frontend** (Next.js, port `3000`) — plain terminal, no container:
   ```
   cd frontend
   npm install
   npm run dev
   ```
   Open `http://localhost:3000` — it redirects to `/canvas`, which
   redirects to `/login` until you sign in (mock auth: any name/email).
   Once signed in, click "Test agent backbone" to call
   `/api/agents/ping` and confirm Bedrock actually replies.
3. **Neo4j — optional, only if/when you need graph features.** Two ways
   to run it, your choice:
   - Docker (simplest, remapped ports so it can run alongside
     frd-generator's own stack — HTTP `7475`, Bolt `7688`):
     `docker compose up -d`
   - Fully Docker-free: install Neo4j directly (e.g. `brew install
     neo4j` or the Neo4j Desktop app), then point `NEO4J_URI` in
     `backend/.env` at wherever that instance listens (default
     `bolt://localhost:7687`) instead of the `:7688` Docker-mapped port.

See `docs/AI_Discovery_Canvas_Feasibility_Analysis.html` for the full
reuse rationale and product roadmap.
