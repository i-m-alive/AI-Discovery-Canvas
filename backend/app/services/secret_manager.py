"""
Centralised secret management.

Single source of truth for every Key-Vault-backed credential the backend
touches. ADAPTATION NOTE (ai-discovery-canvas): AWS Bedrock credentials
(chat via llm_service.py, embeddings via rag/embedder.py) are NOT resolved
here — boto3 reads AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN
directly via its own default credential chain. What's left in this map:

    Logical name (env var name)   Key Vault secret name
    ───────────────────────────   ───────────────────────────
    NEO4J_PASSWORD                neo4j-password

Resolution order (per `get(logical_name)`):

    1. In-memory cache  (one round trip per secret per process)
    2. Azure Key Vault  (if configured + reachable)
    3. Environment var  (the `env_fallback` argument — local dev escape hatch)
    4. Default value    (only for non-secret-bearing scenarios)
    5. Raise            (when `required=True` is set by the caller)

Auth uses azure.identity.DefaultAzureCredential, so the same code path
works for:

  * Managed Identity in Azure App Service / Container Apps / AKS
  * Azure CLI session locally (`az login`)
  * Service principal env vars (AZURE_CLIENT_ID / AZURE_TENANT_ID /
    AZURE_CLIENT_SECRET) for CI / docker-compose
  * Visual Studio Code, Visual Studio, PowerShell ... — the credential
    chain tries each in turn and uses the first that succeeds

Design contracts
~~~~~~~~~~~~~~~~

* SECRET VALUES MUST NEVER BE LOGGED. The module only logs the LOGICAL
  name and the resolution source (`'keyvault' | 'env' | 'default'`).
  Network errors from Key Vault are logged with the error TYPE only,
  not the full exception (which can echo secret-bearing URLs).

* IMPORT MUST NEVER FAIL the rest of the app. If azure-identity or
  azure-keyvault-secrets aren't installed (slim image, dev box with a
  partial venv), the module degrades to "env-only mode" and logs a
  warning at first `get()` call.

* SECRETS MUST NEVER LEAK INTO API RESPONSES. The only public surface
  callers use is `get_secret(...)` -> str. The internal cache + client
  are module-private.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Optional


log = logging.getLogger('app.secret_manager')


# ─────────────────────────────────────────────────────────────────────
# Configuration — Key Vault URL + the logical-name ↔ KV-name map.
#
# Override the vault URL via env (KEY_VAULT_URL or AZURE_KEY_VAULT_URL).
# Defaults to the org vault per the deployment runbook so a fresh dev
# checkout works without extra env wiring after `az login`.
# ─────────────────────────────────────────────────────────────────────

DEFAULT_VAULT_URL = 'https://navicore.vault.azure.net/'


def _resolve_vault_url() -> str:
    return (
        os.environ.get('KEY_VAULT_URL')
        or os.environ.get('AZURE_KEY_VAULT_URL')
        or DEFAULT_VAULT_URL
    ).strip()


# Mapping from the LOGICAL secret name (matches the env var the codebase
# already uses) to the SECRET NAME stored in Key Vault. Adding a new
# secret = one line here + one `get_secret(...)` call at the consumer.
SECRET_NAME_MAP: Dict[str, str] = {
    'POSTGRES_PASSWORD':     'postgres-password',
    'NEO4J_PASSWORD':        'neo4j-password',
    # OAuth client secrets for per-user source connections (one per provider).
    # client_id is NON-secret config (env); only the secret lives in KV.
    'GITHUB_OAUTH_CLIENT_SECRET': 'oauth-client-github-secret',
    'AZURE_DEVOPS_OAUTH_CLIENT_SECRET': 'oauth-client-azure-devops-secret',
    # App-only (client credentials) auth for the SAME NaviCORE app
    # registration used for delegated sign-in — lets graph_teams.py look
    # up a meeting under its ORGANIZER's identity when the signed-in
    # user was only invited (delegated /me/onlineMeetings can never do
    # this). Requires the app OWNER to add a client secret AND an
    # Application-permission admin consent grant; see graph_teams.py's
    # module docstring for the exact Azure Portal steps.
    'TEAMS_CLIENT_SECRET': 'teams-client-secret',
}


# Toggle to disable Key Vault entirely (force env-only). Useful for
# offline dev or tests that don't want to talk to Azure.
def _kv_disabled() -> bool:
    return os.environ.get('DISABLE_KEY_VAULT', '').lower() in ('1', 'true', 'yes')


# ─────────────────────────────────────────────────────────────────────
# Lazy SecretClient bootstrap.
# Created on first use, shared by every get() call across threads.
# ─────────────────────────────────────────────────────────────────────

_client_lock = threading.Lock()
_client = None                # SecretClient | None | False (False = init failed)
_cache: Dict[str, str] = {}
_cache_lock = threading.Lock()
_source: Dict[str, str] = {}  # logical_name -> 'keyvault' | 'env' | 'default'


def _build_client():
    """Return a configured SecretClient or None on any failure.
    NEVER raises — callers must handle None as 'KV unavailable'."""
    if _kv_disabled():
        log.info('[SECRETS] Key Vault disabled via DISABLE_KEY_VAULT=1 - env-only mode')
        return None
    try:
        from azure.identity        import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as e:
        log.warning('[SECRETS] azure-identity / azure-keyvault-secrets not installed '
                    '(%s) - falling back to env vars only', e.__class__.__name__)
        return None

    vault_url = _resolve_vault_url()
    if not vault_url:
        log.warning('[SECRETS] no KEY_VAULT_URL configured - env-only mode')
        return None

    try:
        # exclude_interactive_browser keeps the credential chain quiet in
        # headless containers. Managed Identity / az-cli / SP env vars
        # remain enabled.
        cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        client = SecretClient(vault_url=vault_url, credential=cred)
        log.info('[SECRETS] Key Vault client ready (vault=%s)', vault_url)
        return client
    except Exception as e:
        # Auth wiring problem (no managed identity, no az login, no SP).
        # We log the TYPE only; the message can echo URLs / IDs which we
        # don't want in operator logs.
        log.warning('[SECRETS] Key Vault client init failed (%s) - falling back to env vars',
                    e.__class__.__name__)
        return None


def _get_client():
    global _client
    if _client is False:
        return None
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            built = _build_client()
            _client = built if built is not None else False
        return _client or None


def _kv_fetch(secret_name: str) -> Optional[str]:
    """Read a single secret from Key Vault. Returns None on any failure.
    No secret VALUE is ever logged."""
    client = _get_client()
    if client is None:
        return None
    try:
        bundle = client.get_secret(secret_name)
        value = bundle.value if bundle is not None else None
        # Strip surrounding whitespace/newlines — secrets saved via the
        # Azure Portal or CLI sometimes pick up a trailing newline, which
        # quote_plus() would encode into the connection URL and cause auth
        # failures even though the raw text matches the expected password.
        return value.strip() if value else None
    except Exception as e:
        # Distinguish "not found" (operator hasn't created the secret
        # yet) from "network / auth" so the log is actionable. The
        # azure-keyvault-secrets SDK raises ResourceNotFoundError for
        # the former; everything else is logged generically.
        cls = e.__class__.__name__
        if cls == 'ResourceNotFoundError':
            log.warning("[SECRETS] '%s' not found in Key Vault - will try env fallback",
                        secret_name)
        else:
            log.warning("[SECRETS] Key Vault read for '%s' failed (%s) - falling back to env",
                        secret_name, cls)
        return None


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

class SecretUnavailable(RuntimeError):
    """Raised by get_secret(..., required=True) when no source resolved.
    The error message intentionally OMITS the secret value and only
    names the logical secret — safe to surface in startup logs."""


def get_secret(logical_name: str, *,
               env_fallback: Optional[str] = None,
               default: Optional[str] = None,
               required: bool = False) -> Optional[str]:
    """Resolve a secret. See module docstring for the resolution order.

    `logical_name` is the canonical name (matches the env var the rest
    of the codebase uses, e.g. 'POSTGRES_PASSWORD'). The Key Vault
    secret name is looked up via SECRET_NAME_MAP; if not mapped, the
    logical name itself is used (lowercased, underscores -> dashes).

    `env_fallback` is the env var name to fall back to when Key Vault
    is unavailable. Defaults to `logical_name`.
    """
    # 1. Cache
    with _cache_lock:
        if logical_name in _cache:
            return _cache[logical_name]

    # 2. Key Vault
    kv_secret_name = SECRET_NAME_MAP.get(
        logical_name,
        logical_name.lower().replace('_', '-'),
    )
    value = _kv_fetch(kv_secret_name)
    source = 'keyvault' if value else None

    # 3. Env fallback
    if not value:
        env_var = env_fallback or logical_name
        raw = os.environ.get(env_var, '')
        if raw and raw.strip():
            value = raw.strip()
            source = 'env'

    # 4. Default
    if value is None and default is not None:
        value = default
        source = 'default'

    # 5. Required-and-missing -> raise
    if not value:
        if required:
            raise SecretUnavailable(
                f"Secret '{logical_name}' is unavailable in Key Vault "
                f"({kv_secret_name}) and no fallback was supplied."
            )
        return None

    with _cache_lock:
        _cache[logical_name] = value
        _source[logical_name] = source
    log.info("[SECRETS] resolved '%s' from %s", logical_name, source)
    return value


# ─────────────────────────────────────────────────────────────────────
# Per-source credentials (private-repo PATs, Jira/Atlassian API tokens, …).
#
# A workflow source node persists ONLY a `credential_ref` — the Key Vault
# secret NAME, never the token. get_secret_by_name() resolves that ref at
# fetch time. Because the name comes from caller-controlled workflow JSON,
# it is constrained to a reserved prefix so it can NEVER be used to read an
# unrelated secret (e.g. 'postgres-password' or 'neo4j-password').
# ─────────────────────────────────────────────────────────────────────

SRC_CRED_PREFIX = 'src-cred-'


def get_secret_by_name(ref: str, *, env_fallback: Optional[str] = None) -> Optional[str]:
    """Resolve a per-SOURCE credential by its Key Vault secret NAME.

    UNLIKE get_secret (logical name -> mapped KV name), the KV secret name
    here is taken DIRECTLY from a source node's caller-supplied
    `credential_ref`. To stop that name from being pointed at an unrelated
    secret, it MUST start with SRC_CRED_PREFIX ('src-cred-'); anything else
    is refused (logged by NAME, never fetched) and returns None.

    Inherits secret_manager's contracts:
      * NO-LOG     — only the ref NAME and resolution source are logged,
                     never the secret value.
      * ENV-FALLBACK — when Key Vault is unavailable, falls back to an env
                     var derived from the ref (UPPER, '-' -> '_'), e.g.
                     'src-cred-ado-rd-hp' -> 'SRC_CRED_ADO_RD_HP'. Dev only.
      * PER-PROCESS CACHE — resolved values cached by ref in the shared
                     cache (prefix-namespaced, so no collision with logical
                     names).

    Returns the secret string (whitespace-stripped) or None when the ref is
    empty, out-of-prefix, or unresolved. NEVER raises — the embed path maps
    None -> ledger status 'cred_missing' and skips the source.
    """
    ref = (ref or '').strip()
    if not ref:
        return None
    if not ref.startswith(SRC_CRED_PREFIX):
        # The guard that stops a workflow credential_ref from reading, say,
        # 'postgres-password'. Refuse rather than fetch. Log the NAME only.
        log.warning("[SECRETS] credential_ref '%s' rejected: must start with '%s'",
                    ref, SRC_CRED_PREFIX)
        return None

    # 1. Cache (shared with get_secret; refs are prefix-namespaced).
    with _cache_lock:
        if ref in _cache:
            return _cache[ref]

    # 2. Key Vault — the ref IS the (prefix-checked) KV secret name.
    value = _kv_fetch(ref)
    source = 'keyvault' if value else None

    # 3. Env fallback (local dev): src-cred-ado-rd-hp -> SRC_CRED_ADO_RD_HP
    if not value:
        env_var = env_fallback or ref.upper().replace('-', '_')
        raw = os.environ.get(env_var, '')
        if raw and raw.strip():
            value = raw.strip()
            source = 'env'

    if not value:
        # Not in KV, no env fallback. Caller maps this to 'cred_missing'.
        log.info("[SECRETS] credential_ref '%s' unresolved "
                 "(no Key Vault secret, no env fallback)", ref)
        return None

    with _cache_lock:
        _cache[ref] = value
        _source[ref] = source
    log.info("[SECRETS] resolved credential_ref '%s' from %s", ref, source)
    return value


def set_secret_by_name(ref: str, value: str) -> bool:
    """WRITE a per-SOURCE credential to Key Vault by name. The ONLY writer is
    the OAuth callback, which stores the token JSON after a successful exchange.

    Mirrors get_secret_by_name's guard: the name MUST start with SRC_CRED_PREFIX
    ('src-cred-') so this can never overwrite an app secret (e.g.
    'postgres-password'). NEVER logs the value — only the ref NAME and outcome.
    Updates the in-process cache on success so an immediate read is consistent.

    Returns True on success, False if refused (bad prefix) or Key Vault is
    unavailable. NEVER raises."""
    ref = (ref or '').strip()
    if not ref or not ref.startswith(SRC_CRED_PREFIX):
        log.warning("[SECRETS] set_secret_by_name '%s' rejected: must start with '%s'",
                    ref, SRC_CRED_PREFIX)
        return False
    if value is None or value == '':
        log.warning("[SECRETS] set_secret_by_name '%s' rejected: empty value", ref)
        return False
    client = _get_client()
    if client is None:
        # No Key Vault (dev / env-only mode). We do NOT silently persist the
        # token anywhere unencrypted — caller learns it could not be stored.
        log.warning("[SECRETS] set_secret_by_name '%s': Key Vault unavailable — not stored", ref)
        return False
    try:
        client.set_secret(ref, value)          # value NEVER logged
    except Exception as e:
        log.warning("[SECRETS] set_secret_by_name '%s' failed (%s)", ref, e.__class__.__name__)
        return False
    with _cache_lock:
        _cache[ref] = value
        _source[ref] = 'keyvault'
    log.info("[SECRETS] stored credential_ref '%s' in Key Vault", ref)
    return True


def preload(logical_names: Optional[list] = None) -> Dict[str, str]:
    """Warm the cache with the supplied names. Returns a
    {logical_name -> source} map (never the values) so the boot log can
    say 'these came from KV, these came from env'.

    Failures don't raise; the dependent subsystem will surface the
    missing secret later with a precise error message.
    """
    names = logical_names or list(SECRET_NAME_MAP.keys())
    out: Dict[str, str] = {}
    for n in names:
        try:
            v = get_secret(n)
            out[n] = _source.get(n, 'missing') if v else 'missing'
        except Exception as e:
            log.warning("[SECRETS] preload of '%s' failed (%s)", n, e.__class__.__name__)
            out[n] = 'error'
    return out


def source_of(logical_name: str) -> str:
    """Return where a previously-resolved secret came from. Useful for
    `/health` style endpoints to show 'NEO4J_PASSWORD: keyvault'
    without ever exposing the value."""
    return _source.get(logical_name, 'unresolved')


def invalidate(logical_name: Optional[str] = None) -> None:
    """Drop one (or all) cached secrets. Forces a re-fetch on next get().
    Use after a rotation - or in tests that swap env vars."""
    with _cache_lock:
        if logical_name is None:
            _cache.clear()
            _source.clear()
        else:
            _cache.pop(logical_name, None)
            _source.pop(logical_name, None)


def vault_url() -> str:
    """Public accessor — safe to log."""
    return _resolve_vault_url()


def has_keyvault_client() -> bool:
    """Cheap, never-raising check for diagnostics endpoints."""
    return _get_client() is not None


def cached_names() -> list:
    """Names of currently-cached secrets. Values NEVER returned."""
    with _cache_lock:
        return list(_cache.keys())


__all__ = [
    'SECRET_NAME_MAP',
    'SRC_CRED_PREFIX',
    'SecretUnavailable',
    'cached_names',
    'get_secret',
    'get_secret_by_name',
    'set_secret_by_name',
    'has_keyvault_client',
    'invalidate',
    'preload',
    'source_of',
    'vault_url',
]
