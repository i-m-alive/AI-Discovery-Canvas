"""
Session + JWT helpers.

Two pieces of state coexist:

1. A signed JWT (HS256, stdlib only — no PyJWT dependency) that the
   browser holds. It is self-describing: any worker can validate it
   without consulting shared state.

2. A server-side session record keyed by the JWT's `jti` claim. This
   gives us the one thing a stateless JWT can't: instant revocation on
   /auth/logout. The record also tracks the auth_provider that minted
   the session, so we can route refresh / logout back to the right
   provider when Azure goes live.

The session store is intentionally in-process (matches `credential_store`)
— restarting the server invalidates all sessions, which is the right
default for a single-host dev tool. When we go multi-instance the swap
target is Redis; the public surface (create / get / revoke) doesn't
change.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import uuid
from typing import Optional

from app.auth.config import settings


# --- JWT (HS256, no external dep) -----------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(data: str) -> bytes:
    pad = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _sign(msg: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).digest()


def jwt_encode(payload: dict, secret: Optional[str] = None) -> str:
    secret = secret or settings.jwt_secret
    header = {'alg': 'HS256', 'typ': 'JWT'}
    h = _b64url_encode(json.dumps(header, separators=(',', ':'), sort_keys=True).encode())
    p = _b64url_encode(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode())
    sig = _b64url_encode(_sign(f'{h}.{p}'.encode(), secret))
    return f'{h}.{p}.{sig}'


def jwt_decode(token: str, secret: Optional[str] = None) -> Optional[dict]:
    """Verify signature + exp. Returns the payload dict or None."""
    if not token or token.count('.') != 2:
        return None
    secret = secret or settings.jwt_secret
    h, p, sig = token.split('.')
    try:
        expected = _b64url_encode(_sign(f'{h}.{p}'.encode(), secret))
    except Exception:
        return None
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64url_decode(p))
    except Exception:
        return None
    exp = payload.get('exp')
    if isinstance(exp, (int, float)) and exp < time.time():
        return None
    return payload


# --- Server-side session table -------------------------------------------

_SESSIONS: dict[str, dict] = {}
_LOCK = threading.RLock()


def _purge_expired_unlocked() -> None:
    now = time.time()
    expired = [jti for jti, rec in _SESSIONS.items() if rec.get('exp', 0) < now]
    for jti in expired:
        _SESSIONS.pop(jti, None)


def create(*, user: dict, auth_provider: str,
           ttl_seconds: Optional[int] = None,
           extras: Optional[dict] = None) -> dict:
    """Mint a session for `user`. Returns {token, jti, exp, user}.

    `user` is the canonical user dict (id, name, email, role, auth_provider).
    `extras` is provider-specific scratch data (e.g. Azure tokens) that
    stays server-side — it is never echoed back to the browser.
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.session_ttl_seconds
    now = int(time.time())
    jti = uuid.uuid4().hex
    exp = now + ttl

    payload = {
        'sub': user.get('id') or user.get('email') or jti,
        'jti': jti,
        'iat': now,
        'exp': exp,
        'name':  user.get('name'),
        'email': user.get('email'),
        'role':  user.get('role'),
        'auth_provider': auth_provider,
    }
    token = jwt_encode(payload)

    with _LOCK:
        _purge_expired_unlocked()
        _SESSIONS[jti] = {
            'jti': jti,
            'user': dict(user),
            'auth_provider': auth_provider,
            'created': now,
            'exp': exp,
            'active_mode': user.get('active_mode', user.get('assigned_role', 'user')),
            'extras': dict(extras or {}),
        }

    return {'token': token, 'jti': jti, 'exp': exp, 'user': dict(user)}


def get(jti: str) -> Optional[dict]:
    if not jti:
        return None
    with _LOCK:
        _purge_expired_unlocked()
        rec = _SESSIONS.get(jti)
        if rec is None:
            return None
        return {
            **rec,
            'user': dict(rec['user']),
            'active_mode': rec.get('active_mode', 'user'),
            'extras': dict(rec['extras']),
        }


def revoke(jti: str) -> bool:
    if not jti:
        return False
    with _LOCK:
        return _SESSIONS.pop(jti, None) is not None


def update_mode(jti: str, active_mode: str) -> bool:
    """Update active_mode for a session. Only valid values are 'developer' or 'user'."""
    if not jti or active_mode not in ('developer', 'user'):
        return False
    with _LOCK:
        rec = _SESSIONS.get(jti)
        if rec is None:
            return False
        rec['active_mode'] = active_mode
        rec['user'] = dict(rec['user'])
        rec['user']['active_mode'] = active_mode
        return True


def validate(token: str) -> Optional[dict]:
    """Decode + check the server-side record still exists. Returns the
    session record (with user dict) or None."""
    payload = jwt_decode(token)
    if not payload:
        return None
    jti = payload.get('jti')
    rec = get(jti) if jti else None
    if rec is None:
        return None
    # Belt-and-braces: the JWT carries user data but the server record
    # is authoritative (e.g. role may have been changed mid-session).
    return rec
