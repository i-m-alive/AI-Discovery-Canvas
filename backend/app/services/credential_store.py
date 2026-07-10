"""
In-memory credential store.

The frontend never holds raw secrets after they are submitted: the user
posts {client_id, client_secret} to /salesforce/credentials, this store
mints a UUID, keeps the secrets server-side, and returns only the cred_id
(plus a masked label) to the browser. Every subsequent call (preview,
pipeline run, callback) references the credential by id.

Thread-safe. 24h TTL. Never logs the secrets dict; `safe_view()` is what
should be returned over the wire and what may appear in logs.

This is deliberately not persisted to disk — restarting the server
invalidates all sessions, which forces re-auth and minimises the blast
radius if the host is ever shared. If a longer-lived store is ever
needed, swap the dict for Vault / Secrets Manager — the public surface
(register/get/update/delete/safe_view) stays identical.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional


_STORE: dict[str, dict] = {}
_LOCK = threading.RLock()
_TTL_SECONDS = 24 * 3600


def _now() -> float:
    return time.time()


def _purge_expired_unlocked() -> None:
    cutoff = _now() - _TTL_SECONDS
    expired = [cid for cid, rec in _STORE.items() if rec.get('updated', 0) < cutoff]
    for cid in expired:
        _STORE.pop(cid, None)


def register(provider: str, fields: Optional[dict] = None,
             secrets: Optional[dict] = None) -> str:
    """Create a new credential record. Returns its id.

    `fields` is non-sensitive metadata (e.g. instance_url once known).
    `secrets` is the sensitive payload (client_secret, access_token, etc.)
    and is never returned by `safe_view`."""
    cred_id = uuid.uuid4().hex
    now = _now()
    with _LOCK:
        _purge_expired_unlocked()
        _STORE[cred_id] = {
            'id':       cred_id,
            'provider': provider,
            'status':   'pending',
            'fields':   dict(fields or {}),
            'secrets':  dict(secrets or {}),
            'created':  now,
            'updated':  now,
        }
    return cred_id


def get(cred_id: str) -> Optional[dict]:
    """Return the full record (including secrets) — only for use inside
    the connector. Never echo this back to a client."""
    if not cred_id:
        return None
    with _LOCK:
        _purge_expired_unlocked()
        rec = _STORE.get(cred_id)
        if rec is None:
            return None
        # Return a shallow copy so callers can't mutate store contents.
        return {
            **rec,
            'fields':  dict(rec['fields']),
            'secrets': dict(rec['secrets']),
        }


def update(cred_id: str, *, fields: Optional[dict] = None,
           secrets: Optional[dict] = None,
           status: Optional[str] = None) -> Optional[dict]:
    """Merge updates into an existing record. Returns the updated record
    or None if not found."""
    with _LOCK:
        _purge_expired_unlocked()
        rec = _STORE.get(cred_id)
        if rec is None:
            return None
        if fields:
            rec['fields'].update(fields)
        if secrets:
            rec['secrets'].update(secrets)
        if status:
            rec['status'] = status
        rec['updated'] = _now()
        return get(cred_id)


def delete(cred_id: str) -> bool:
    with _LOCK:
        return _STORE.pop(cred_id, None) is not None


def safe_view(cred_id: str) -> Optional[dict]:
    """Return what the UI is allowed to see: id, provider, status,
    non-sensitive fields, masked hint of which secrets are populated.
    NEVER includes raw client_secret / access_token / refresh_token."""
    rec = get(cred_id)
    if rec is None:
        return None
    s = rec['secrets']
    return {
        'id':       rec['id'],
        'provider': rec['provider'],
        'status':   rec['status'],
        'fields':   dict(rec['fields']),
        'has': {
            'client_id':     bool(s.get('client_id')),
            'client_secret': bool(s.get('client_secret')),
            'access_token':  bool(s.get('access_token')),
            'refresh_token': bool(s.get('refresh_token')),
        },
        'created':  rec['created'],
        'updated':  rec['updated'],
    }


def list_safe(provider: Optional[str] = None) -> list:
    with _LOCK:
        _purge_expired_unlocked()
        ids = list(_STORE.keys())
    out = []
    for cid in ids:
        v = safe_view(cid)
        if v is None:
            continue
        if provider and v['provider'] != provider:
            continue
        out.append(v)
    return out


def find_by_state(state: str, provider: Optional[str] = None) -> Optional[dict]:
    """Look up a record by the OAuth `state` value stored in fields.
    Returns None if no match. Used inside /salesforce/callback to find
    which credential a callback corresponds to."""
    if not state:
        return None
    with _LOCK:
        _purge_expired_unlocked()
        for rec in _STORE.values():
            if rec['fields'].get('auth_state') == state:
                if provider and rec['provider'] != provider:
                    continue
                return get(rec['id'])
    return None
