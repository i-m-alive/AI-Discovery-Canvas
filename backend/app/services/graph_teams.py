"""
Microsoft Teams meeting transcripts via Microsoft Graph — Phase 3/4.

REALITY CHECK (verified against the NaviCore/frd-generator source): what
NaviCore actually has is an Entra ID *app registration* (tenant id +
client id, used for sign-in) — there is NO Graph/Teams code and NO
client secret over there. This module builds the Teams integration ON
that app registration:

  * Auth: OAuth 2.0 **device-code flow** against that tenant/client id.
    No client secret needed (public-client flow) and no redirect URI —
    the right shape for a locally-run dev backend. The facilitator gets
    a code + https://microsoft.com/devicelogin link, signs in with their
    M365 account.

  * Transcript: resolve the meeting from its join URL, list its
    transcripts, download the VTT, parse to speaker lines.

WHAT THE AZURE ADMIN MUST STILL ENABLE on that app registration before
this works live (surfaced verbatim as AADSTS/Graph errors, never
guessed): (1) "Allow public client flows" = Yes; (2) delegated Graph
permissions OnlineMeetings.Read + OnlineMeetingTranscript.Read.All with
admin consent; (3) the meeting must have transcription turned on. Until
then every call returns a precise, actionable error string.

CONFIRMED LIVE: Graph's /me/onlineMeetings?$filter=JoinWebUrl lookup
(a SEARCH) only works for meetings the SIGNED-IN USER organized — error
"3003: User does not have access to lookup meeting" for anything else,
even with fully admin-consented scopes.

THE FIX for "invited, not organizer" (no admin action needed): Microsoft
Graph's GET-BY-EXACT-ID form, `GET /me/onlineMeetings/{id}`, is
explicitly documented to accept "both the organizer's and the invited
attendee's user token" under DELEGATED permission —
https://learn.microsoft.com/en-us/graph/api/onlinemeeting-get — the
restriction above is specific to the $filter SEARCH form, not to lookup
by id. The id itself never has to come from that restricted search: a
join URL already embeds its own thread id ("19:meeting_...@thread.v2")
and the organizer's object id (the `Oid` in its `context` query
param), and Graph's onlineMeeting id is a stable, publicly-documented
encoding of exactly those two values — `base64("1*{organizerOid}*0**{threadId}")`
(see _construct_meeting_id; construction verified against
https://practical365.com/teams-online-meetings-report/ and community
Graph tooling that computes the same value). So `_resolve_meeting` below
constructs the id locally (no extra Graph call) and fetches it via
`/me/onlineMeetings/{id}` with the SAME delegated token+scopes already
in use — no client secret, no Application permissions, no admin consent
beyond what's already granted.

CONFIRMED LIVE — this fix resolves the large majority of "invited, not
organizer" meetings, but NOT all of them; two distinct cases still 403
with the same 3003 via BOTH the search AND the constructed-id GET (not
a bug in the construction — verified same meeting, same signed-in user,
different outcome vs. a sibling meeting from the same organizer):
  1. The meeting's organizer is a shared/distribution mailbox (e.g.
     "companyname-all@..."), not a real user mailbox.
  2. A SPECIFIC occurrence of a recurring/repeated meeting where Graph's
     own participants.attendees list for that occurrence doesn't include
     the signed-in user — even though it appears on their calendar. A
     sibling occurrence of the exact same recurring meeting, same
     organizer, resolved fine — the difference lives in Microsoft's own
     per-occurrence attendance record, not anything this app controls.
Both remaining cases need the ORGANIZER FALLBACK below (app-only /
Azure admin setup) — there's no further delegated-only trick for them.

ORGANIZER FALLBACK (app-only / client-credentials) — the fallback for
the residual cases above: looking the meeting up under the
ORGANIZER's identity via app-only auth (_app_token/_resolve_meeting
below), which can query /users/{organizer}/onlineMeetings for ANY user
in the tenant. This needs THREE things this code cannot do by itself —
the app registration's OWNER (confirmed via `az ad app owner list`:
Vanshita Mediratta, NOT the tenant admin necessarily) or an actual
tenant admin must, in Azure Portal:
  1. App registrations > NaviCORE > Certificates & secrets > New client
     secret > put the VALUE in backend/.env as TEAMS_CLIENT_SECRET (or
     Key Vault secret 'teams-client-secret' — see secret_manager.py).
     NOTE: this makes the app a confidential client too — a real security-
     posture change worth the owner knowing about, not just rubber-stamping.
  2. NaviCORE > API permissions > Add a permission > Microsoft Graph >
     APPLICATION permissions (not delegated) > add OnlineMeetings.Read.All
     and OnlineMeetingTranscript.Read.All.
  3. Click "Grant admin consent for Navikenz" for those two — needs an
     actual Global/Cloud Application/Application Administrator role.
Until all three are done, is_app_only_configured() is False and this
fallback stays a clean no-op — fully additive, never a breaking change.

PERSISTENT CONNECTION (fixes real dev friction hit repeatedly: every
backend restart used to drop the Teams connection — DEBUG=false means no
auto-reload, and the connection lived ONLY in an in-process dict — forcing
a fresh interactive Microsoft sign-in before Teams features worked
again). The device-code flow's token response includes a `refresh_token`
(the `_SCOPES` list includes `offline_access`, which is what makes
Microsoft issue one) — this module persists it per-user in Postgres
(app.postgres.models.teams_connection) and `_token()` silently redeems it
for a fresh access token whenever the in-memory one is missing/expired,
so `connected: true` survives a restart transparently. The MSAL-bridge
path (`set_token`, used when the frontend's own Microsoft sign-in already
has a Graph-scoped token) has no refresh token to capture — the
browser-side MSAL cache handles its own refresh — so only the
device-code flow ever persists.

TOKEN-SCOPE INTROSPECTION (ported from NaviCore/frd-generator's
`teams_transcripts.py::_note_403` — same technique, confirmed valuable
there): on a 401/403, decode the CURRENT token's own `scp` claim (no
signature verification — diagnostic only, the token is never trusted for
auth based on this) and check it against the scopes this integration
needs. Distinguishes "your token doesn't carry the scope at all" (fix:
reconnect Teams — a token minted before a scope was added, or from a
different app, keeps failing until refreshed) from "the scope IS present
and Graph still denies" (a genuine per-meeting/tenant restriction, not a
token problem) — guessing "missing admin consent" for the former case
sent this project down the wrong path once already.

Uses urllib only — no msal/msgraph-sdk dependency.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.logging import log
from app.services import secret_manager

# macOS python.org builds ship without system CA certs wired into ssl —
# use certifi's bundle so HTTPS to login.microsoftonline.com / Graph
# verifies properly instead of failing with CERTIFICATE_VERIFY_FAILED.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:                                    # pragma: no cover
    _SSL_CTX = ssl.create_default_context()

GRAPH = 'https://graph.microsoft.com/v1.0'
# Only scopes ALREADY tenant-consented for this app registration (verified
# live via `az ad app permission list-grants` against NaviCORE). Meeting
# scheduling and Outlook mail import were tried and dropped — see
# app/routes/integrations.py's module docstring for why.
_SCOPES = 'openid profile offline_access OnlineMeetings.Read OnlineMeetingTranscript.Read.All Calendars.Read'
# Just the Graph (not openid/profile/offline_access) scopes — what
# _missing_scopes checks a token's own `scp` claim against.
_REQUIRED_GRAPH_SCOPES = ('Calendars.Read', 'OnlineMeetings.Read', 'OnlineMeetingTranscript.Read.All')


def _tenant() -> str:
    return (os.environ.get('TEAMS_TENANT_ID') or os.environ.get('AZURE_TENANT_ID') or '').strip()


def _client_id() -> str:
    return (os.environ.get('TEAMS_CLIENT_ID') or os.environ.get('AZURE_CLIENT_ID') or '').strip()


def is_configured() -> bool:
    return bool(_tenant() and _client_id())


def _client_secret() -> str:
    return secret_manager.get_secret('TEAMS_CLIENT_SECRET', env_fallback='TEAMS_CLIENT_SECRET') or ''


def is_app_only_configured() -> bool:
    """Whether the organizer-fallback (app-only) path is usable — needs
    the SAME app registration's client secret AND its Application
    permissions to already be admin-consented. This code can grant
    neither; see the module docstring for the exact Azure Portal steps
    the app owner/a tenant admin must do first."""
    return bool(is_configured() and _client_secret())


# ── Per-user in-process token state ────────────────────────────────────
# Keyed by owner_user_id (the Postgres users.id — same identity
# Projects/Workshops use), now that this app has real multiple users.
# {owner_user_id: {device:{...}, token, exp, account}}. The app-only
# (client-credentials) token is tenant-wide, not per-user, so it lives in
# its own separate dict.
_state_lock = threading.Lock()
_state: dict[int, dict] = {}
_app_state: dict = {}


def _user_state(owner_user_id: int) -> dict:
    return _state.setdefault(owner_user_id, {})


def _http_json(url: str, data: Optional[dict] = None, token: Optional[str] = None,
               form: bool = False) -> tuple[int, dict]:
    """Tiny urllib helper. Returns (status, parsed_json|{'raw': text})."""
    body = None
    headers = {'Accept': 'application/json'}
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            body = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
            text = resp.read().decode('utf-8', errors='replace')
            status = resp.status
    except urllib.error.HTTPError as e:
        text = e.read().decode('utf-8', errors='replace')
        status = e.code
    except Exception as e:
        log.warning('[TEAMS] network error calling %s (%s): %s', url, e.__class__.__name__, e)
        return 0, {'error': f'network error: {e.__class__.__name__}: {e}'}
    try:
        return status, json.loads(text)
    except Exception:
        return status, {'raw': text}


def start_device_flow(owner_user_id: int) -> dict:
    """Begin device-code auth. Returns {user_code, verification_uri,
    message} or {error}."""
    if not is_configured():
        return {'error': 'Teams is not configured: set TEAMS_TENANT_ID / TEAMS_CLIENT_ID '
                         '(or AZURE_TENANT_ID / AZURE_CLIENT_ID) in backend/.env'}
    status, j = _http_json(
        f'https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0/devicecode',
        {'client_id': _client_id(), 'scope': _SCOPES}, form=True)
    if status != 200 or 'device_code' not in j:
        err = j.get('error_description') or j.get('error') or j.get('raw') or 'unknown error'
        log.warning('[TEAMS] device flow start failed (%s): %s', status, str(err)[:300])
        return {'error': f'Microsoft sign-in could not start: {str(err)[:400]}'}
    with _state_lock:
        _user_state(owner_user_id)['device'] = {
            'code': j['device_code'],
            'interval': int(j.get('interval', 5)),
            'expires': time.time() + int(j.get('expires_in', 900)),
        }
    return {'user_code': j['user_code'],
            'verification_uri': j.get('verification_uri', 'https://microsoft.com/devicelogin'),
            'message': j.get('message', '')}


def poll_device_flow(owner_user_id: int) -> dict:
    """One poll tick. Returns {status: 'pending'|'connected'|'error', ...}."""
    with _state_lock:
        dev = _user_state(owner_user_id).get('device')
    if not dev:
        return {'status': 'error', 'error': 'no sign-in in progress — start again'}
    if time.time() > dev['expires']:
        return {'status': 'error', 'error': 'sign-in code expired — start again'}
    status, j = _http_json(
        f'https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0/token',
        {'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
         'client_id': _client_id(), 'device_code': dev['code']}, form=True)
    if 'access_token' in j:
        account = ''
        try:  # id_token claims are informational here — never trusted for auth
            payload = j.get('id_token', '').split('.')[1]
            payload += '=' * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            account = claims.get('preferred_username') or claims.get('name') or ''
        except Exception:
            pass
        with _state_lock:
            st = _user_state(owner_user_id)
            st.pop('device', None)
            st['token'] = j['access_token']
            st['exp'] = time.time() + int(j.get('expires_in', 3600)) - 60
            st['account'] = account
        refresh_token = j.get('refresh_token')
        if refresh_token:
            _persist_refresh_token(owner_user_id, refresh_token, account)
        log.info('[TEAMS] Graph sign-in complete (%s)', account or 'account hidden')
        return {'status': 'connected', 'account': account}
    err = j.get('error', '')
    if err in ('authorization_pending', 'slow_down'):
        return {'status': 'pending'}
    desc = j.get('error_description') or err or j.get('raw') or 'unknown error'
    return {'status': 'error', 'error': str(desc)[:400]}


def set_token(owner_user_id: int, access_token: str, *, expires_in: int = 3300) -> dict:
    """Adopt a Graph access token the FRONTEND already acquired via its own
    MSAL session (the exact same Microsoft sign-in used to log into the
    app) — this is what makes Teams connection automatic instead of
    running a separate device-code flow. Verifies the token actually
    works by calling Graph /me (never trusts an unverified token) and
    returns the resolved account. Returns {'error': str} on any failure —
    never raises.

    No refresh token is available from this path (MSAL keeps its own
    cache browser-side) — this connection does NOT survive a backend
    restart on its own; `_token()` falls back to a persisted device-flow
    connection (if any) or the frontend re-bridges automatically on its
    next Teams action."""
    access_token = (access_token or '').strip()
    if not access_token:
        return {'error': 'no access token provided'}
    status, j = _http_json(f'{GRAPH}/me', token=access_token)
    if status != 200:
        return {'error': _graph_err('verifying the Microsoft sign-in', status, j)}
    account = j.get('userPrincipalName') or j.get('mail') or j.get('displayName') or ''
    with _state_lock:
        st = _user_state(owner_user_id)
        st.pop('device', None)
        st['token'] = access_token
        st['exp'] = time.time() + max(60, int(expires_in)) - 30
        st['account'] = account
    log.info('[TEAMS] Graph token adopted from the app\'s own Microsoft sign-in (%s)',
             account or 'account hidden')
    return {'account': account}


def connection_status(owner_user_id: int) -> dict:
    """{'configured', 'connected', 'account'}. `connected` reflects the
    in-memory token OR a successfully-redeemed persisted connection —
    _token() attempts that redemption as a side effect, so this can
    report `connected: true` immediately after a backend restart without
    the caller needing to reconnect first."""
    tok = _token(owner_user_id)
    if not tok:
        return {'configured': is_configured(), 'connected': False, 'account': ''}
    with _state_lock:
        account = _user_state(owner_user_id).get('account', '')
    return {'configured': is_configured(), 'connected': True, 'account': account}


def _token(owner_user_id: int) -> Optional[str]:
    with _state_lock:
        st = _user_state(owner_user_id)
        if st.get('token') and time.time() < st.get('exp', 0):
            return st['token']
    # In-memory token missing/expired — try a persisted refresh token
    # before giving up. This is what makes a connection survive a
    # backend restart instead of requiring a fresh interactive sign-in.
    refresh_token, account = _load_persisted_connection(owner_user_id)
    if not refresh_token:
        return None
    j = _redeem_refresh_token(refresh_token)
    if 'access_token' not in j:
        log.info('[TEAMS] persisted refresh token redemption failed for user %s (%s) — reconnect required',
                 owner_user_id, j.get('error') or j.get('error_description') or 'unknown')
        return None
    with _state_lock:
        st = _user_state(owner_user_id)
        st['token'] = j['access_token']
        st['exp'] = time.time() + int(j.get('expires_in', 3600)) - 60
        st['account'] = account
    # Microsoft may rotate the refresh token on redemption — persist the
    # new one so the NEXT restart still works (an old, rotated-out token
    # would otherwise fail redemption once Microsoft's rotation policy
    # invalidates it).
    new_refresh = j.get('refresh_token')
    if new_refresh and new_refresh != refresh_token:
        _persist_refresh_token(owner_user_id, new_refresh, account)
    log.info('[TEAMS] connection silently restored from persisted refresh token (user %s)', owner_user_id)
    return j['access_token']


def _redeem_refresh_token(refresh_token: str) -> dict:
    """POST the OAuth refresh_token grant. Returns the parsed token
    response — may contain 'access_token'/'refresh_token' on success or
    'error'/'error_description' on failure (e.g. the refresh token was
    revoked or expired — Microsoft's typically ~90 days for confidential
    clients, longer for public clients with recent activity)."""
    status, j = _http_json(
        f'https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0/token',
        {'grant_type': 'refresh_token', 'client_id': _client_id(),
         'refresh_token': refresh_token, 'scope': _SCOPES}, form=True)
    return j


def _persist_refresh_token(owner_user_id: int, refresh_token: str, account: str) -> None:
    """Best-effort — a persistence failure must never fail the sign-in
    that just succeeded. Postgres down degrades to in-memory-only
    (the original behaviour), not a connection failure."""
    try:
        from app.postgres import session_scope
        from app.postgres.repositories import teams_connections as repo
        with session_scope() as s:
            if s is None:
                return
            repo.upsert(s, owner_user_id=owner_user_id, refresh_token=refresh_token, account=account)
        log.info('[TEAMS] refresh token persisted for user %s — connection will survive restarts',
                 owner_user_id)
    except Exception as e:
        log.info('[TEAMS] refresh-token persistence skipped (%s)', e.__class__.__name__)


def _load_persisted_connection(owner_user_id: int) -> tuple[Optional[str], str]:
    try:
        from app.postgres import session_scope
        from app.postgres.repositories import teams_connections as repo
        with session_scope() as s:
            if s is None:
                return None, ''
            row = repo.get(s, owner_user_id)
            if row is None:
                return None, ''
            return row.refresh_token, row.account or ''
    except Exception as e:
        log.info('[TEAMS] persisted-connection lookup skipped (%s)', e.__class__.__name__)
        return None, ''


def _app_token() -> Optional[str]:
    """Client-credentials (app-only) Graph token — the only way to look
    up ANOTHER user's (the meeting organizer's) onlineMeetings; delegated
    /me/onlineMeetings can never do that for an invited attendee. Tenant-
    wide, not per-user. Returns None (never raises) if TEAMS_CLIENT_SECRET
    isn't set or the token request fails — callers treat that as
    "fallback unavailable" and keep behaving exactly as before this
    existed."""
    if not is_app_only_configured():
        return None
    with _state_lock:
        if _app_state.get('token') and time.time() < _app_state.get('exp', 0):
            return _app_state['token']
    status, j = _http_json(
        f'https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0/token',
        {'grant_type': 'client_credentials', 'client_id': _client_id(),
         'client_secret': _client_secret(), 'scope': 'https://graph.microsoft.com/.default'},
        form=True)
    if status != 200 or 'access_token' not in j:
        log.warning('[TEAMS] app-only token request failed (%s): %s', status,
                    str(j.get('error_description') or j.get('error') or j.get('raw') or '')[:300])
        return None
    with _state_lock:
        _app_state['token'] = j['access_token']
        _app_state['exp'] = time.time() + int(j.get('expires_in', 3600)) - 60
    return _app_state['token']


_THREAD_ID_RE = re.compile(r'(19:meeting_[^/]+@thread\.v2)')
_ORGANIZER_OID_RE = re.compile(r'"Oid"\s*:\s*"([0-9a-fA-F-]+)"')


def _construct_meeting_id(join_url: str) -> Optional[str]:
    """Build the Graph onlineMeeting id directly from a join URL's own
    embedded thread id + organizer object id — a documented, stable
    encoding (see module docstring), not a Graph API call. Lets
    _resolve_meeting fetch a meeting BY ID (attendee-accessible under
    delegated permission) instead of only via the organizer-restricted
    $filter=JoinWebUrl search. Returns None if the join URL doesn't have
    the expected shape (falls back to the existing behaviour)."""
    decoded = urllib.parse.unquote(join_url or '')
    thread_m = _THREAD_ID_RE.search(decoded)
    oid_m = _ORGANIZER_OID_RE.search(decoded)
    if not thread_m or not oid_m:
        return None
    lookup = f'1*{oid_m.group(1)}*0**{thread_m.group(1)}'
    return base64.b64encode(lookup.encode('utf-8')).decode('ascii')


def _resolve_meeting(owner_user_id: int, join_url: str,
                     organizer_email: Optional[str] = None) -> dict:
    """Resolve one meeting by join URL, in order:
      1. The signed-in user's own delegated /me/onlineMeetings SEARCH
         (fast, one call) — works when they organized the meeting.
      2. GET /me/onlineMeetings/{id} with an id CONSTRUCTED from the join
         URL itself (see _construct_meeting_id) — Graph documents this
         form as attendee-accessible too, so this is what actually fixes
         "invited, not organizer" without any admin action.
      3. If an organizer_email plus app-only credentials are available,
         falls back to looking the meeting up under the ORGANIZER's
         identity via /users/{organizer}/onlineMeetings (app-only auth —
         see _app_token) — last resort, needs Azure admin setup.
    Returns {'meeting': {...}, 'base': 'me'|'users/<email>', 'token': str}
    on success (every subsequent call — transcripts, content — MUST reuse
    the same base+token), or {'error': str}."""
    tok = _token(owner_user_id)
    if not tok:
        return {'error': 'not signed in to Microsoft — connect Teams first'}
    # NOTE: `safe` must NOT include a literal space — Python's http.client
    # rejects any raw space/control character in a request URL
    # ("InvalidURL: URL can't contain control characters"). Only ' and =
    # stay unescaped (readable OData filter syntax); the spaces around
    # "JoinWebUrl eq" get percent-encoded to %20 like everything else.
    flt = urllib.parse.quote(f"JoinWebUrl eq '{join_url}'", safe="'=")
    status, j = _http_json(f'{GRAPH}/me/onlineMeetings?$filter={flt}', token=tok)
    if status == 200 and (j.get('value') or []):
        return {'meeting': j['value'][0], 'base': 'me', 'token': tok}

    constructed_id = _construct_meeting_id(join_url)
    if constructed_id:
        status_id, j_id = _http_json(f'{GRAPH}/me/onlineMeetings/{constructed_id}', token=tok)
        if status_id == 200 and j_id.get('id'):
            log.info('[TEAMS] resolved meeting via constructed id (attendee path, not organizer search)')
            return {'meeting': j_id, 'base': 'me', 'token': tok}

    if organizer_email:
        app_tok = _app_token()
        if app_tok:
            org_q = urllib.parse.quote(organizer_email)
            status2, j2 = _http_json(f'{GRAPH}/users/{org_q}/onlineMeetings?$filter={flt}', token=app_tok)
            if status2 == 200 and (j2.get('value') or []):
                return {'meeting': j2['value'][0], 'base': f'users/{org_q}', 'token': app_tok}
            if status2 != 200:
                return {'error': _graph_err('resolving the meeting (as organizer)', status2, j2)}

    if status != 200:
        return {'error': _graph_err('resolving the meeting', status, j, token=tok)}
    return {'error': 'no meeting found for that join URL under this signed-in account '
                     '(you must be the organiser or an invited attendee)'}


def _list_and_download_transcript(base: str, mid: str, tok: str) -> tuple[Optional[str], Optional[str]]:
    """List a meeting's transcripts under the given identity (base/tok)
    and download the newest one's content, trying both Accept formats.
    Returns (vtt_text, None) on success or (None, error). Both format
    attempts' outcomes are logged even when only the first is surfaced,
    so a 403 on both no longer silently hides what the SECOND attempt
    actually returned (previously err2 was discarded outright when it
    also failed — this made every dual-failure look identical to a
    single-format failure in the logs, with no way to tell them apart)."""
    status, j = _http_json(f'{GRAPH}/{base}/onlineMeetings/{mid}/transcripts', token=tok)
    if status != 200:
        return None, _graph_err('listing transcripts', status, j, token=tok)
    transcripts = j.get('value') or []
    if not transcripts:
        return None, ('this meeting has no transcript yet — transcription must be turned on '
                      'in the meeting, and the transcript appears after it ends')
    tid = transcripts[-1]['id']  # newest

    url = f'{GRAPH}/{base}/onlineMeetings/{mid}/transcripts/{tid}/content'
    vtt, err = _fetch_transcript_content(url, tok, accept='text/vtt')
    if err:
        # Always retry with the unattributed format on ANY content-fetch
        # failure, not only when we recognize the specific
        # SpeakerAttributionNotAllowed code — confirmed live that Graph
        # sometimes 403s this exact endpoint with a bare "UnknownError"
        # and no innerError.code at all, so string-matching a known code
        # can't catch every case. This costs one harmless extra call when
        # the failure truly isn't format-related (both attempts then fail
        # the same way); it silently RECOVERS the transcript (just
        # without speaker names) whenever it is.
        log.info('[TEAMS] text/vtt content fetch failed via %s (%s) — retrying unattributed format',
                 base, err[:150])
        vtt2, err2 = _fetch_transcript_content(url, tok, accept='application/vnd.microsoft.graph.transcript+text')
        if err2 is None:
            return vtt2, None
        log.info('[TEAMS] unattributed-format content fetch also failed via %s (%s)', base, err2[:150])
        return None, err
    return vtt, None


# ── Transcript fetch ──────────────────────────────────────────────────
def fetch_transcript(owner_user_id: int, join_url: str,
                     organizer_email: Optional[str] = None) -> dict:
    """join_url → {lines: ['Speaker: text', ...], meeting_subject} or
    {error}. organizer_email (from the calendar event, when browsing —
    see list_recent_meetings) enables the app-only organizer fallback in
    _resolve_meeting when the signed-in user was only invited, AND is
    reused here for a second fallback specific to transcript CONTENT
    (see below)."""
    join_url = (join_url or '').strip()
    if not join_url.startswith('https://'):
        return {'error': 'paste the full Teams meeting join URL (https://teams.microsoft.com/...)'}

    res = _resolve_meeting(owner_user_id, join_url, organizer_email)
    if 'error' in res:
        return res
    meeting, base, tok = res['meeting'], res['base'], res['token']
    mid = meeting['id']

    vtt, err = _list_and_download_transcript(base, mid, tok)
    if err and base == 'me' and organizer_email:
        # Confirmed live: transcript LISTING can succeed for an invited
        # attendee (resolved via the constructed-id path in
        # _resolve_meeting) while downloading the transcript CONTENT
        # still 403s with a bare "UnknownError" and no inner code — Graph
        # appears to gate content bytes more strictly than metadata, even
        # though the scopes on the token are identical for both calls.
        # Retry the whole list+download under the ORGANIZER's identity
        # (app-only) — the same fallback _resolve_meeting already uses
        # for meeting lookup. A clean no-op (same error returned) unless
        # TEAMS_CLIENT_SECRET + admin-consented Application permissions
        # are configured (see is_app_only_configured / module docstring).
        app_tok = _app_token()
        if app_tok:
            org_base = f'users/{urllib.parse.quote(organizer_email)}'
            log.info('[TEAMS] content download failed under attendee identity — retrying under organizer identity (%s)',
                     organizer_email)
            vtt2, err2 = _list_and_download_transcript(org_base, mid, app_tok)
            if err2 is None:
                vtt, err = vtt2, None
            else:
                log.info('[TEAMS] organizer-identity content fallback also failed: %s', err2[:200])
    if err:
        return {'error': err}

    lines = vtt_to_lines(vtt)
    if not lines:
        return {'error': 'transcript downloaded but contained no parseable lines'}
    return {'lines': lines, 'meeting_subject': meeting.get('subject') or 'Teams meeting'}


def _fetch_transcript_content(url: str, tok: str, *, accept: str) -> tuple[Optional[str], Optional[str]]:
    """GET one transcript content URL with the given Accept header (per
    current Graph docs — content-negotiated via Accept, not `$format`).
    Returns (content, None) on success or (None, error_message) — the
    error message is built via _graph_err from the ACTUAL parsed Graph
    error body (previously this path passed the raw, unparsed response
    text to _graph_err, which silently defeated its innerError.code
    extraction and made every content-download failure look like a
    generic, unidentifiable 403)."""
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + tok, 'Accept': accept})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            return resp.read().decode('utf-8', errors='replace'), None
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {'raw': body[:300]}
        return None, _graph_err('downloading the transcript', e.code, parsed, token=tok)
    except Exception as e:
        return None, f'network error downloading transcript: {e}'


def list_recent_meetings(owner_user_id: int, start: Optional[str] = None,
                         end: Optional[str] = None, q: Optional[str] = None) -> dict:
    """List the signed-in user's calendar meetings that have a Teams join
    link, newest first — so the facilitator can pick one instead of
    having to already know its join URL. `start`/`end` (ISO strings)
    default to the last 30 days / 1 day ahead; `q` filters by a
    case-insensitive substring match on subject, done here rather than
    via Graph's own `$search` (which Graph refuses to combine with
    `$orderby` on calendarView). Returns
    {meetings: [{subject, start, end, join_url, organizer}]} or {error}.
    organizer (email) is included so the caller can pass it back into
    fetch_transcript/check_meeting_availability — needed for the
    app-only organizer fallback on meetings you were only invited to."""
    tok = _token(owner_user_id)
    if not tok:
        return {'error': 'not signed in to Microsoft — connect Teams first'}
    now = datetime.now(timezone.utc)
    start_dt = start or (now - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%S')
    end_dt = end or (now + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S')
    params = urllib.parse.urlencode({
        'startDateTime': start_dt,
        'endDateTime': end_dt,
        '$select': 'subject,start,end,isOnlineMeeting,onlineMeeting,organizer',
        '$orderby': 'start/dateTime desc',
        '$top': '100',
    })
    status, j = _http_json(f'{GRAPH}/me/calendarView?{params}', token=tok)
    if status != 200:
        return {'error': _graph_err('listing calendar meetings', status, j, token=tok)}
    q_lower = (q or '').strip().lower()
    meetings = []
    for ev in (j.get('value') or []):
        om = ev.get('onlineMeeting') or {}
        join_url = om.get('joinUrl') or ''
        subject = ev.get('subject') or 'Untitled meeting'
        if not ev.get('isOnlineMeeting') or not join_url:
            continue
        if q_lower and q_lower not in subject.lower():
            continue
        meetings.append({
            'subject': subject,
            'start': _graph_datetime_to_iso(ev.get('start')),
            'end': _graph_datetime_to_iso(ev.get('end')),
            'join_url': join_url,
            'organizer': ((ev.get('organizer') or {}).get('emailAddress') or {}).get('address') or '',
        })
    return {'meetings': meetings}


def _graph_datetime_to_iso(field: Optional[dict]) -> str:
    """Graph's calendarView returns start/end as {dateTime, timeZone}
    with dateTime carrying NO offset — this app never sends a `Prefer:
    outlook.timezone` header, so timeZone is always 'UTC' by Graph's
    default, but the missing 'Z' makes JS's `new Date(iso)` parse it as
    local time instead of UTC, silently shifting the displayed meeting
    time by the browser's own UTC offset. Append 'Z' so the frontend
    parses it as UTC and converts it to the viewer's actual local time."""
    field = field or {}
    dt = field.get('dateTime') or ''
    tz = (field.get('timeZone') or '').upper()
    if dt and tz == 'UTC' and not dt.endswith('Z'):
        return dt + 'Z'
    return dt


