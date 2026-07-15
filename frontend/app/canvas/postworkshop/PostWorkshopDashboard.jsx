'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPatch, apiPost } from '../../lib/api';
import { Icon } from '../../lib/icons';
import DocumentViewer from '../preworkshop/DocumentViewer';
import { ArtifactsGrid, ConfirmDeleteModal } from '../preworkshop/PreWorkshopDashboard';
import ArtifactExplorer from '../ArtifactExplorer';
import SynthesisCanvas, { loadCanvasSet } from '../duringworkshop/SynthesisCanvas';
import AdoSyncModal from './AdoSyncModal';
import '../../shared.css';

// The Post-Workshop dashboard — phase 3 of 4 ("Backlog · Opportunities ·
// MoM"). Same layout language as the During-Workshop dashboard (left
// ArtifactExplorer, hero + stats, generator canvas, content panels,
// artifacts grid); the generators and panels are this phase's own:
//   backlog        -> the Product Backlog tree (backlog_* tables)
//   opportunities  -> the Future Opportunities Register (opportunities table)
//   mom            -> Minutes of Meeting (generated_docs.mom_json)
// plus the one-way "Sync to Azure DevOps" push (routes/backlog.py).

// Pipeline order: the backlog is the anchor artifact; opportunities and
// minutes read the same context and are independent of it.
const POST_GENERATORS = [
  { id: 'backlog', label: 'Product Backlog', icon: 'list' },
  { id: 'opportunities', label: 'Opportunities', icon: 'bulb' },
  { id: 'mom', label: 'Minutes of Meeting', icon: 'doc-text' },
];

const HORIZON_LABEL = { phase_1: 'Phase 1', phase_2: 'Phase 2', phase_3: 'Phase 3', explore: 'Explore' };
const OPP_STATUS = {
  accepted: { label: 'Accepted', cls: 'pw-pill-ingested' },
  flagged_for_pruning: { label: 'Prune', cls: 'pw-pill-parsing' },
  rejected: { label: 'Rejected', cls: 'pw-pill-failed' },
};

