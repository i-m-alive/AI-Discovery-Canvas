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
    M365 account, and this process holds the delegated token in memory
    (per backend run — deliberately not persisted in Phase 3/4).

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

ORGANIZER FALLBACK (app-only / client-credentials) — kept as a further
fallback for the rare case the id-construction approach doesn't apply
(e.g. an unexpected join-URL shape): looking the meeting up under the
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

Uses urllib only — no msal/msgraph-sdk dependency for two token POSTs
and three GETs.
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


# ── In-process token state (one facilitator, one backend run) ─────────
_state_lock = threading.Lock()
_state: dict = {}   # {device: {...}, token, exp, account (delegated); app_token, app_exp (app-only)}


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


def start_device_flow() -> dict:
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
        _state['device'] = {'code': j['device_code'],
                            'interval': int(j.get('interval', 5)),
                            'expires': time.time() + int(j.get('expires_in', 900))}
    return {'user_code': j['user_code'],
            'verification_uri': j.get('verification_uri', 'https://microsoft.com/devicelogin'),
            'message': j.get('message', '')}


def poll_device_flow() -> dict:
    """One poll tick. Returns {status: 'pending'|'connected'|'error', ...}."""
    with _state_lock:
        dev = _state.get('device')
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
            import base64
            payload = j.get('id_token', '').split('.')[1]
            payload += '=' * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            account = claims.get('preferred_username') or claims.get('name') or ''
        except Exception:
            pass
        with _state_lock:
            _state.pop('device', None)
            _state['token'] = j['access_token']
            _state['exp'] = time.time() + int(j.get('expires_in', 3600)) - 60
            _state['account'] = account
        log.info('[TEAMS] Graph sign-in complete (%s)', account or 'account hidden')
        return {'status': 'connected', 'account': account}
    err = j.get('error', '')
    if err in ('authorization_pending', 'slow_down'):
        return {'status': 'pending'}
    desc = j.get('error_description') or err or j.get('raw') or 'unknown error'
    return {'status': 'error', 'error': str(desc)[:400]}


def set_token(access_token: str, *, expires_in: int = 3300) -> dict:
    """Adopt a Graph access token the FRONTEND already acquired via its own
    MSAL session (the exact same Microsoft sign-in used to log into the
    app) — this is what makes Teams connection automatic instead of
    running a separate device-code flow. Verifies the token actually
    works by calling Graph /me (never trusts an unverified token) and
    returns the resolved account. Returns {'error': str} on any failure —
    never raises."""
    access_token = (access_token or '').strip()
    if not access_token:
        return {'error': 'no access token provided'}
    status, j = _http_json(f'{GRAPH}/me', token=access_token)
    if status != 200:
        return {'error': _graph_err('verifying the Microsoft sign-in', status, j)}
    account = j.get('userPrincipalName') or j.get('mail') or j.get('displayName') or ''
    with _state_lock:
        _state.pop('device', None)
        _state['token'] = access_token
        _state['exp'] = time.time() + max(60, int(expires_in)) - 30
        _state['account'] = account
    log.info('[TEAMS] Graph token adopted from the app\'s own Microsoft sign-in (%s)',
             account or 'account hidden')
    return {'account': account}


def connection_status() -> dict:
    with _state_lock:
        ok = bool(_state.get('token')) and time.time() < _state.get('exp', 0)
        return {'configured': is_configured(), 'connected': ok,
                'account': _state.get('account', '') if ok else ''}


def _token() -> Optional[str]:
    with _state_lock:
        if _state.get('token') and time.time() < _state.get('exp', 0):
            return _state['token']
    return None


def _app_token() -> Optional[str]:
    """Client-credentials (app-only) Graph token — the only way to look
    up ANOTHER user's (the meeting organizer's) onlineMeetings; delegated
    /me/onlineMeetings can never do that for an invited attendee. Returns
    None (never raises) if TEAMS_CLIENT_SECRET isn't set or the token
    request fails — callers treat that as "fallback unavailable" and keep
    behaving exactly as before this existed."""
    if not is_app_only_configured():
        return None
    with _state_lock:
        if _state.get('app_token') and time.time() < _state.get('app_exp', 0):
            return _state['app_token']
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
        _state['app_token'] = j['access_token']
        _state['app_exp'] = time.time() + int(j.get('expires_in', 3600)) - 60
    return _state['app_token']


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


def _resolve_meeting(join_url: str, organizer_email: Optional[str] = None) -> dict:
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
    tok = _token()
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
        return {'error': _graph_err('resolving the meeting', status, j)}
    return {'error': 'no meeting found for that join URL under this signed-in account '
                     '(you must be the organiser or an invited attendee)'}