def check_meeting_availability(owner_user_id: int, join_url: str,
                               organizer_email: Optional[str] = None) -> dict:
    """For the "has transcript" filter: resolve one meeting by join URL
    (via _resolve_meeting — same organizer fallback fetch_transcript
    uses) and check whether it has any transcripts. Deliberately called
    per-meeting, on demand, only for whatever page of results is
    currently on screen — never for a whole calendar window at once, to
    keep the extra Graph calls bounded. Any resolution failure (not
    found, not organizer and no app-only fallback available, etc.)
    reports has_transcript:False rather than an error — this is a filter
    chip, not the import action itself, so a per-row error would be
    noisy; the real error still surfaces when the user actually tries to
    import via fetch_transcript. (A "has recording" filter was
    deliberately dropped: it needs OnlineMeetingRecording.Read.All, a
    Microsoft "Admin"-classified permission — confirmed via the Graph
    service principal's own oauth2PermissionScopes metadata — which can
    never be self-granted by a non-admin user, unlike
    Calendars.ReadWrite/People.Read/Mail.Read which are "User"-classified
    and this tenant's consent policy allows users to self-grant.)
    Returns {has_transcript: bool}."""
    res = _resolve_meeting(owner_user_id, (join_url or '').strip(), organizer_email)
    if 'error' in res:
        return {'has_transcript': False}
    meeting, base, tok = res['meeting'], res['base'], res['token']
    status_t, jt = _http_json(f'{GRAPH}/{base}/onlineMeetings/{meeting["id"]}/transcripts', token=tok)
    has_transcript = status_t == 200 and bool(jt.get('value'))
    return {'has_transcript': has_transcript}


