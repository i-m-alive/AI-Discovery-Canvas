"""
Object storage abstraction (Wave 0) — LOCAL backend only.

Single seam for durable BYTES — raw repo snapshots, raw documents/images, and
generated outputs. Today there is exactly one backend:

    local  — content-addressed files under OBJECT_STORE_DIR (defaults to
             backend/data/object_store). Identical durability semantics to the
             app's existing disk usage, so behaviour is unchanged when nothing
             new is configured.

The backend is selected via `OBJECT_STORE_BACKEND` purely so a remote/object
backend can be slotted in LATER without touching any call site. No remote
backend is implemented now — any non-`local` value still resolves to local.

Design rules (match the codebase):
  * Never raises on store failure — returns None and logs.
  * Bytes only here; METADATA (uri, size, hash) is the caller's job
    (Postgres `sources` / Neo4j `:Source.object_uri`).
  * Keys are logical paths; the local backend stores a content-addressed blob
    plus a pointer file so identical bytes are de-duplicated.

Public surface:
    put_bytes(key, data, content_type=None)        -> uri (str)
    put_file(key, path, content_type=None)         -> uri
    get_bytes(uri_or_key)                           -> bytes | None
    exists(uri_or_key)                              -> bool
    snapshot_dir(key, dir_path, exclude=...)        -> uri | None  (tar.gz a tree)
    is_enabled()                                    -> bool   (always True — local)
    backend_name()                                  -> str    ('local')
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import threading
from pathlib import Path
from typing import Optional

from app.core import config as app_config
from app.core.logging import log


_URI_LOCAL_PREFIX = 'objlocal://'

_DEFAULT_EXCLUDE = ('.git', 'node_modules', '__pycache__', '.venv', 'venv',
                    'dist', 'build', '.next', '.terraform')

_lock = threading.Lock()


def backend_name() -> str:
    """Only `local` is implemented; any other configured value resolves here."""
    return 'local'


def is_enabled() -> bool:
    return True


def _local_root() -> Path:
    root = getattr(app_config, 'OBJECT_STORE_DIR', '') or ''
    if not root:
        backend_root = Path(__file__).resolve().parent.parent.parent  # backend/
        root = str(backend_root / 'data' / 'object_store')
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def put_bytes(key: str, data: bytes, content_type: Optional[str] = None) -> str:
    """Store `data` under logical `key`; return a durable uri. content_type is
    accepted for forward-compatibility with a future remote backend (ignored
    by the local backend)."""
    return _local_put(_norm_key(key), data)


def put_file(key: str, path: str, content_type: Optional[str] = None) -> str:
    with open(path, 'rb') as fh:
        return put_bytes(key, fh.read(), content_type=content_type)


def get_bytes(uri_or_key: str) -> Optional[bytes]:
    """Read bytes back by uri (preferred) or bare key. None if absent."""
    if not uri_or_key:
        return None
    return _local_get(uri_or_key)


def exists(uri_or_key: str) -> bool:
    return get_bytes(uri_or_key) is not None


def snapshot_dir(key: str, dir_path: str,
                 exclude: tuple[str, ...] = _DEFAULT_EXCLUDE) -> Optional[str]:
    """tar.gz a directory tree and store it under `key`. Returns the uri, or
    None on failure (caller treats absence as 'no snapshot')."""
    try:
        buf = io.BytesIO()
        base = Path(dir_path)
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            for root, dirs, files in os.walk(dir_path):
                dirs[:] = [d for d in dirs if d not in exclude]
                for f in files:
                    fp = Path(root) / f
                    try:
                        tar.add(str(fp), arcname=str(fp.relative_to(base)))
                    except (OSError, ValueError):
                        continue
        return put_bytes(key, buf.getvalue(), content_type='application/gzip')
    except Exception as e:
        log.warning("[OBJSTORE] snapshot_dir failed for %s (%s)",
                    key, e.__class__.__name__)
        return None


# ---------------------------------------------------------------------------
# Local backend — content-addressed blob + logical pointer
# ---------------------------------------------------------------------------

def _norm_key(key: str) -> str:
    return (key or '').strip().lstrip('/').replace('\\', '/')


def _local_put(key: str, data: bytes) -> str:
    root = _local_root()
    sha = hashlib.sha256(data).hexdigest()
    with _lock:
        blob = root / 'blobs' / sha[:2] / sha
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            tmp = blob.with_suffix('.tmp')
            tmp.write_bytes(data)
            os.replace(tmp, blob)
        ptr = root / 'keys' / key
        ptr.parent.mkdir(parents=True, exist_ok=True)
        ptr.write_text(sha, encoding='utf-8')
    return f'{_URI_LOCAL_PREFIX}{key}'


def _local_get(uri_or_key: str) -> Optional[bytes]:
    root = _local_root()
    key = (uri_or_key[len(_URI_LOCAL_PREFIX):]
           if uri_or_key.startswith(_URI_LOCAL_PREFIX) else _norm_key(uri_or_key))
    ptr = root / 'keys' / key
    if not ptr.exists():
        return None
    try:
        sha = ptr.read_text(encoding='utf-8').strip()
        blob = root / 'blobs' / sha[:2] / sha
        return blob.read_bytes() if blob.exists() else None
    except Exception:
        return None