"""
Provider factory.

`get_provider()` returns the AuthProvider instance for the currently
configured AUTH_MODE. Cached for the process lifetime — providers are
expected to be stateless beyond their (lazily initialised) clients.
"""

from __future__ import annotations

from typing import Optional

from app.auth.config import settings
from app.auth.providers.base import AuthProvider
from app.auth.providers.mock import MockAuthProvider
from app.auth.providers.azure_ad import AzureADAuthProvider


_INSTANCE: Optional[AuthProvider] = None


def get_provider() -> AuthProvider:
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    if settings.is_azure:
        _INSTANCE = AzureADAuthProvider(settings)
    else:
        _INSTANCE = MockAuthProvider(settings)
    return _INSTANCE


def reset_provider() -> None:
    """For tests — drop the cached provider so the next get_provider()
    call re-reads the current settings."""
    global _INSTANCE
    _INSTANCE = None


__all__ = ['AuthProvider', 'MockAuthProvider', 'AzureADAuthProvider',
           'get_provider', 'reset_provider']