def _decode_token_claims(token: str) -> dict:
    """DIAGNOSTIC-ONLY decode of an access token's JWT payload — NO
    signature verification (the token is never trusted for auth based on
    this; it's only inspected to see which scopes it carries). Returns {}
    for opaque/non-JWT tokens (Microsoft sometimes issues those depending
    on token configuration)."""
    try:
        payload = (token or '').split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode('ascii')))
    except Exception:
        return {}


def _missing_scopes(token: Optional[str]) -> Optional[list[str]]:
    """Required Graph scopes absent from the token's own `scp` claim.
    None when the token isn't a decodable JWT (nothing can be asserted
    either way — stay silent rather than guess)."""
    if not token:
        return list(_REQUIRED_GRAPH_SCOPES)
    claims = _decode_token_claims(token)
    if not claims:
        return None
    have = {s.strip().lower() for s in (claims.get('scp') or '').split()}
    return [s for s in _REQUIRED_GRAPH_SCOPES if s.lower() not in have]


def _graph_err(doing: str, status: int, j: dict, *, token: Optional[str] = None) -> str:
    detail = ''
    inner_code = ''
    err = j.get('error')
    if isinstance(err, dict):
        detail = err.get('message') or err.get('code') or ''
        # innerError.code carries Graph's SPECIFIC reason (e.g.
        # 'GraphAccessToTranscriptsDisabled') — the top-level message is
        # often just a generic "Forbidden"/"UnknownError" wrapper, so
        # dropping innerError (as this used to) throws away the one
        # field that actually explains a 403.
        inner = err.get('innerError')
        if isinstance(inner, dict):
            inner_code = str(inner.get('code') or '').strip()
    elif isinstance(err, str):
        # HTTP 0 path (_http_json's network-exception branch) puts the
        # real underlying error here as a plain string, not a Graph-shaped
        # dict — without this branch it was silently dropped, leaving a
        # blank "(HTTP 0):" message with no clue what actually failed.
        detail = err
    detail = detail or j.get('error_description') or j.get('raw') or ''
    combined = f'{detail} {inner_code}'.lower()
    hint = ''
    # Token-scope introspection FIRST (ported from NaviCore/frd-generator
    # — see module docstring): if the CURRENT token is simply missing a
    # required scope, say so precisely instead of falling through to the
    # generic hints below, which would incorrectly suggest a per-meeting/
    # tenant restriction when the real fix is just "reconnect Teams".
    missing = _missing_scopes(token) if status in (401, 403) else None
    if missing:
        hint = (f' — your CURRENT session token does not carry the required scope(s): '
                f'{", ".join(missing)}. This happens with a token minted before these '
                f'permissions were added (or from a different sign-in) — disconnect and '
                f'reconnect Teams to get a token with the full scope set.')
    # Confirmed live (error 3003): Graph's /me/onlineMeetings lookup-by-
    # join-URL only works for meetings the SIGNED-IN USER organized — an
    # invited attendee gets this 403 even with fully admin-consented
    # scopes. Give the accurate reason instead of the generic (and, in
    # this case, wrong) "missing admin consent" guess.
    elif 'does not have access to lookup meeting' in combined or ' 3003' in f' {detail}':
        hint = (' — Microsoft Graph only lets the MEETING ORGANIZER look up an online meeting '
                'by its join URL. If someone else scheduled this meeting and you were just '
                'invited, delegated access to it isn\'t available under your account — try a '
                'meeting you scheduled yourself.')
    elif 'graphaccesstotranscriptsdisabled' in combined:
        hint = (' — a Teams ADMIN CENTER meeting policy ("Allow Cloud Recording"/transcript '
                'access via Graph) is turned off tenant-wide for transcripts. This is not an '
                'Entra API-permissions issue — it needs a Teams admin to enable Graph API access '
                'to transcripts in the Teams admin center meeting policy, not another app '
                'permission or consent.')
    elif 'speakerattributionnotallowed' in combined:
        # fetch_transcript already retries with the unattributed format
        # automatically when this happens — reaching this message means
        # BOTH formats failed, which shouldn't happen per Graph's own
        # docs ("the speaker-attribution setting never blocks the
        # unattributed format"), so this is worth a bug report if seen.
        hint = (' — tenant policy disallows speaker-attributed transcripts (text/vtt); the '
                'automatic retry with the unattributed format also failed, which is unexpected.')
    elif status in (401, 403) and not inner_code:
        # No specific inner code came back, and the token DOES carry the
        # required scopes (missing was falsy above) — genuinely
        # ambiguous. Since the SAME delegated scopes already work for
        # meetings you organize, a blanket "missing admin consent" guess
        # here would usually be WRONG — more likely this is meeting- or
        # tenant-policy-specific.
        hint = (' — your token DOES carry the required scopes (so this isn\'t a missing-'
                'permission issue); this is more likely a per-meeting or tenant policy '
                'restriction (e.g. the organizer or a Teams admin policy limits transcript '
                'access for attendees).')
    return f'Graph error while {doing} (HTTP {status}): {str(detail)[:300]}{(" [" + inner_code + "]") if inner_code else ""}{hint}'


