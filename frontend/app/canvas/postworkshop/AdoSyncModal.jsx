'use client';

import { useEffect, useRef, useState } from 'react';
import { apiGet, apiPost } from '../../lib/api';
import { Icon } from '../../lib/icons';

// "Sync to Azure DevOps" — the one-way backlog push (see backend
// services/ado_workitems.py). Flow: load sync status on open → show the
// pending/synced counts (or the not-configured instructions) → push →
// per-item receipt list with board deep links. Idempotent by design:
// items already on the board and unchanged are skipped server-side.

const ACTION_PILL = {
  created: 'pw-pill-ingested',
  updated: 'pw-pill-draft',
  skipped: 'pw-pill-queued',
  failed: 'pw-pill-failed',
};

export default function AdoSyncModal({ workshopId, onClose, onPushed }) {
  // phase: loading | ready | pushing | done | error
  const [phase, setPhase] = useState('loading');
  const [status, setStatus] = useState(null);   // GET /api/backlog/sync/status payload
  const [result, setResult] = useState(null);   // POST push payload
  const [error, setError] = useState('');
  const disposed = useRef(false);
  useEffect(() => {
    disposed.current = false;
    return () => { disposed.current = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const d = await apiGet(`/api/backlog/sync/status?workshop_id=${workshopId}`);
        if (cancelled) return;
        if (!d || !d.ok) { setError((d && d.error) || 'could not read the sync state'); setPhase('error'); return; }
        setStatus(d);
        setPhase('ready');
      } catch (e) {
        if (!cancelled) { setError(e.message || 'could not read the sync state'); setPhase('error'); }
      }
    })();
    return () => { cancelled = true; };
  }, [workshopId]);

  async function push() {
    setPhase('pushing');
    setError('');
    try {
      const d = await apiPost('/api/backlog/sync/azure-devops', { workshop_id: workshopId });
      if (disposed.current) return;
      setResult(d);
      if (!d.ok && !d.results) { setError(d.error || 'push failed'); setPhase('error'); return; }
      setPhase('done');
      onPushed && onPushed();
    } catch (e) {
      if (!disposed.current) { setError(e.message || 'push failed'); setPhase('error'); }
    }
  }

  const results = (result && result.results) || [];

  return (
    <div className="pw-modal-backdrop" onClick={onClose}>
      <div className="pb-sync-modal" onClick={(e) => e.stopPropagation()}>
        <div className="dw-teams-head">
          <span className="pw-ic pw-ic-accent"><Icon name="grid" /></span>
          <div>
            <div className="pw-h3">Sync to Azure DevOps</div>
            <div className="pw-sub">One-way push — Epics, Features and Stories become work items on your board.</div>
          </div>
          <button className="pw-view-btn" onClick={onClose} title="Close"><Icon name="x" /></button>
        </div>

        {phase === 'loading' && <div className="pw-empty">Checking the sync state…</div>}

        {phase === 'error' && (
          <div className="dw-teams-connect">
            <div className="app-error pw-err">⚠ {error}</div>
          </div>
        )}

        {phase !== 'loading' && status && !status.configured && (
          <div className="dw-teams-connect">
            <p>Azure DevOps isn't configured yet. Set these in <b>backend/.env</b> and restart the backend:</p>
            <ul className="pb-sync-env">
              <li><code>ADO_ORG_URL</code> — e.g. <code>https://dev.azure.com/your-org</code></li>
              <li><code>ADO_PROJECT</code> — the target project name</li>
              <li><code>ADO_PAT</code> — a Personal Access Token with <b>Work Items: Read &amp; Write</b></li>
              <li><code>ADO_STORY_TYPE</code> — optional; <i>User Story</i> (Agile, default) or <i>Product Backlog Item</i> (Scrum)</li>
            </ul>
            {status.missing && status.missing.length > 0 && (
              <p className="pw-sub">Missing right now: {status.missing.join(', ')}</p>
            )}
          </div>
        )}

        {(phase === 'ready' || phase === 'pushing') && status && status.configured && (
          <div className="pb-sync-body">
            <div className="pb-sync-target">
              <div><span className="pb-sync-lbl">Organization</span>{status.org_url}</div>
              <div><span className="pb-sync-lbl">Project</span>{status.project}</div>
              <div><span className="pb-sync-lbl">Story type</span>{status.story_type}</div>
            </div>
            <div className="pb-sync-counts">
              <span className="pw-pill pw-pill-parsing">{status.pending} to push</span>
              <span className="pw-pill pw-pill-ingested">{status.synced} up to date</span>
              <span className="pw-pill pw-pill-queued">{status.total} total</span>
            </div>
            <p className="pw-sub">
              New items are created (Epic → Feature → Story, parent-linked, acceptance criteria
              included); items pushed before and edited here since are updated; unchanged items
              are skipped. Nothing is ever read back or deleted from the board.
            </p>
            <button className="btn solid pb-sync-push" onClick={push}
              disabled={phase === 'pushing' || status.pending === 0}>
              <Icon name="sparkles" />
              {phase === 'pushing' ? 'Pushing…'
                : status.pending === 0 ? 'Everything is up to date'
                : `Push ${status.pending} item${status.pending === 1 ? '' : 's'}`}
            </button>
          </div>
        )}

        {phase === 'done' && result && (
          <div className="pb-sync-body">
            <div className="pb-sync-counts">
              {result.created > 0 && <span className="pw-pill pw-pill-ingested">{result.created} created</span>}
              {result.updated > 0 && <span className="pw-pill pw-pill-draft">{result.updated} updated</span>}
              {result.skipped > 0 && <span className="pw-pill pw-pill-queued">{result.skipped} skipped</span>}
              {result.failed > 0 && <span className="pw-pill pw-pill-failed">{result.failed} failed</span>}
            </div>
            {result.error && <div className="app-error pw-err">⚠ {result.error}</div>}
            <ul className="pb-sync-results">
              {results.map((r, i) => (
                <li key={i} className="pb-sync-row">
                  <span className="pb-sync-code">{r.code}</span>
                  <span className="pb-sync-title" title={r.title}>{r.title}</span>
                  <span className={`pw-pill ${ACTION_PILL[r.action] || 'pw-pill-queued'}`}>{r.action}</span>
                  {r.url ? (
                    <a className="pb-sync-link" href={r.url} target="_blank" rel="noreferrer"
                      title="Open on the board">#{r.external_id}</a>
                  ) : r.error ? (
                    <span className="dw-teams-err" title={r.error}>⚠ {r.error}</span>
                  ) : <span />}
                </li>
              ))}
            </ul>
            <button className="btn" onClick={onClose}>Done</button>
          </div>
        )}
      </div>
    </div>
  );
}