// ══════════════════════════════════════════════════════════════════════
export default function PostWorkshopDashboard({ user, workshopId, onBoardView }) {
  const [docs, setDocs] = useState([]);
  const [artifacts, setArtifacts] = useState([]);
  const [backlog, setBacklog] = useState(null);            // {epics, counts}
  const [opps, setOpps] = useState([]);
  const [mom, setMom] = useState(null);                    // {doc_id, decisions, actions, ...} | null
  const [syncStatus, setSyncStatus] = useState(null);      // {configured, pending, synced, ...}
  const [error, setError] = useState('');

  const [canvasItems, setCanvasItems] = useState([]);
  const [pipeline, setPipeline] = useState(null);
  const [lastResult, setLastResult] = useState(null);
  const runningGen = pipeline ? (pipeline.find((s) => s.status === 'running') || {}).id || null : null;

  const [viewerDocId, setViewerDocId] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [syncOpen, setSyncOpen] = useState(false);
  const fileInputRef = useRef(null);

  const loadDocs = useCallback(async () => {
    try {
      const d = await apiGet(`/api/agents/prepare-docs?workshop_id=${workshopId}`);
      if (d && d.ok) setDocs(d.docs || []);
    } catch { /* transient */ }
  }, [workshopId]);

  const loadArtifacts = useCallback(async () => {
    try {
      const d = await apiGet(`/api/agents/generated-docs?workshop_id=${workshopId}`);
      if (d && d.ok) setArtifacts(d.docs || []);
    } catch { /* transient */ }
  }, [workshopId]);

  const loadBacklog = useCallback(async () => {
    try {
      const d = await apiGet(`/api/backlog?workshop_id=${workshopId}`);
      if (d && d.ok) setBacklog({ epics: d.epics || [], counts: d.counts || {} });
    } catch { /* transient */ }
  }, [workshopId]);

  const loadOpps = useCallback(async () => {
    try {
      const d = await apiGet(`/api/opportunities?workshop_id=${workshopId}`);
      if (d && d.ok) setOpps(d.opportunities || []);
    } catch { /* transient */ }
  }, [workshopId]);

  const loadMom = useCallback(async () => {
    try {
      const d = await apiGet(`/api/backlog/mom?workshop_id=${workshopId}`);
      if (d && d.ok) setMom(d.mom);
    } catch { /* transient */ }
  }, [workshopId]);

  const loadSync = useCallback(async () => {
    try {
      const d = await apiGet(`/api/backlog/sync/status?workshop_id=${workshopId}`);
      if (d && d.ok) setSyncStatus(d);
    } catch { /* transient */ }
  }, [workshopId]);

  const refreshAll = useCallback(() => {
    loadDocs(); loadArtifacts(); loadBacklog(); loadOpps(); loadMom(); loadSync();
  }, [loadDocs, loadArtifacts, loadBacklog, loadOpps, loadMom, loadSync]);

  useEffect(() => { refreshAll(); }, [refreshAll]);

  // The canvas working set is the WORKSHOP's, not the phase's — the same
  // sessionStorage key During-Workshop uses, so a set composed there
  // carries straight into this phase ("same set -> backlog -> minutes").
  useEffect(() => { setCanvasItems(loadCanvasSet(workshopId)); }, [workshopId]);

  const updateCanvasItems = useCallback((next) => {
    setCanvasItems((prev) => {
      const value = typeof next === 'function' ? next(prev) : next;
      queueMicrotask(() => {
        try { window.sessionStorage.setItem(`aidc-canvas-${workshopId}`, JSON.stringify(value)); }
        catch { /* private mode — the set just won't persist */ }
      });
      return value;
    });
  }, [workshopId]);

  // Poll ingestion while any source upload is in flight.
  useEffect(() => {
    const pending = docs.some((d) => d.status === 'queued' || d.status === 'parsing');
    if (!pending) return;
    const t = setInterval(() => { loadDocs(); }, 4000);
    return () => clearInterval(t);
  }, [docs, loadDocs]);

  async function handleUpload(e) {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const fd = new FormData();
    fd.append('workshop_id', String(workshopId));
    fd.append('file', file);
    fd.append('phase', 'Post-Workshop');
    setError('');
    try {
      const res = await fetch('/api/agents/upload', { method: 'POST', credentials: 'same-origin', body: fd });
      const data = await res.json();
      if (!data.ok) { setError(data.error || 'upload failed'); return; }
      loadDocs();
    } catch (err) {
      setError(err.message || 'upload failed');
    }
  }

  // One generator call. Unlike During-Workshop, an empty canvas is a
  // valid run here: the captured requirements table + capability map
  // ride along server-side regardless of selection (_engagement_context),
  // and they — not the corpus — are the backlog's primary source.
  async function runOne(agentId, prompt) {
    const doc_ids = canvasItems.length ? {
      sources: canvasItems.filter((i) => i.kind === 'source').map((i) => i.doc_id),
      generated: canvasItems.filter((i) => i.kind === 'generated').map((i) => i.doc_id),
    } : undefined;
    const res = await apiPost('/api/agents/run', {
      agent_id: agentId, workshop_id: workshopId, context: { zone: 'Post-Workshop' },
      extra: (prompt || '').trim() || undefined,
      ...(doc_ids ? { options: { doc_ids } } : {}),
    });
    if (!res.ok) throw new Error(res.error || 'failed');
    const draft = res.draft || {};
    if (agentId === 'backlog') {
      loadBacklog(); loadSync();
      const c = (draft.backlog && draft.backlog.counts) || {};
      return { note: `${c.epics || 0} epics · ${c.features || 0} features · ${c.stories || 0} stories`, docId: draft.node && draft.node.docId };
    }
    if (agentId === 'opportunities') {
      loadOpps();
      const added = (draft.opportunities || []).length;
      return { note: `+${added} new`, docId: draft.node && draft.node.docId };
    }
    if (agentId === 'mom') {
      loadMom();
      const m = draft.mom || {};
      return {
        note: `${(m.decisions || []).length} decisions · ${(m.actions || []).length} actions`,
        docId: draft.node && draft.node.docId,
      };
    }
    return { note: 'done', docId: draft.node && draft.node.docId };
  }

  const pipelineBusy = useRef(false);
  async function runPipeline(agentIds, prompt) {
    if (pipelineBusy.current) return;
    pipelineBusy.current = true;
    try {
      await _runPipelineInner(agentIds, prompt);
    } finally {
      pipelineBusy.current = false;
    }
  }

  async function _runPipelineInner(agentIds, prompt) {
    const order = POST_GENERATORS.filter((g) => agentIds.includes(g.id));
    if (!order.length) return;
    setError('');
    setLastResult(null);
    setPipeline(order.map((g) => ({ id: g.id, label: g.label, status: 'pending', note: '' })));
    let doneCount = 0;
    let lastDocId = null;
    for (const g of order) {
      setPipeline((p) => p.map((s) => s.id === g.id ? { ...s, status: 'running' } : s));
      try {
        const out = await runOne(g.id, prompt);
        doneCount += 1;
        if (out.docId) lastDocId = out.docId;
        setPipeline((p) => p.map((s) => s.id === g.id ? { ...s, status: 'done', note: out.note } : s));
      } catch (err) {
        setPipeline((p) => p.map((s) => s.id === g.id ? { ...s, status: 'failed', note: err.message || 'failed' } : s));
      }
    }
    loadArtifacts();
    setLastResult({
      label: doneCount === order.length
        ? `${doneCount} output${doneCount === 1 ? '' : 's'} generated`
        : `${doneCount}/${order.length} outputs generated — see the failed step above`,
      docId: order.length === 1 ? lastDocId : null,
    });
  }

  function addToCanvas(item) {
    updateCanvasItems((prev) => prev.some((x) => x.doc_id === item.doc_id) ? prev : [...prev, item]);
  }

  function deleteArtifact(docId, name) { setConfirmDelete({ docId, name }); }

  async function performDelete() {
    if (!confirmDelete) return;
    setDeleting(true);
    setError('');
    try {
      const res = await apiDelete(`/api/agents/document/${confirmDelete.docId}?workshop_id=${workshopId}`);
      if (!res.ok) { setError(res.error || 'delete failed'); return; }
      if (viewerDocId === confirmDelete.docId) setViewerDocId(null);
      refreshAll();
    } catch (err) {
      setError(err.message || 'delete failed');
    } finally {
      setDeleting(false);
      setConfirmDelete(null);
    }
  }

  const counts = (backlog && backlog.counts) || {};
  const hasBacklog = !!(backlog && backlog.epics && backlog.epics.length);

  return (
    <div className="pw-dash">
      <ArtifactExplorer workshopId={workshopId} docs={docs} artifacts={artifacts}
        activePhase="Post-Workshop"
        onAdd={() => fileInputRef.current?.click()}
        onView={setViewerDocId}
        onDelete={deleteArtifact}
        onAddToCanvas={addToCanvas} />
      <input ref={fileInputRef} type="file" style={{ display: 'none' }} onChange={handleUpload}
        accept=".pdf,.docx,.xlsx,.pptx,.csv,.html,.txt,.md,.vtt,.zip" />

      <div className="pw-scroll">
        <header className="pw-hero">
          <div className="pw-hero-txt">
            <h1>Post-Workshop Delivery Prep</h1>
            <p>Turn validated requirements into an epics → features → stories backlog with acceptance
              criteria, push directly to Azure DevOps, capture future opportunities, and
              auto-compile the minutes.</p>
            {onBoardView && (
              <button className="dw-board-toggle" onClick={onBoardView}
                title="Open the freeform canvas board for this phase">
                <Icon name="flow" />Board view
              </button>
            )}
          </div>
          <div className="pw-stats">
            <div className="pw-stat"><span className="pw-stat-ic"><Icon name="layers" /></span><div className="pw-stat-body"><div className="pw-stat-num">{counts.epics ?? '—'}</div><div className="pw-stat-lbl">Epics</div></div></div>
            <div className="pw-stat"><span className="pw-stat-ic"><Icon name="grid" /></span><div className="pw-stat-body"><div className="pw-stat-num">{counts.features ?? '—'}</div><div className="pw-stat-lbl">Features</div></div></div>
            <div className="pw-stat"><span className="pw-stat-ic"><Icon name="list" /></span><div className="pw-stat-body"><div className="pw-stat-num">{counts.stories ?? '—'}</div><div className="pw-stat-lbl">Stories</div></div></div>
            <div className="pw-stat"><span className="pw-stat-ic"><Icon name="target" /></span><div className="pw-stat-body"><div className="pw-stat-num">{opps.length}</div><div className="pw-stat-lbl">Opportunities</div></div></div>
          </div>
        </header>

        {error && <div className="app-error pw-err">⚠ {error}</div>}

        <SynthesisCanvas workshopId={workshopId} items={canvasItems} onItemsChange={updateCanvasItems}
          docs={docs} artifacts={artifacts}
          pipeline={pipeline} onGenerate={runPipeline}
          lastResult={lastResult} onOpenResult={setViewerDocId}
          generators={POST_GENERATORS}
          title="Delivery Generator"
          subtitle="Backlog, opportunities and minutes generate from the captured requirements and capability map — optionally scope them by dragging documents here."
          emptyOk />

        {hasBacklog && syncStatus && syncStatus.configured && (
          <div className="pb-sync-banner">
            <span className="pb-sync-txt">
              <Icon name="refresh" />
              One-way push keeps your Azure DevOps board aligned with this backlog.
            </span>
            <button className="btn solid" onClick={() => setSyncOpen(true)}
              disabled={syncStatus.pending === 0}
              title={syncStatus.pending === 0 ? 'Everything is up to date on the board'
                : `${syncStatus.pending} item${syncStatus.pending === 1 ? '' : 's'} new or changed since the last push`}>
              <Icon name="sparkles" />
              {syncStatus.pending === 0 ? 'Board up to date' : `Push ${syncStatus.pending} item${syncStatus.pending === 1 ? '' : 's'}`}
            </button>
          </div>
        )}

        <BacklogBoard workshopId={workshopId} backlog={backlog}
          onChanged={() => { loadBacklog(); loadSync(); }}
          generating={runningGen === 'backlog'}
          onGenerate={() => runPipeline(['backlog'], '')}
          onOpenSync={() => setSyncOpen(true)} />

        <div className="dw-cols">
          <OpportunitiesRegister workshopId={workshopId} opps={opps} onChanged={loadOpps}
            generating={runningGen === 'opportunities'}
            onGenerate={() => runPipeline(['opportunities'], '')} />
          <MomCard mom={mom} generating={runningGen === 'mom'}
            onGenerate={() => runPipeline(['mom'], '')}
            onOpenDoc={setViewerDocId} />
        </div>

        <ArtifactsGrid docs={docs} artifacts={artifacts} onView={setViewerDocId} workshopId={workshopId}
          onViewDiagram={() => {}} onViewAnalysis={() => {}} onDelete={deleteArtifact}
          title="Post-Workshop Artifacts" zone="Post-Workshop" showSources={false} />
      </div>

      {viewerDocId && (
        <DocumentViewer workshopId={workshopId} docId={viewerDocId} onClose={() => setViewerDocId(null)} />
      )}
      {confirmDelete && (
        <ConfirmDeleteModal name={confirmDelete.name} busy={deleting}
          onCancel={() => setConfirmDelete(null)} onConfirm={performDelete} />
      )}
      {syncOpen && (
        <AdoSyncModal workshopId={workshopId} onClose={() => setSyncOpen(false)}
          onPushed={() => { loadSync(); }} />
      )}
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════
function BacklogBoard({ workshopId, backlog, onChanged, generating, onGenerate, onOpenSync }) {
  const epics = (backlog && backlog.epics) || [];
  const counts = (backlog && backlog.counts) || {};

  async function removeItem(itemType, rowId) {
    try {
      await apiDelete(`/api/backlog/${itemType}/${rowId}?workshop_id=${workshopId}`);
      onChanged();
    } catch { /* transient */ }
  }

  return (
    <section className="pw-panel pb-board">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="layers" /></span>
          <div>
            <div className="pw-h3">Product Backlog</div>
            <div className="pw-sub">
              Epics · Features · Stories with acceptance criteria
              {counts.stories ? <> · {counts.stories_with_ac}/{counts.stories} stories have criteria</> : null}
            </div>
          </div>
        </div>
        <div className="pb-board-actions">
          {epics.length > 0 && <span className="pw-pill pw-pill-draft dw-panel-pill">Draft</span>}
          {epics.length > 0 && (
            <button className="btn" onClick={onOpenSync} title="Push this backlog to Azure DevOps Boards">
              <Icon name="grid" />Sync to Azure DevOps
            </button>
          )}
          <button className="btn solid" onClick={onGenerate} disabled={generating}
            title="Generate from the captured requirements + capability map (regenerating replaces the tree)">
            <Icon name="sparkles" />{generating ? 'Building…' : epics.length ? 'Regenerate' : 'Generate backlog'}
          </button>
        </div>
      </div>

      {epics.length === 0 ? (
        <div className="pw-empty">
          No backlog yet — <b>Generate backlog</b> turns the captured requirements and capability
          map into an epics → features → stories tree, acceptance criteria included.
        </div>
      ) : (
        <div className="pb-epics">
          {epics.map((e) => (
            <EpicCard key={e.id} epic={e} workshopId={workshopId}
              onChanged={onChanged} onRemove={removeItem} />
          ))}
        </div>
      )}
    </section>
  );
}

function EpicCard({ epic, workshopId, onChanged, onRemove }) {
  const [open, setOpen] = useState(true);
  const features = epic.features || [];
  const stories = features.flatMap((f) => f.stories || []);
  const withAc = stories.filter((s) => (s.acceptance_criteria || []).length > 0).length;
  return (
    <div className={'pb-epic' + (open ? '' : ' collapsed')}>
      <div className="pb-epic-hd" onClick={() => setOpen((v) => !v)} role="button" tabIndex={0}
        aria-expanded={open} title={open ? 'Collapse epic' : 'Expand epic'}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen((v) => !v); } }}>
        <span className={'pb-epic-chev' + (open ? ' open' : '')}>▸</span>
        <span className="pb-epic-ic"><Icon name="layers" /></span>
        <div className="pb-epic-ttl">
          <div className="pb-eyebrow">EPIC · {epic.epic_id}</div>
          <div className="pb-epic-name" title={epic.description || undefined}>{epic.title}</div>
        </div>
        {stories.length > 0 && (
          <span className="pb-epic-progress" title={`${withAc} of ${stories.length} stories have acceptance criteria`}>
            {features.length} feature{features.length === 1 ? '' : 's'} · {stories.length} stor{stories.length === 1 ? 'y' : 'ies'} · {Math.round((withAc / stories.length) * 100)}% AC
          </span>
        )}
        <button className="pw-view-btn pw-del-btn pb-del" title="Delete this epic (and its features/stories)"
          onClick={(e) => { e.stopPropagation(); onRemove('epic', epic.id); }}>
          <Icon name="trash" />
        </button>
      </div>
      {open && features.map((f) => (
        <FeatureCard key={f.id} feature={f} workshopId={workshopId}
          onChanged={onChanged} onRemove={onRemove} />
      ))}
    </div>
  );
}