# ── VTT parsing (unit-testable offline) ───────────────────────────────
_VTT_SPEAKER = re.compile(r'<v\s+([^>]+)>(.*?)</v>', re.DOTALL)
_VTT_TS = re.compile(r'^\d{2}:\d{2}[:.\d]*\s+-->')
# Teams VTT cue-identifier lines are sometimes a bare hex/uuid-ish token
# (e.g. "6cb64f13-.../19-1"), not always the plain sequential digits
# `raw.isdigit()` catches — without this, such a line falls through and
# is misread as a (speakerless) transcript line of garbage text.
_CUE_ID_RE = re.compile(r'^[0-9a-fA-F-]{8,}(/[\d-]+)?$')


def vtt_to_lines(vtt: str) -> list[str]:
    """WebVTT → ['Speaker: text', ...]. Handles Teams' <v Speaker>text</v>
    cue payloads; consecutive lines from the same speaker are merged."""
    lines: list[str] = []
    for raw in (vtt or '').splitlines():
        raw = raw.strip()
        if not raw or raw == 'WEBVTT' or _VTT_TS.match(raw) or raw.isdigit() \
           or _CUE_ID_RE.match(raw) or raw.startswith(('NOTE', 'STYLE', 'REGION')):
            continue
        m = _VTT_SPEAKER.search(raw)
        if m:
            speaker, text = m.group(1).strip(), m.group(2).strip()
        else:
            speaker, text = '', re.sub(r'<[^>]+>', '', raw).strip()
        if not text:
            continue
        entry = f'{speaker}: {text}' if speaker else text
        if lines and speaker and lines[-1].startswith(speaker + ': '):
            lines[-1] += ' ' + text
        else:
            lines.append(entry)
    return lines
