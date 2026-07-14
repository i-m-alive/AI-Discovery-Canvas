# AI Discovery Canvas — Windows Setup Guide

Get the full stack (Flask backend + Next.js frontend + Postgres + Neo4j)
running on a Windows machine from scratch. All commands are **PowerShell**
(not cmd.exe).

## Architecture at a glance

| Piece | Runs where | Port |
|---|---|---|
| Backend (Flask) | host, from `backend\.venv` | 5101 |
| Frontend (Next.js dev) | host, `npm run dev` | 5173 |
| PostgreSQL | host, Windows service (installer) | 5432 |
| Neo4j 5 + APOC | Docker (`docker-compose.yml` in repo root) | 7475 (UI) / 7688 (Bolt) |

The frontend proxies `/api/*` to the backend, so you only ever open
`http://localhost:5173` in the browser.

## 1. Install prerequisites

| Tool | Where | Notes |
|---|---|---|
| Git | <https://git-scm.com> | defaults are fine |
| Python 3.12+ | <https://python.org> | tick **“Add python.exe to PATH”** during install |
| Node.js 20+ LTS | <https://nodejs.org> | for the Next.js frontend |
| PostgreSQL 15 or 16 | <https://postgresql.org/download/windows> | remember the password you set for the `postgres` superuser; it installs as an auto-starting Windows service |
| Docker Desktop | <https://docker.com> | only needed for Neo4j |

## 2. Clone the repo and start Neo4j

```powershell
git clone <repo-url> ai-discovery-canvas
cd ai-discovery-canvas

# Neo4j 5 community + APOC (ports 7475 / 7688 — see docker-compose.yml)
docker compose up -d
```

Docker Desktop must be running first. Verify with `docker ps` — you should
see a healthy `canvas-neo4j` container. The Neo4j browser UI is at
<http://localhost:7475> (user `neo4j`, password `canvas-dev` unless you set
`NEO4J_PASSWORD` in a `.env` next to `docker-compose.yml`).

## 3. Create the Postgres database

```powershell
& "C:\Program Files\PostgreSQL\16\bin\createdb.exe" -U postgres ai_discovery_canvas
```

(Adjust the `16` if you installed Postgres 15.) Enter the password you chose
during installation. **That's all** — every table is created automatically by
the backend on first start (`create_all()`), there are no migration scripts
to run.

## 4. Backend setup

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
# if activation is blocked:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env
```

### Edit `backend\.env`

The values that actually matter for local dev:

```ini
# --- app / auth ---
AUTH_MODE=mock              # mock sign-in page, any name/email works
DISABLE_KEY_VAULT=1         # env-only secrets, no Azure Key Vault

# --- Postgres (NOTE: user is `postgres` on Windows) ---
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=ai_discovery_canvas
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<the password from the Postgres installer>
POSTGRES_SSLMODE=disable

# --- Neo4j (matches docker-compose.yml's host-side ports) ---
NEO4J_URI=bolt://localhost:7688
NEO4J_USER=neo4j
NEO4J_PASSWORD=canvas-dev
NEO4J_DATABASE=neo4j

# --- AWS Bedrock: REQUIRED for all agents / Copilot / RAG ---
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=...
BEDROCK_MODEL_ID=...
BEDROCK_ROUTER_MODEL_ID=...        # cheap model for routing/classification
BEDROCK_EMBEDDING_MODEL_ID=...     # Titan embed v2

# --- integrations (optional) ---
TAVILY_API_KEY=...          # Grounded Web Researcher needs this
TEAMS_TENANT_ID=...         # only for the Teams transcript import
TEAMS_CLIENT_ID=...
```

The realistic shortcut: get a working `.env` from a teammate over a secure
channel and change only the `POSTGRES_*` lines. **Never commit `.env` to
git.**

## 5. Run it (two terminals)

```powershell
# Terminal 1 — backend on :5101
cd ai-discovery-canvas\backend
.venv\Scripts\python.exe main.py

# Terminal 2 — frontend on :5173
cd ai-discovery-canvas\frontend
npm install       # first time only
npm run dev
```

The backend prints a self-diagnosing startup banner — it names whatever is
misconfigured (`[ok]`/warning lines for Bedrock, Neo4j, Postgres) and lists
the tables it ensured. Look there first when something doesn't work.

Open **http://localhost:5173**, sign in with the mock form (any name/email —
a fresh user row is created automatically), create a Project and a Workshop,
and start from the Pre-Workshop tab by uploading a document.

## Troubleshooting

- **`Activate.ps1` refuses to run** → `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, reopen PowerShell.
- **Port 5432 already in use** (another Postgres, e.g. a Docker one) → point
  `POSTGRES_PORT` in `.env` at whichever instance you actually created the
  database on.
- **`docker compose up` fails** → start Docker Desktop first; on first run it
  needs to download the Neo4j image.
- **Agents fail / Copilot silent** → almost always Bedrock credentials or
  region; the startup banner and backend logs name the exact call that failed.
- **Workshops are per-user**: the mock email you sign in with owns what you
  create — sign in with the same name/email every time or your projects
  “disappear”.