function FeatureCard({ feature, workshopId, onChanged, onRemove }) {
  const stories = feature.stories || [];
  const allHaveAc = stories.length > 0 && stories.every((s) => (s.acceptance_criteria || []).length > 0);
  return (
    <div className="pb-feature">
      <div className="pb-feature-hd">
        <span className="pb-feature-pill">Feature</span>
        <span className="pb-feature-name">{feature.title}</span>
        <button className="pw-view-btn pw-del-btn pb-del" title="Delete this feature (and its stories)"
          onClick={() => onRemove('feature', feature.id)}>
          <Icon name="trash" />
        </button>
      </div>
      <ul className="pb-stories">
        {stories.map((s) => (
          <StoryRow key={s.id} story={s} workshopId={workshopId}
            onChanged={onChanged} onRemove={onRemove} />
        ))}
      </ul>
      {allHaveAc && (
        <div className="pb-ac-done"><Icon name="check-circle" />Acceptance criteria generated</div>
      )}
    </div>
  );
}

function StoryRow({ story, workshopId, onChanged, onRemove }) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(null);   // {text, acceptance_criteria} while editing
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const ac = story.acceptance_criteria || [];

  async function save() {
    if (!editing || !editing.text.trim()) return;
    setBusy(true); setErr('');
    try {
      const res = await apiPatch(`/api/backlog/story/${story.id}`, {
        workshop_id: workshopId, text: editing.text,
        acceptance_criteria: editing.acceptance_criteria,
      });
      if (!res.ok) { setErr(res.error || 'could not save'); return; }
      setEditing(null);
      onChanged();
    } catch (e) { setErr(e.message || 'could not save'); } finally { setBusy(false); }
  }

  function editAc(i, key, value) {
    setEditing((ed) => ({
      ...ed,
      acceptance_criteria: ed.acceptance_criteria.map((c, ix) => ix === i ? { ...c, [key]: value } : c),
    }));
  }

  if (editing) {
    return (
      <li className="pb-story pb-story-edit">
        <textarea className="pw-instruction" rows={2} value={editing.text} autoFocus
          onChange={(e) => setEditing({ ...editing, text: e.target.value })} />
        {editing.acceptance_criteria.map((c, i) => (
          <div key={i} className="pb-ac-edit">
            <input type="text" placeholder="Given…" value={c.given} onChange={(e) => editAc(i, 'given', e.target.value)} />
            <input type="text" placeholder="When…" value={c.when} onChange={(e) => editAc(i, 'when', e.target.value)} />
            <input type="text" placeholder="Then…" value={c.then} onChange={(e) => editAc(i, 'then', e.target.value)} />
            <button className="pw-view-btn pw-del-btn" title="Remove this scenario"
              onClick={() => setEditing((ed) => ({ ...ed, acceptance_criteria: ed.acceptance_criteria.filter((_, ix) => ix !== i) }))}>
              <Icon name="x" />
            </button>
          </div>
        ))}
        <div className="pb-story-edit-row">
          <button className="pw-view-btn" onClick={() => setEditing((ed) => ({
            ...ed, acceptance_criteria: [...ed.acceptance_criteria, { given: '', when: '', then: '' }],
          }))}><Icon name="plus" />Scenario</button>
          <span className="pb-spacer" />
          {err && <span className="dw-teams-err">⚠ {err}</span>}
          <button className="btn solid" onClick={save} disabled={busy || !editing.text.trim()}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          <button className="btn" onClick={() => setEditing(null)} disabled={busy}>Cancel</button>
        </div>
      </li>
    );
  }

  return (
    <li className={'pb-story' + (open ? ' open' : '') + (ac.length ? '' : ' pb-story-noac')}>
      <button className="pb-story-main" onClick={() => setOpen((v) => !v)}
        title={ac.length ? (open ? 'Hide acceptance criteria' : 'Show acceptance criteria') : 'No acceptance criteria yet'}>
        <span className="pb-story-dot" />
        <span className="pb-story-text">{story.text}</span>
      </button>
      <span className="pb-story-tools">
        <button className="pw-view-btn" title="Edit story & criteria"
          onClick={() => setEditing({ text: story.text, acceptance_criteria: ac.map((c) => ({ ...c })) })}>
          <Icon name="check" />Edit
        </button>
        <button className="pw-view-btn pw-del-btn" title="Delete story" onClick={() => onRemove('story', story.id)}>
          <Icon name="trash" />
        </button>
      </span>
      {open && ac.length > 0 && (
        <div className="pb-ac-list">
          {ac.map((c, i) => (
            <div key={i} className="pb-gwt">
              {c.given && <div><b>Given</b> {c.given}</div>}
              {c.when && <div><b>When</b> {c.when}</div>}
              {c.then && <div><b>Then</b> {c.then}</div>}
            </div>
          ))}
          {(story.source_req_ids || []).length > 0 && (
            <div className="pb-story-trace">Traces to: {story.source_req_ids.join(', ')}</div>
          )}
        </div>
      )}
    </li>
  );
}

