"""
Guardrails persistence facade.

Tries Postgres first; falls back to a process-local in-memory dict
whenever Postgres is unconfigured, unreachable, OR the schema migration
hasn't been applied yet (which is the common case during local dev).

Goal: the Privacy & Guardrails feature MUST work the moment a developer
opens the page, before they ever run `alembic upgrade head`. The
in-memory state lasts the lifetime of the process and is good enough
for demo / preview workflows.

Public surface (single import point — every endpoint + the resolver
go through this module instead of touching SQL directly):

    load_org()                       -> dict             (mode/categories/controls)
    save_org(payload, *, by_email)   -> dict             (echoed back)

    load_project(project_kg_id)      -> dict | None
    save_project(project_kg_id, payload)

    load_workflow(workflow_kg_id)    -> (dict | None, project_kg_id_hint)
    save_workflow(workflow_kg_id, payload)

Errors NEVER raise. Worst-case, the call returns the in-memory state.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional, Tuple


log = logging.getLogger('app.guardrails.store')


# ─────────────────────────────────────────────────────────────────────
# In-memory fallback state. Keyed by:
#   "_ORG"                        -> org defaults dict
#   "_PROJECT:<project_kg_id>"    -> project payload dict
#   "_WORKFLOW:<workflow_kg_id>"  -> { 'guardrails': <payload>, 'project_kg_id': <hint> }
# ─────────────────────────────────────────────────────────────────────
_MEM: Dict[str, dict] = {}
_LOCK = threading.RLock()


_DEFAULT_ORG = {
    'mode':       'standard',
    'categories': {},
    'controls': {
        'hide_in_graph':     False,
        'block_llm':         True,
        'redact_documents':  True,
        'mask_logs':         True,
        'block_export':      False,
    },
    'updated_by_email': None,
    'updated_at':       None,
}


def _pg_ready() -> bool:
    """Cheap, never-raising 'is Postgres usable right now?' check."""
    try:
        from app.postgres import is_ready
        return bool(is_ready())
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# Org settings
# ─────────────────────────────────────────────────────────────────────

def _hydrate_org(raw: Optional[dict]) -> dict:
    """Make sure an org payload has every required key with sensible
    defaults, even if it was returned from a freshly-created (empty)
    Postgres row."""
    if not raw:
        return dict(_DEFAULT_ORG)
    out = dict(_DEFAULT_ORG)
    if raw.get('mode'):           out['mode']             = raw['mode']
    if raw.get('categories'):     out['categories']       = dict(raw['categories'])
    # Merge controls so missing keys still get the safe default.
    if raw.get('controls'):       out['controls']         = {**out['controls'], **dict(raw['controls'])}
    if 'updated_by_email' in raw: out['updated_by_email'] = raw['updated_by_email']
    if 'updated_at' in raw:       out['updated_at']       = raw['updated_at']
    return out


def load_org() -> dict:
    """Return the org-wide defaults. Always succeeds — falls back to
    the in-memory copy (which itself defaults to STANDARD mode the
    first time the process starts)."""
    if _pg_ready():
        try:
            from app.postgres                 import session_scope
            from app.postgres.repositories    import org_settings as org_repo
            with session_scope() as session:
                if session is not None:
                    row = org_repo.get_or_default(session)
                    hydrated = _hydrate_org(row)
                    with _LOCK:
                        _MEM['_ORG'] = hydrated
                    return dict(hydrated)
        except Exception as e:
            log.warning('[store] load_org Postgres path failed (%s) - using in-memory fallback', e)

    with _LOCK:
        return _hydrate_org(_MEM.get('_ORG'))


def save_org(payload: dict, *, by_email: Optional[str] = None) -> dict:
    """Persist org-wide defaults. Writes to Postgres when available AND
    always updates the in-memory mirror so the change is live without
    a process restart even if the DB write fails."""
    mode       = (payload.get('mode') or 'standard')
    categories = payload.get('categories') or {}
    controls   = payload.get('controls')   or {}
    snapshot = {
        'mode':             mode,
        'categories':       categories,
        'controls':         controls,
        'updated_by_email': by_email,
        'updated_at':       None,
    }
    with _LOCK:
        _MEM['_ORG'] = snapshot

    if _pg_ready():
        try:
            from app.postgres                 import session_scope
            from app.postgres.repositories    import org_settings as org_repo
            with session_scope() as session:
                if session is not None:
                    org_repo.upsert(
                        session,
                        mode=mode,
                        categories=categories,
                        controls=controls,
                        updated_by_email=by_email,
                    )
        except Exception as e:
            log.warning('[store] save_org Postgres write failed (%s) - kept in memory only', e)
    return dict(snapshot)


# ─────────────────────────────────────────────────────────────────────
# Project guardrails
# ─────────────────────────────────────────────────────────────────────

def load_project(project_kg_id: str) -> Optional[dict]:
    """Return the project's persisted guardrails payload (the JSONB
    blob: {inherit, mode, categories, controls}) or None when nothing
    has been saved at this scope."""
    if not project_kg_id:
        return None
    if _pg_ready():
        try:
            from app.postgres                          import session_scope
            from sqlalchemy                            import select
            from app.postgres.models.project_metadata  import ProjectMetadata
            with session_scope() as session:
                if session is not None:
                    row = session.execute(
                        select(ProjectMetadata).where(ProjectMetadata.kg_id == project_kg_id)
                    ).scalar_one_or_none()
                    if row is not None and getattr(row, 'guardrails', None):
                        payload = dict(row.guardrails)
                        with _LOCK:
                            _MEM[f'_PROJECT:{project_kg_id}'] = payload
                        return payload
        except Exception as e:
            log.warning('[store] load_project Postgres path failed for %s (%s) - using memory',
                        project_kg_id, e)

    with _LOCK:
        v = _MEM.get(f'_PROJECT:{project_kg_id}')
        return dict(v) if v else None


def save_project(project_kg_id: str, payload: dict) -> dict:
    if not project_kg_id:
        return payload
    with _LOCK:
        _MEM[f'_PROJECT:{project_kg_id}'] = dict(payload)

    if _pg_ready():
        try:
            from app.postgres                          import session_scope
            from sqlalchemy                            import select
            from app.postgres.models.project_metadata  import ProjectMetadata
            with session_scope() as session:
                if session is None:
                    return payload
                row = session.execute(
                    select(ProjectMetadata).where(ProjectMetadata.kg_id == project_kg_id)
                ).scalar_one_or_none()
                if row is None:
                    row = ProjectMetadata(kg_id=project_kg_id, project_name=project_kg_id)
                    session.add(row)
                # The model may not have the guardrails attribute mapped at
                # runtime if the migration hasn't been applied - try/except
                # the assignment so we don't break the in-memory write.
                try:
                    row.guardrails = payload
                except Exception as set_err:
                    log.warning('[store] save_project: row.guardrails assignment failed (%s)', set_err)
        except Exception as e:
            log.warning('[store] save_project Postgres write failed for %s (%s) - kept in memory only',
                        project_kg_id, e)
    return dict(payload)


# ─────────────────────────────────────────────────────────────────────
# Workflow guardrails (lives inside workflow_metadata JSONB blob)
# ─────────────────────────────────────────────────────────────────────

def load_workflow(workflow_kg_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """Return (guardrails_payload, parent_project_kg_id_hint)."""
    if not workflow_kg_id:
        return None, None

    if _pg_ready():
        try:
            from app.postgres                            import session_scope
            from sqlalchemy                              import select
            from app.postgres.models.workflow_metadata   import WorkflowMetadata
            with session_scope() as session:
                if session is not None:
                    row = session.execute(
                        select(WorkflowMetadata).where(WorkflowMetadata.kg_id == workflow_kg_id)
                    ).scalar_one_or_none()
                    if row is not None:
                        meta = row.workflow_metadata or {}
                        guardrails = meta.get('guardrails') if isinstance(meta, dict) else None
                        proj = row.project_kg_id
                        with _LOCK:
                            _MEM[f'_WORKFLOW:{workflow_kg_id}'] = {
                                'guardrails': dict(guardrails) if guardrails else None,
                                'project_kg_id': proj,
                            }
                        return (dict(guardrails) if guardrails else None), proj
        except Exception as e:
            log.warning('[store] load_workflow Postgres path failed for %s (%s) - using memory',
                        workflow_kg_id, e)

    with _LOCK:
        v = _MEM.get(f'_WORKFLOW:{workflow_kg_id}')
        if not v:
            return None, None
        g = v.get('guardrails')
        return (dict(g) if g else None), v.get('project_kg_id')


def save_workflow(workflow_kg_id: str, payload: dict,
                  project_kg_id: Optional[str] = None) -> dict:
    if not workflow_kg_id:
        return payload
    with _LOCK:
        _MEM[f'_WORKFLOW:{workflow_kg_id}'] = {
            'guardrails':    dict(payload),
            'project_kg_id': project_kg_id,
        }

    if _pg_ready():
        try:
            from app.postgres                            import session_scope
            from sqlalchemy                              import select
            from app.postgres.models.workflow_metadata   import WorkflowMetadata
            with session_scope() as session:
                if session is None:
                    return payload
                row = session.execute(
                    select(WorkflowMetadata).where(WorkflowMetadata.kg_id == workflow_kg_id)
                ).scalar_one_or_none()
                if row is None:
                    row = WorkflowMetadata(kg_id=workflow_kg_id, workflow_name=workflow_kg_id)
                    if project_kg_id:
                        row.project_kg_id = project_kg_id
                    session.add(row)
                meta = dict(row.workflow_metadata or {})
                meta['guardrails'] = payload
                row.workflow_metadata = meta
        except Exception as e:
            log.warning('[store] save_workflow Postgres write failed for %s (%s) - kept in memory only',
                        workflow_kg_id, e)
    return dict(payload)
