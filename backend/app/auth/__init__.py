"""
Authentication subsystem for NaviCORE / Document Generator.

Public surface — only these names should be imported by `server.py` and
other modules outside the `auth` package. Everything else is internal.

    auth_bp           Flask blueprint mounting /auth/* routes + /login page
    install_auth      Wire the blueprint + before-request gate into a Flask app
    auth_required     Decorator for routes that need a logged-in user
    current_user      Helper returning the user attached to the active request
                      (or None if the route is anonymous / pre-auth)
    AUTH_MODE         Effective auth mode at process start ("mock" or "azure")

The provider classes (MockAuthProvider, AzureADAuthProvider) live under
`auth.providers` and are picked up automatically by `install_auth` based
on AUTH_MODE. Adding a new provider later (e.g. Okta, Google Workspace)
means dropping a new file in `auth/providers/` and registering it in
`auth.providers.get_provider` — nothing else changes.
"""

from app.auth.config import AUTH_MODE, settings  # noqa: F401
from app.auth.middleware import (
    auth_required,
    current_user,
    install_auth,
)
from app.auth.routes import auth_bp  # noqa: F401

__all__ = [
    'AUTH_MODE',
    'auth_bp',
    'auth_required',
    'current_user',
    'install_auth',
    'settings',
]