// ══════════════════════════════════════════════════════════════════════
function OpportunitiesRegister({ workshopId, opps, onChanged, generating, onGenerate }) {
  async function setStatus(o, status) {
    try {
      // Clicking the current status toggles it back to open.
      await apiPatch(`/api/opportunities/${o.id}`, {
        workshop_id: workshopId, status: o.status === status ? 'open' : status,
      });
      onChanged();
    } catch { /* transient */ }
  }

  async function removeOne(o) {
    try {
      await apiDelete(`/api/opportunities/${o.id}?workshop_id=${workshopId}`);
      onChanged();
    } catch { /* transient */ }
  }

  return (
    <section className="pw-panel pb-opps">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="bulb" /></span>
          <div>
            <div className="pw-h3">Future Opportunities Register</div>
            <div className="pw-sub">Adjacent scope surfaced in discovery — flagged for pruning</div>
          </div>
        </div>
        <button className="btn solid" onClick={onGenerate} disabled={generating}
          title="Mine the discovery material for adjacent-scope opportunities (re-runs only add new ones)">
          <Icon name="sparkles" />{generating ? 'Mining…' : opps.length ? 'Find more' : 'Find opportunities'}
        </button>
      </div>

      {opps.length === 0 ? (
        <div className="pw-empty">
          No opportunities registered yet — <b>Find opportunities</b> surfaces the improvement and
          automation scope adjacent to this engagement.
        </div>
      ) : (
        <ul className="pb-opp-list">
          {opps.map((o) => {
            const st = OPP_STATUS[o.status];
            return (
              <li key={o.id} className={'pb-opp' + (o.status === 'rejected' ? ' rejected' : '')}>
                <span className="pb-opp-ic"><Icon name="bulb" /></span>
                <div className="pb-opp-main">
                  <div className="pb-opp-ttl">{o.title}</div>
                  {o.description && <div className="pb-opp-desc">{o.description}</div>}
                  <div className="pb-opp-meta">Horizon: {HORIZON_LABEL[o.horizon] || o.horizon}
                    {(o.source_req_ids || []).length > 0 && <> · {o.source_req_ids.join(', ')}</>}</div>
                </div>
                <span className="pb-opp-badges">
                  <span className="pb-size">Size {o.size}</span>
                  <span className={`pb-prio pb-prio-${o.priority}`}>{o.priority}</span>
                  {st && <span className={`pw-pill ${st.cls}`}>{st.label}</span>}
                </span>
                <span className="pb-opp-tools">
                  <button className="pw-view-btn" title={o.status === 'accepted' ? 'Un-accept' : 'Accept into scope'}
                    onClick={() => setStatus(o, 'accepted')}><Icon name="check" /></button>
                  <button className="pw-view-btn" title={o.status === 'flagged_for_pruning' ? 'Unflag' : 'Flag for pruning'}
                    onClick={() => setStatus(o, 'flagged_for_pruning')}><Icon name="alert" /></button>
                  <button className="pw-view-btn" title={o.status === 'rejected' ? 'Un-reject' : 'Reject'}
                    onClick={() => setStatus(o, 'rejected')}><Icon name="x" /></button>
                  <button className="pw-view-btn pw-del-btn" title="Delete" onClick={() => removeOne(o)}>
                    <Icon name="trash" /></button>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// ══════════════════════════════════════════════════════════════════════
function MomCard({ mom, generating, onGenerate, onOpenDoc }) {
  return (
    <section className="pw-panel pb-mom">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="doc-text" /></span>
          <div>
            <div className="pw-h3">Minutes of Meeting</div>
            <div className="pw-sub">Aggregated from Teams transcripts + notes</div>
          </div>
        </div>
        <div className="pb-mom-actions">
          {mom && mom.confidence != null && (
            <span className="pw-pill pw-pill-ingested dw-panel-pill">{mom.confidence}% conf.</span>
          )}
          <button className="btn solid" onClick={onGenerate} disabled={generating}
            title="Assemble minutes from the imported transcripts and session notes">
            <Icon name="sparkles" />{generating ? 'Compiling…' : mom ? 'Recompile' : 'Compile minutes'}
          </button>
        </div>
      </div>

      {!mom ? (
        <div className="pw-empty">
          No minutes yet — <b>Compile minutes</b> assembles decisions, actions and open questions
          from the imported Teams transcripts and session notes.
        </div>
      ) : (
        <div className="pb-mom-body">
          {(mom.decisions || []).length > 0 && (
            <div className="pb-mom-sec">
              <div className="pb-mom-hd">Decisions</div>
              <ul>{mom.decisions.map((d, i) => <li key={i} className="pb-dot-green">{d}</li>)}</ul>
            </div>
          )}
          {(mom.actions || []).length > 0 && (
            <div className="pb-mom-sec">
              <div className="pb-mom-hd">Actions</div>
              <ul>{mom.actions.map((a, i) => (
                <li key={i} className="pb-dot-purple">
                  <b>{a.owner}</b> — {a.text}{a.due ? ` (by ${a.due})` : ''}
                </li>
              ))}</ul>
            </div>
          )}
          {(mom.open_questions || []).length > 0 && (
            <div className="pb-mom-sec">
              <div className="pb-mom-hd">Open questions</div>
              <ul>{mom.open_questions.map((q, i) => <li key={i} className="pb-dot-amber">{q}</li>)}</ul>
            </div>
          )}
          <button className="pb-mom-open" onClick={() => onOpenDoc(mom.doc_id)}>
            <Icon name="eye" />Open the full minutes document
          </button>
        </div>
      )}
    </section>
  );
}
