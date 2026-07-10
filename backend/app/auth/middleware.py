"""
Flask middleware: token extraction, before-request gate, decorators.

The integration model is deliberately minimal:

    from auth import install_auth
    install_auth(app)

`install_auth` registers the auth blueprint AND installs a single
`before_request` hook that:

  1. Pulls the token from the Authorization header (preferred) or the
     `navicore_session` cookie.
  2. Validates it against the active AuthProvider.
  3. Attaches the resolved user to `flask.g.current_user` (or None).
  4. Rejects requests to non-public paths when there is no user.

Public paths are the login page, the auth endpoints themselves, static
assets, and the `/check-dependencies` health probe. Everything else
requires a session.

The `@auth_required` decorator is provided for routes that want explicit
opt-in (e.g. blueprints registered after install_auth). It is a no-op
when install_auth's before_request gate is already protecting the path,
so applying both is safe.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable, Iterable, Optional

from flask import Flask, Response, g, jsonify, redirect, request

from app.auth.providers import get_provider


# Paths that never require a session. Patterns are exact prefixes —
# keep this list small and obvious.
_PUBLIC_PREFIXES: tuple[str, ...] = (
    '/login',
    '/auth/',           # /auth/me, /auth/login/*, /auth/logout, /auth/callback/*
    '/check-dependencies',
    '/favicon.ico',
    '/static/',
)


def _extract_token() -> Optional[str]:
    """Pull a bearer token from the request. Header wins over cookie so
    API clients can override the browser cookie when needed."""
    auth_hdr = request.headers.get('Authorization', '')
    if auth_hdr.lower().startswith('bearer '):
        token = auth_hdr.split(None, 1)[1].strip()
        if token:
            return token
    cookie = request.cookies.get('navicore_session')
    return cookie or None


def _is_public(path: str) -> bool:
    for prefix in _PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return True
    return False


def _wants_html(path: str) -> bool:
    """A best-effort guess at whether this request was a top-level
    browser navigation vs. an XHR/fetch. Drives the 401 → /login
    redirect: HTML navigations are redirected, JSON callers get a 401.

    Fetch / XHR conventions we lean on:
      * `Sec-Fetch-Mode: navigate` is set by browsers on top-level
        navigations and never on fetch().
      * `X-Requested-With: XMLHttpRequest` is set by some XHR clients.
      * `Accept: text/html` strongly suggests a navigation; conversely
        `Accept: application/json` (or `*/*`) does not.
    The path-extension fallback from earlier was too aggressive — it
    redirected JSON fetches with default `Accept: */*` to /login, which
    fetch() can't follow cross-origin.
    """
    if request.headers.get('Sec-Fetch-Mode') == 'navigate':
        return True
    if request.headers.get('X-Requested-With', '').lower() == 'xmlhttprequest':
        return False
    accept = request.headers.get('Accept', '')
    if 'text/html' in accept:
        return True
    return False


def current_user() -> Optional[dict]:
    """Return the user dict for the active request, or None."""
    return getattr(g, 'current_user', None)


def auth_required(fn: Callable) -> Callable:
    """Decorator form. Most routes don't need this because the
    before_request gate already protects them — useful for routes
    mounted on blueprints that bypass the gate, or for routes that
    want a clearer audit trail in code."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return _unauthorized()
        return fn(*args, **kwargs)

    return wrapper


def _unauthorized() -> Response:
    if _wants_html(request.path):
        # Pass the original path back so the login page can bounce the
        # user to where they were trying to go.
        return redirect(f'/login?next={request.full_path.rstrip("?")}')
    resp = jsonify({'error': 'unauthorized', 'message': 'sign in required'})
    resp.status_code = 401
    return resp


def install_auth(app: Flask,
                 extra_public_prefixes: Iterable[str] = ()) -> None:
    """Wire the auth blueprint and the before_request gate into `app`.

    `extra_public_prefixes` lets the host app add bespoke unauthenticated
    paths (e.g. webhook callbacks) without editing this file.

    Prefix matching rule: an entry that ends with `/` is a prefix and
    matches everything below it. Any other entry is an EXACT path
    match. This lets the host app expose `/` (the React shell) as a
    single public path without accidentally opening up `/projects`,
    `/people`, etc.
    """
    # Imported here, not at top, because routes.py imports from this
    # module — avoid circular import at package-init time.
    from app.auth.routes import auth_bp
    app.register_blueprint(auth_bp)

    raw_public = tuple(_PUBLIC_PREFIXES) + tuple(extra_public_prefixes)
    # `/` is an exact-only match -- treating it as a prefix would whitelist
    # every URL. Other entries ending with `/` (e.g. `/auth/`, `/assets/`)
    # are real prefixes. Anything without a trailing slash is exact.
    exact_paths   = tuple(p for p in raw_public if p == '/' or not p.endswith('/'))
    path_prefixes = tuple(p for p in raw_public if p != '/' and p.endswith('/'))

    @app.before_request
    def _auth_gate():
        # CORS preflight requests (OPTIONS) never carry credentials — the
        # browser sends them before the real request to check CORS policy.
        # Blocking them with a 401 causes the browser to report
        # "Failed to fetch" for ALL cross-origin POST/PUT/DELETE calls,
        # even when the user is fully authenticated.  Let Flask-CORS
        # handle OPTIONS responses entirely; we skip auth for that method.
        if request.method == 'OPTIONS':
            g.current_user = None
            g.auth_session = None
            return None

        # Always resolve the user (even on public paths) so anonymous
        # JSON endpoints like /check-dependencies can still log who hit
        # them if they want to.
        token = _extract_token()
        provider = get_provider()
        rec = provider.validate_token(token) if token else None
        g.current_user = rec['user'] if rec else None
        g.auth_session = rec

        path = request.path or '/'
        # Public if it matches an exact path or a slash-prefix.
        if path in exact_paths:
            return None
        for prefix in path_prefixes:
            if path.startswith(prefix):
                return None

        if g.current_user is None:
            return _unauthorized()
        return None
