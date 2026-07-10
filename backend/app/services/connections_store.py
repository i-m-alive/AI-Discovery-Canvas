"""
In-process source-connection registry.

ADAPTATION NOTE (ai-discovery-canvas scaffold): upstream frd-generator's
`app/routes/connections.py` persisted the OAuth "Connect <provider>" registry
in Postgres (`app.postgres.repositories.source_connections`, a real table
with `id`, `tenant_id`, `user_id`, `provider`, `kv_secret_name`, `oauth_state`,
`status`, `account_label`, `scopes`, `expires_at`). This project does not
carry the Postgres subsystem (out of scope for the lean backbone — see the
root README), so the registry is reimplemented here as an in-process dict,
mirroring `app.services.credential_store` / `app.auth.sessions` (same
"restart clears state" trade-off the rest of this backend already accepts
for dev). The public surface (`upsert_pending`, `get_by_state`,
`mark_status`, `mark_connected`, `list_for_user`) matches the method names
the route module calls, returning attribute-accessible records (via
SimpleNamespace) so `connections.py` needed no logic changes beyond the
import + dropping the `with_session(...)` wrapper.

NEVER stores the OAuth token itself — only the Key Vault secret NAME
(`kv_secret_name`), exactly like the upstream Postgres row. The token lives
only in Key Vault, written by `app.services.secret_manager.set_secret_by_name`.

Swap target if this project later needs multi-instance / durable
connections: back this module with a real table (Postgres, Neo4j, or
SQLite) — callers only ever go through the five functions below.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Optional


_LOCK = threading.RLock()
_BY_ID: dict[str, dict] = {}


def _record(**kw) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def upsert_pending(*, id: str, tenant_id: str, user_id: str, provider: str,
                   kv_secret_name: str, oauth_state: str, scopes: str) -> SimpleNamespace:
    """Create (or reuse) the pending connection row for (tenant, user, provider).
    Mirrors the upstream repo's "reuses the (tenant,user,provider) row" behaviour."""
    now = time.time()
    with _LOCK:
        existing_id = None
        for rid, row in _BY_ID.items():
            if (row['tenant_id'], row['user_id'], row['provider']) == (tenant_id, user_id, provider):
                existing_id = rid
                break
        row_id = existing_id or id
        _BY_ID[row_id] = {
            'id': row_id,
            'tenant_id': tenant_id,
            'user_id': user_id,
            'provider': provider,
            'kv_secret_name': kv_secret_name,
            'oauth_state': oauth_state,
            'status': 'pending',
            'account_label': None,
            'scopes': scopes,
            'expires_at': None,
            'created_at': _BY_ID.get(row_id, {}).get('created_at', now),
            'updated_at': now,
        }
        return _record(**_BY_ID[row_id])


def get_by_state(state: str) -> Optional[SimpleNamespace]:
    if not state:
        return None
    with _LOCK:
        for row in _BY_ID.values():
            if row.get('oauth_state') == state:
                return _record(**row)
    return None


def mark_status(connection_id: str, status: str) -> None:
    with _LOCK:
        row = _BY_ID.get(connection_id)
        if row is not None:
            row['status'] = status
            row['updated_at'] = time.time()


def mark_connected(connection_id: str, *, account_label: Optional[str] = None,
                   scopes: Optional[str] = None, expires_at=None) -> None:
    with _LOCK:
        row = _BY_ID.get(connection_id)
        if row is None:
            return
        row['status'] = 'connected'
        if account_label is not None:
            row['account_label'] = account_label
        if scopes is not None:
            row['scopes'] = scopes
        row['expires_at'] = expires_at
        row['updated_at'] = time.time()


def list_for_user(tenant_id: str, user_id: str) -> list[dict]:
    """Non-secret view for GET /connections — same shape the frontend expects
    from the upstream Postgres-backed endpoint."""
    with _LOCK:
        out = []
        for row in _BY_ID.values():
            if row['tenant_id'] == (tenant_id or '') and row['user_id'] == user_id:
                out.append({
                    'id': row['id'],
                    'provider': row['provider'],
                    'status': row['status'],
                    'account_label': row['account_label'],
                    'scopes': row['scopes'],
                    'expires_at': (row['expires_at'].isoformat()
                                   if hasattr(row['expires_at'], 'isoformat') else row['expires_at']),
                })
        return out