# ── Transcript fetch ──────────────────────────────────────────────────
def fetch_transcript(join_url: str, organizer_email: Optional[str] = None) -> dict:
    """join_url → {lines: ['Speaker: text', ...], meeting_subject} or
    {error}. organizer_email (from the calendar event, when browsing —
    see list_recent_meetings) enables the app-only organizer fallback in
    _resolve_meeting when the signed-in user was only invited."""
    join_url = (join_url or '').strip()
    if not join_url.startswith('https://'):
        return {'error': 'paste the full Teams meeting join URL (https://teams.microsoft.com/...)'}

    res = _resolve_meeting(join_url, organizer_email)
    if 'error' in res:
        return res
    meeting, base, tok = res['meeting'], res['base'], res['token']
    mid = meeting['id']

    status, j = _http_json(f'{GRAPH}/{base}/onlineMeetings/{mid}/transcripts', token=tok)
    if status != 200:
        return {'error': _graph_err('listing transcripts', status, j)}
    transcripts = j.get('value') or []
    if not transcripts:
        return {'error': 'this meeting has no transcript yet — transcription must be turned on '
                         'in the meeting, and the transcript appears after it ends'}
    tid = transcripts[-1]['id']  # newest

    url = f'{GRAPH}/{base}/onlineMeetings/{mid}/transcripts/{tid}/content?$format=text/vtt'
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + tok})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            vtt = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:300]
        return {'error': _graph_err('downloading the transcript', e.code, {'raw': body})}
    except Exception as e:
        return {'error': f'network error downloading transcript: {e}'}

    lines = vtt_to_lines(vtt)
    if not lines:
        return {'error': 'transcript downloaded but contained no parseable lines'}
    return {'lines': lines, 'meeting_subject': meeting.get('subject') or 'Teams meeting'}


def list_recent_meetings(start: Optional[str] = None, end: Optional[str] = None,
                         q: Optional[str] = None) -> dict:
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
    tok = _token()
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
        return {'error': _graph_err('listing calendar meetings', status, j)}
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
            'start': (ev.get('start') or {}).get('dateTime') or '',
            'end': (ev.get('end') or {}).get('dateTime') or '',
            'join_url': join_url,
            'organizer': ((ev.get('organizer') or {}).get('emailAddress') or {}).get('address') or '',
        })
    return {'meetings': meetings}


def check_meeting_availability(join_url: str, organizer_email: Optional[str] = None) -> dict:
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
    res = _resolve_meeting((join_url or '').strip(), organizer_email)
    if 'error' in res:
        return {'has_transcript': False}
    meeting, base, tok = res['meeting'], res['base'], res['token']
    status_t, jt = _http_json(f'{GRAPH}/{base}/onlineMeetings/{meeting["id"]}/transcripts', token=tok)
    has_transcript = status_t == 200 and bool(jt.get('value'))
    return {'has_transcript': has_transcript}


def _graph_err(doing: str, status: int, j: dict) -> str:
    detail = ''
    err = j.get('error')
    if isinstance(err, dict):
        detail = err.get('message') or err.get('code') or ''
    elif isinstance(err, str):
        # HTTP 0 path (_http_json's network-exception branch) puts the
        # real underlying error here as a plain string, not a Graph-shaped
        # dict — without this branch it was silently dropped, leaving a
        # blank "(HTTP 0):" message with no clue what actually failed.
        detail = err
    detail = detail or j.get('error_description') or j.get('raw') or ''
    hint = ''
    # Confirmed live (error 3003): Graph's /me/onlineMeetings lookup-by-
    # join-URL only works for meetings the SIGNED-IN USER organized — an
    # invited attendee gets this 403 even with fully admin-consented
    # scopes. Give the accurate reason instead of the generic (and, in
    # this case, wrong) "missing admin consent" guess.
    if 'does not have access to lookup meeting' in str(detail).lower() or ' 3003' in f' {detail}':
        hint = (' — Microsoft Graph only lets the MEETING ORGANIZER look up an online meeting '
                'by its join URL. If someone else scheduled this meeting and you were just '
                'invited, delegated access to it isn\'t available under your account — try a '
                'meeting you scheduled yourself.')
    elif status in (401, 403):
        hint = (' — the app registration likely lacks admin-consented Graph permissions '
                '(OnlineMeetings.Read, OnlineMeetingTranscript.Read.All)')
    return f'Graph error while {doing} (HTTP {status}): {str(detail)[:300]}{hint}'


# ── VTT parsing (unit-testable offline) ───────────────────────────────
_VTT_SPEAKER = re.compile(r'<v\s+([^>]+)>(.*?)</v>', re.DOTALL)
_VTT_TS = re.compile(r'^\d{2}:\d{2}[:.\d]*\s+-->')


def vtt_to_lines(vtt: str) -> list[str]:
    """WebVTT → ['Speaker: text', ...]. Handles Teams' <v Speaker>text</v>
    cue payloads; consecutive lines from the same speaker are merged."""
    lines: list[str] = []
    for raw in (vtt or '').splitlines():
        raw = raw.strip()
        if not raw or raw == 'WEBVTT' or _VTT_TS.match(raw) or raw.isdigit() \
           or raw.startswith(('NOTE', 'STYLE', 'REGION')):
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
