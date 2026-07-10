"""
AuthProvider interface.

Every concrete provider (mock, Azure AD, future Okta/Google) implements
these four methods. The HTTP routes in `auth.routes` are the *only*
caller — they don't care which provider is mounted, so swapping
AUTH_MODE flips the whole login flow without touching the routes.

User dict shape (canonical across providers):

    {
      "id":            "<provider-specific stable id>",
      "name":          "Avinash Negi",
      "email":         "avinash.negi@navikenz.com",
      "role":          "user",
      "auth_provider": "mock" | "azure",
      "claims":        { ... raw provider claims, optional ... }
    }

Login result shape:

    {
      "user":     <user dict>,
      "session":  { "token": "<jwt>", "jti": "...", "exp": <unix ts> }
    }

Providers that need a redirect (Azure) return instead:

    {
      "redirect": "<authorize url>",
      "state":    "<opaque state value>"
    }
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class AuthProvider(ABC):
    name: str = 'abstract'

    @abstractmethod
    def login(self, **kwargs: Any) -> dict:
        """Initiate or complete login.

        Mock providers complete in one call (returns session).
        Redirect-based providers (Azure) return a `redirect` URL; the
        actual session is minted in `handle_callback`.
        """

    @abstractmethod
    def logout(self, token: str) -> dict:
        """Revoke the session. Return value carries optional provider
        hints — e.g. a Microsoft logout URL the frontend should redirect
        the user to so the browser-side AAD session is also cleared."""

    @abstractmethod
    def validate_token(self, token: str) -> Optional[dict]:
        """Return the session record for `token`, or None if invalid
        / expired / revoked."""

    @abstractmethod
    def get_current_user(self, token: str) -> Optional[dict]:
        """Return the user dict for `token`, or None. Convenience wrapper
        around validate_token() for routes that only need the user."""

    # Optional — override in providers that need a callback. Default
    # raises so a misrouted callback to a mock provider is obvious.
    def handle_callback(self, **kwargs: Any) -> dict:
        raise NotImplementedError(
            f'{self.name} provider does not implement an OAuth callback'
        )
