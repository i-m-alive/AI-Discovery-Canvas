'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiGet, apiPost } from '../../lib/api';
import { Icon } from '../../lib/icons';
import { getMsalInstance } from '../../lib/msalConfig';

// Browse the signed-in user's Teams meetings and import a transcript as
// a workshop source — the During-Workshop capture path (replaces live
// transcription by explicit product decision). The whole backend
// pipeline already exists (routes/integrations.py + graph_teams.py);
// this is the clean React rebuild of the vanilla-JS flow the old canvas
// had: status check → automatic MSAL token adoption → device-code
// fallback → calendar list → per-meeting transcript availability →
// POST /api/agents/import-transcript (which registers the transcript as
// a normal prepare-doc: status pills, RAG, context cache all included).

const TEAMS_SCOPES = ['OnlineMeetings.Read', 'OnlineMeetingTranscript.Read.All', 'Calendars.Read'];

function fmtWhen(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString(undefined, {
      weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    });
  } catch { return ''; }
}

function fmtDuration(startIso, endIso) {
  if (!startIso || !endIso) return '';
  const mins = Math.round((new Date(endIso) - new Date(startIso)) / 60000);
  if (!Number.isFinite(mins) || mins <= 0) return '';
  return mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60 ? `${mins % 60}m` : ''}`.trim() : `${mins} min`;
}

export default function TeamsImportModal({ workshopId, onClose, onImported }) {
  // phase: checking | connect | device | loading | list | error
  const [phase, setPhase] = useState('checking');
  const [error, setError] = useState('');
  const [device, setDevice] = useState(null);          // {verification_uri, user_code}
  const [meetings, setMeetings] = useState([]);
  const [avail, setAvail] = useState({});              // join_url -> {has_transcript}
  const [importing, setImporting] = useState(null);    // join_url in flight
  const [done, setDone] = useState({});                // join_url -> {ok, name, already}
  const disposed = useRef(false);
  useEffect(() => () => { disposed.current = true; }, []);

  const loadMeetings = useCallback(async () => {
    setPhase('loading');
    try {
      const j = await apiGet('/api/integrations/teams/meetings');
      if (disposed.current) return;
      if (!j || !j.ok) { setError((j && j.error) || 'could not load your calendar'); setPhase('error'); return; }
      const list = j.meetings || [];
      setMeetings(list);
      setPhase('list');
      // Transcript availability for the visible page only — the backend
      // checks exactly the meetings we send.
      if (list.length) {
        try {
          const a = await apiPost('/api/integrations/teams/meetings/availability', {
            meetings: list.map((m) => ({ join_url: m.join_url, organizer: m.organizer })),
          });
          if (!disposed.current && a && a.ok) setAvail(a.results || {});
        } catch { /* availability is a nice-to-have badge */ }
      }
    } catch (e) {
      if (!disposed.current) { setError(e.message || 'could not load your calendar'); setPhase('error'); }
    }
  }, []);

  // Connect gate on open: reuse the app's own Microsoft session when
  // possible (silent token), else offer device-code sign-in.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let st = null;
      try { st = await apiGet('/api/integrations/teams/status'); } catch { /* fall through */ }
      if (cancelled) return;
      if (!st) { setError('backend unreachable'); setPhase('error'); return; }
      if (!st.configured) {
        setError('Teams is not configured — set TEAMS_TENANT_ID / TEAMS_CLIENT_ID in backend/.env.');
        setPhase('error');
        return;
      }
      if (st.connected) { loadMeetings(); return; }
      // Automatic: adopt a Graph token from the frontend's MSAL session.
      const msal = getMsalInstance();
      if (msal) {
        try {
          await msal.ready;
          const accounts = msal.instance.getAllAccounts();
          if (accounts.length) {
            let result;
            try {
              result = await msal.instance.acquireTokenSilent({ scopes: TEAMS_SCOPES, account: accounts[0] });
            } catch {
              result = await msal.instance.acquireTokenPopup({ scopes: TEAMS_SCOPES, account: accounts[0] });
            }
            const expiresIn = result.expiresOn
              ? Math.max(60, Math.floor((result.expiresOn.getTime() - Date.now()) / 1000)) : 3300;
            const j = await apiPost('/api/integrations/teams/connect-token',
              { access_token: result.accessToken, expires_in: expiresIn });
            if (cancelled) return;
            if (j && j.ok) { loadMeetings(); return; }
          }
        } catch { /* fall through to manual */ }
      }
      if (!cancelled) setPhase('connect');
    })();
    return () => { cancelled = true; };
  }, [loadMeetings]);

  async function startDeviceFlow() {
    setError('');
    try {
      const j = await apiPost('/api/integrations/teams/connect', {});
      if (!j || !j.ok) { setError((j && j.error) || 'could not start Microsoft sign-in'); return; }
      setDevice({ verification_uri: j.verification_uri, user_code: j.user_code });
      setPhase('device');
      for (let tick = 0; tick < 90 && !disposed.current; tick++) {
        await new Promise((r) => setTimeout(r, 5000));
        let p = null;
        try { p = await apiPost('/api/integrations/teams/poll', {}); } catch { /* keep polling */ }
        if (disposed.current) return;
        if (p && p.status === 'connected') { loadMeetings(); return; }
        if (p && p.status === 'error') { setError(p.error || 'sign-in failed'); setPhase('connect'); return; }
      }
    } catch (e) {
      setError(e.message || 'could not start Microsoft sign-in');
    }
  }

  async function importOne(mtg) {
    setImporting(mtg.join_url);
    setError('');
    try {
      const j = await apiPost('/api/agents/import-transcript', {
        workshop_id: workshopId, join_url: mtg.join_url,
        organizer: mtg.organizer || undefined, subject: mtg.subject || undefined,
      });
      if (disposed.current) return;
      if (!j || !j.ok) {
        setDone((d) => ({ ...d, [mtg.join_url]: { ok: false, error: (j && j.error) || 'import failed' } }));
        return;
      }
      setDone((d) => ({ ...d, [mtg.join_url]: { ok: true, name: j.doc.name, already: !!j.already_imported } }));
      onImported && onImported(j.doc);
    } catch (e) {
      setDone((d) => ({ ...d, [mtg.join_url]: { ok: false, error: e.message || 'import failed' } }));
    } finally {
      if (!disposed.current) setImporting(null);
    }
  }

  return (
    <div className="pw-modal-backdrop" onClick={onClose}>
      <div className="dw-teams-modal" onClick={(e) => e.stopPropagation()}>
        <div className="dw-teams-head">
          <span className="pw-ic pw-ic-accent"><Icon name="users" /></span>
          <div>
            <div className="pw-h3">Import a Teams transcript</div>
            <div className="pw-sub">Browse your meetings, pick one with a transcript, and it becomes a workshop source.</div>
          </div>
          <button className="pw-view-btn" onClick={onClose} title="Close"><Icon name="x" /></button>
        </div>

        {phase === 'checking' && <div className="pw-empty">Checking your Teams connection…</div>}

        {phase === 'connect' && (
          <div className="dw-teams-connect">
            <p>Your Microsoft Teams account isn't connected yet. Sign in once and this app can browse
              your meetings and download their transcripts.</p>
            {error && <div className="app-error pw-err">⚠ {error}</div>}
            <button className="btn solid" onClick={startDeviceFlow}><Icon name="users" />Sign in with Microsoft</button>
          </div>
        )}

        {phase === 'device' && device && (
          <div className="dw-teams-connect">
            <p>Open <b>{device.verification_uri}</b> and enter this code, then sign in with your
              Microsoft 365 account:</p>
            <div className="dw-teams-code">{device.user_code}</div>
            <div className="pw-sub">Waiting for sign-in…</div>
          </div>
        )}

        {phase === 'loading' && <div className="pw-empty">Loading your Teams meetings…</div>}

        {phase === 'error' && (
          <div className="dw-teams-connect">
            <div className="app-error pw-err">⚠ {error}</div>
            <button className="btn" onClick={loadMeetings}>Try again</button>
          </div>
        )}

        {phase === 'list' && (
          meetings.length === 0 ? (
            <div className="pw-empty">No Teams meetings found in your recent calendar window.</div>
          ) : (
            <ul className="dw-teams-list">
              {meetings.map((m) => {
                const a = avail[m.join_url];
                const hasTr = a ? !!a.has_transcript : null;
                const st = done[m.join_url];
                const dur = fmtDuration(m.start, m.end);
                return (
                  <li key={m.join_url} className="dw-teams-item">
                    <span className="pw-source-icon" style={{ background: '#efedfd', color: '#6d5ce8' }}>
                      <Icon name="clock" />
                    </span>
                    <div className="dw-teams-main">
                      <div className="dw-teams-subj">{m.subject || 'Untitled meeting'}</div>
                      <div className="dw-teams-meta">
                        {fmtWhen(m.start)}{dur ? ` · ${dur}` : ''}{m.organizer ? ` · ${m.organizer}` : ''}
                      </div>
                      {st && !st.ok && <div className="dw-teams-err">⚠ {st.error}</div>}
                    </div>
                    {hasTr === true && <span className="pw-pill pw-pill-ingested">Transcript</span>}
                    {hasTr === false && <span className="pw-pill pw-pill-failed">No transcript</span>}
                    {st && st.ok ? (
                      <span className="pw-pill pw-pill-ingested">{st.already ? 'Already imported' : 'Imported ✓'}</span>
                    ) : (
                      <button className="btn solid dw-teams-import" disabled={importing !== null || hasTr === false}
                        onClick={() => importOne(m)}>
                        {importing === m.join_url ? 'Importing…' : 'Import'}
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )
        )}
      </div>
    </div>
  );
}
