'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPatch, apiPost } from '../../lib/api';
import { Icon } from '../../lib/icons';
import DiagramModal from '../preworkshop/DiagramModal';
import DocumentViewer from '../preworkshop/DocumentViewer';
import { ArtifactsGrid, ConfirmDeleteModal } from '../preworkshop/PreWorkshopDashboard';
import { isTranscript, timeAgo } from '../artifactMeta';
import ArtifactExplorer from '../ArtifactExplorer';
import DiagramCanvas from './DiagramCanvas';
import SynthesisCanvas, { GENERATORS, loadCanvasSet } from './SynthesisCanvas';
import TeamsImportModal from './TeamsImportModal';
import '../../shared.css';

const MOSCOW_LABEL = { must: 'Must', should: 'Should', could: 'Could', wont: "Won't" };
const REQ_CATEGORIES = ['Process', 'Data', 'Integration', 'Reporting', 'Security', 'UX', 'Compliance', 'Other'];

// ══════════════════════════════════════════════════════════════════════
export default function DuringWorkshopDashboard({ user, workshopId, onBoardView }) {
  const [docs, setDocs] = useState([]);
  const [artifacts, setArtifacts] = useState([]);
  const [stats, setStats] = useState(null);
  const [reqs, setReqs] = useState([]);
  const [capmap, setCapmap] = useState(null);
  const [error, setError] = useState('');

  const [teamsOpen, setTeamsOpen] = useState(false);
  // The Synthesis Canvas working set — every generator below runs scoped
  // to exactly these items (options.doc_ids), never the whole workshop.
  const [canvasItems, setCanvasItems] = useState([]);
  const [pipeline, setPipeline] = useState(null);         // [{id,label,status,note}] while a run is live
  const [lastResult, setLastResult] = useState(null);     // {label, docId?}
  const runningGen = pipeline ? (pipeline.find((s) => s.status === 'running') || {}).id || null : null;

  const [viewerDocId, setViewerDocId] = useState(null);
  // {xml, diagrams?, title} — with diagram JSON it opens the native
  // viewer (DiagramModal), XML-only (DiagramSection's "Edit in draw.io")
  // falls straight through to the draw.io editor.
  const [editDiagram, setEditDiagram] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [capmapModal, setCapmapModal] = useState(null);    // capmap payload for an artifact card
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

  const loadStats = useCallback(async () => {
    try {
      const d = await apiGet(`/api/agents/workshop-stats?workshop_id=${workshopId}`);
      if (d && d.ok) setStats(d.stats);
    } catch { /* transient */ }
  }, [workshopId]);

  const loadReqs = useCallback(async () => {
    try {
      const d = await apiGet(`/api/agents/requirements?workshop_id=${workshopId}`);
      if (d && d.ok) setReqs(d.requirements || []);
    } catch { /* transient */ }
  }, [workshopId]);

  const loadCapmap = useCallback(async () => {
    try {
      const d = await apiGet(`/api/agents/capmap?workshop_id=${workshopId}`);
      if (d && d.ok) setCapmap(d.capmap);
    } catch { /* transient */ }
  }, [workshopId]);

  const refreshAll = useCallback(() => {
    loadDocs(); loadArtifacts(); loadStats(); loadReqs(); loadCapmap();
  }, [loadDocs, loadArtifacts, loadStats, loadReqs, loadCapmap]);

  useEffect(() => { refreshAll(); }, [refreshAll]);

  // Restore the canvas working set after mount (sessionStorage is
  // browser-only — reading it during render would break hydration).
  useEffect(() => { setCanvasItems(loadCanvasSet(workshopId)); }, [workshopId]);

  // Persist at MUTATION time, not via an effect on canvasItems: a
  // persist-effect fires on mount with the initial empty list and (under
  // StrictMode's double-mount) wipes the stored set before restore wins.
  // Every canvas mutation flows through here instead.
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

  // Poll ingestion while any source (e.g. a fresh transcript) is in flight.
  useEffect(() => {
    const pending = docs.some((d) => d.status === 'queued' || d.status === 'parsing');
    if (!pending) return;
    const t = setInterval(() => { loadDocs(); loadStats(); }, 4000);
    return () => clearInterval(t);
  }, [docs, loadDocs, loadStats]);

  async function handleUpload(e) {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const fd = new FormData();
    fd.append('workshop_id', String(workshopId));
    fd.append('file', file);
    setError('');
    try {
      const res = await fetch('/api/agents/upload', { method: 'POST', credentials: 'same-origin', body: fd });
      const data = await res.json();
      if (!data.ok) { setError(data.error || 'upload failed'); return; }
      loadDocs(); loadStats();
    } catch (err) {
      setError(err.message || 'upload failed');
    }
  }

  // One generator call, scoped to the canvas working set. Returns a
  // short note for the pipeline step (throws on failure).
  async function runOne(agentId, prompt) {
    const doc_ids = {
      sources: canvasItems.filter((i) => i.kind === 'source').map((i) => i.doc_id),
      generated: canvasItems.filter((i) => i.kind === 'generated').map((i) => i.doc_id),
    };
    const res = await apiPost('/api/agents/run', {
      agent_id: agentId, workshop_id: workshopId, context: { zone: 'During Workshop' },
      extra: (prompt || '').trim() || undefined,
      options: { doc_ids },
    });
    if (!res.ok) throw new Error(res.error || 'failed');
    if (agentId === 'extract_reqs') {
      const added = (res.draft.requirements || []).length;
      const total = res.draft.extracted_count || 0;
      loadReqs();
      return {
        note: `+${added} new` + (total > added ? ` · ${total - added} duplicate${total - added === 1 ? '' : 's'} skipped` : ''),
        docId: null,
      };
    }
    if (agentId === 'capmap') loadCapmap();
    return { note: 'done', docId: res.draft && res.draft.node && res.draft.node.docId };
  }

  // The Run button: executes the selected outputs as a sequential
  // pipeline in dependency order (GENERATORS' order — capmap reads the
  // requirements table, brd reads both), each step scoped to the canvas
  // selection. Strict gating by design: no selection, no generation.
  // A failed step is marked and the pipeline continues — later outputs
  // may still be useful.
  // `pipelineBusy` is a REF, not state: the button's disabled flag only
  // takes effect after a re-render, so a double-click fired two whole
  // pipelines concurrently (observed live: duplicate capability maps and
  // BRDs ~1 min apart). A ref flips synchronously on the first call and
  // makes the second a no-op.
  const pipelineBusy = useRef(false);
  async function runPipeline(agentIds, prompt) {
    if (pipelineBusy.current) return;
    if (canvasItems.length === 0) {
      setError('Drag documents onto the Synthesis Canvas first — generation reads only what you put there.');
      return;
    }
    pipelineBusy.current = true;
    try {
      await _runPipelineInner(agentIds, prompt);
    } finally {
      pipelineBusy.current = false;
    }
  }

  async function _runPipelineInner(agentIds, prompt) {
    const order = GENERATORS.filter((g) => agentIds.includes(g.id));
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
    loadArtifacts(); loadStats();
    // The finished pipeline stays visible (its per-step notes are the
    // receipt); the next run replaces it.
    setLastResult({
      label: doneCount === order.length
        ? `${doneCount} output${doneCount === 1 ? '' : 's'} generated from ${canvasItems.length} input${canvasItems.length === 1 ? '' : 's'}`
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

  async function viewCapmapArtifact(a) {
    try {
      const d = await apiGet(`/api/agents/document/${a.doc_id}/capmap?workshop_id=${workshopId}`);
      if (d && d.ok) setCapmapModal({ name: a.name, domains: d.domains || [], version: d.version });
    } catch { /* no capmap, or transient */ }
  }

  const transcripts = docs.filter((d) => isTranscript(d.name));

  return (
    <div className="pw-dash">
      <ArtifactExplorer workshopId={workshopId} docs={docs} artifacts={artifacts}
        activePhase="During Workshop"
        onAdd={() => fileInputRef.current?.click()}
        onView={setViewerDocId}
        onOpenDiagram={setEditDiagram}
        onOpenCapmap={setCapmapModal}
        onDelete={deleteArtifact}
        onAddToCanvas={addToCanvas}
        extraAction={(
          <button className="btn solid dw-teams-cta" onClick={() => setTeamsOpen(true)}>
            <Icon name="users" />Import from Teams
          </button>
        )} />
      <input ref={fileInputRef} type="file" style={{ display: 'none' }} onChange={handleUpload}
        accept=".pdf,.docx,.xlsx,.pptx,.csv,.html,.txt,.md,.vtt,.zip" />

      <div className="pw-scroll">
        <header className="pw-hero">
          <div className="pw-hero-txt">
            <h1>Live Workshop Synthesis</h1>
            <p>Import what was said, and watch it become structure: traced business requirements,
              a capability heat map, process flows and a BRD — every item tied back to its source.</p>
            {onBoardView && (
              <button className="dw-board-toggle" onClick={onBoardView}
                title="Open the freeform canvas board for this phase">
                <Icon name="flow" />Board view
              </button>
            )}
          </div>
          <div className="pw-stats">
            <div className="pw-stat"><div className="pw-stat-num">{stats ? stats.transcripts : transcripts.length}</div><div className="pw-stat-lbl">Transcripts imported</div></div>
            <div className="pw-stat"><div className="pw-stat-num">{stats ? stats.requirements : reqs.length}</div><div className="pw-stat-lbl">Requirements captured</div></div>
            <div className="pw-stat"><div className="pw-stat-num">{stats ? stats.capabilities : '—'}</div><div className="pw-stat-lbl">Capabilities mapped</div></div>
            <div className="pw-stat"><div className="pw-stat-num">{stats ? stats.artifacts : artifacts.length}</div><div className="pw-stat-lbl">Artifacts live</div></div>
          </div>
        </header>

        {error && <div className="app-error pw-err">⚠ {error}</div>}

        <SynthesisCanvas workshopId={workshopId} items={canvasItems} onItemsChange={updateCanvasItems}
          docs={docs} artifacts={artifacts}
          pipeline={pipeline} onGenerate={runPipeline}
          onImportTeams={() => setTeamsOpen(true)}
          lastResult={lastResult} onOpenResult={setViewerDocId}
          transcriptsCount={transcripts.length} />

        {/* Progressive disclosure: each panel exists only once its content
            does — before the first run, the page is hero + canvas + grid. */}
        {(reqs.length > 0 || capmap) && (
          <div className="dw-cols">
            {reqs.length > 0 && (
              <RequirementsPanel workshopId={workshopId} reqs={reqs} onChanged={() => { loadReqs(); loadStats(); }}
                extracting={runningGen === 'extract_reqs'} onExtract={() => runPipeline(['extract_reqs'], '')}
                canRun={canvasItems.length > 0} />
            )}
            {capmap && (
              <CapabilityMapPanel capmap={capmap} building={runningGen === 'capmap'}
                onBuild={() => runPipeline(['capmap'], '')} hasInput={canvasItems.length > 0} />
            )}
          </div>
        )}

        {artifacts.some((a) => a.has_diagram) && (
          <DiagramSection workshopId={workshopId} artifacts={artifacts}
            building={runningGen === 'workflow'} onBuild={() => runPipeline(['workflow'], '')}
            canRun={canvasItems.length > 0} onEdit={setEditDiagram} />
        )}

        <ArtifactsGrid docs={docs} artifacts={artifacts} onView={setViewerDocId} workshopId={workshopId}
          onViewDiagram={setEditDiagram} onViewAnalysis={() => {}} onDelete={deleteArtifact}
          onViewCapmap={viewCapmapArtifact}
          title="Workshop Artifacts" zone="During Workshop" showSources={false} />
      </div>

      {teamsOpen && (
        <TeamsImportModal workshopId={workshopId} onClose={() => setTeamsOpen(false)}
          onImported={() => { loadDocs(); loadStats(); }} />
      )}
      {viewerDocId && (
        <DocumentViewer workshopId={workshopId} docId={viewerDocId} onClose={() => setViewerDocId(null)} />
      )}
      {editDiagram && (
        <DiagramModal xml={editDiagram.xml} diagrams={editDiagram.diagrams}
          title={editDiagram.title} onClose={() => setEditDiagram(null)} />
      )}
      {capmapModal && (
        <div className="pw-modal-backdrop" onClick={() => setCapmapModal(null)}>
          <div className="dw-capmap-modal" onClick={(e) => e.stopPropagation()}>
            <div className="dw-teams-head">
              <span className="pw-ic pw-ic-accent"><Icon name="target" /></span>
              <div>
                <div className="pw-h3">{capmapModal.name}</div>
                <div className="pw-sub">Business capability heat map · {capmapModal.version || 'v1.0'}</div>
              </div>
              <button className="pw-view-btn" onClick={() => setCapmapModal(null)} title="Close"><Icon name="x" /></button>
            </div>
            <CapabilityHeat domains={capmapModal.domains} />
          </div>
        </div>
      )}
      {confirmDelete && (
        <ConfirmDeleteModal name={confirmDelete.name} busy={deleting}
          onCancel={() => setConfirmDelete(null)} onConfirm={performDelete} />
      )}
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════
function RequirementsPanel({ workshopId, reqs, onChanged, extracting, onExtract, canRun }) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ text: '', category: 'Process', moscow: 'should' });
  const [editing, setEditing] = useState(null);   // {id, text, category, moscow}
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  async function addOne() {
    if (!draft.text.trim()) return;
    setBusy(true); setErr('');
    try {
      const res = await apiPost('/api/agents/requirements', { workshop_id: workshopId, ...draft });
      if (!res.ok) { setErr(res.error || 'could not add'); return; }
      setDraft({ text: '', category: 'Process', moscow: 'should' });
      setAdding(false);
      onChanged();
    } catch (e) { setErr(e.message || 'could not add'); } finally { setBusy(false); }
  }

  async function saveEdit() {
    if (!editing) return;
    setBusy(true); setErr('');
    try {
      const res = await apiPatch(`/api/agents/requirements/${editing.id}`, { workshop_id: workshopId, ...editing });
      if (!res.ok) { setErr(res.error || 'could not save'); return; }
      setEditing(null);
      onChanged();
    } catch (e) { setErr(e.message || 'could not save'); } finally { setBusy(false); }
  }

  async function toggleStatus(r) {
    try {
      await apiPatch(`/api/agents/requirements/${r.id}`, {
        workshop_id: workshopId, status: r.status === 'approved' ? 'in_review' : 'approved',
      });
      onChanged();
    } catch { /* transient */ }
  }

  async function removeOne(r) {
    try {
      await apiDelete(`/api/agents/requirements/${r.id}?workshop_id=${workshopId}`);
      onChanged();
    } catch { /* transient */ }
  }

  // Group by source for the reference's "grouped by source" reading.
  const groups = [];
  const byLabel = {};
  reqs.forEach((r) => {
    const key = r.source_label || 'Unattributed';
    if (!byLabel[key]) { byLabel[key] = []; groups.push(key); }
    byLabel[key].push(r);
  });

  return (
    <section className="pw-panel dw-reqs">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="list" /></span>
          <div>
            <div className="pw-h3">Business Requirements — Live</div>
            <div className="pw-sub">Auto-extracted &amp; traced to source utterances · {reqs.length} captured</div>
          </div>
        </div>
        <div className="dw-reqs-actions">
          {reqs.length > 0 && (
            <span className={'pw-pill dw-panel-pill ' + (reqs.every((r) => r.status === 'approved') ? 'pw-pill-ingested' : 'pw-pill-parsing')}>
              {reqs.every((r) => r.status === 'approved') ? 'Approved' : 'In review'}
            </span>
          )}
          <button className="btn solid" onClick={onExtract} disabled={extracting || !canRun}
            title={canRun
              ? 'Mine the canvas selection for requirements'
              : 'Drag documents onto the Synthesis Canvas first'}>
            <Icon name="sparkles" />{extracting ? 'Extracting…' : 'Extract'}
          </button>
        </div>
      </div>

      {err && <div className="app-error pw-err">⚠ {err}</div>}

      {reqs.length === 0 ? (
        <div className="pw-empty">
          No requirements yet — import a transcript and run <b>Extract</b>, or add one manually below.
        </div>
      ) : (
        <div className="dw-req-scroll">
        {groups.map((label) => (
          <div key={label} className="dw-req-group">
            <div className="dw-req-group-hd">
              <Icon name={isTranscript(label) ? 'users' : 'doc-text'} />
              {label}<span className="dw-req-group-n">{byLabel[label].length}</span>
            </div>
            <ul className="dw-req-list">
              {byLabel[label].map((r) => (
                <li key={r.id} className="dw-req-row">
                  {editing && editing.id === r.id ? (
                    <div className="dw-req-edit">
                      <textarea className="pw-instruction" rows={2} value={editing.text}
                        onChange={(e) => setEditing({ ...editing, text: e.target.value })} />
                      <div className="dw-req-add-row">
                        <select className="dw-select" value={editing.category || 'Other'}
                          onChange={(e) => setEditing({ ...editing, category: e.target.value })}>
                          {REQ_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
                        </select>
                        <select className="dw-select" value={editing.moscow}
                          onChange={(e) => setEditing({ ...editing, moscow: e.target.value })}>
                          {Object.entries(MOSCOW_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                        </select>
                        <button className="btn solid" onClick={saveEdit} disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
                        <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="dw-req-head">
                        <span className="dw-req-id">{r.req_id}</span>
                        {r.category && <span className="dw-req-cat">{r.category}</span>}
                        <span className={`dw-moscow dw-moscow-${r.moscow}`}>{MOSCOW_LABEL[r.moscow] || r.moscow}</span>
                        <button className={'dw-req-status' + (r.status === 'approved' ? ' ok' : '')}
                          onClick={() => toggleStatus(r)}
                          title={r.status === 'approved' ? 'Approved — click to send back to review' : 'In review — click to approve'}>
                          {r.status === 'approved' ? 'Approved' : 'In review'}
                        </button>
                        <span className="dw-req-tools">
                          <button className="pw-view-btn" title="Edit requirement"
                            onClick={() => { setEditing({ id: r.id, text: r.text, category: r.category, moscow: r.moscow }); setAdding(false); }}>
                            <Icon name="check" />Edit
                          </button>
                          <button className="pw-view-btn pw-del-btn" title="Delete requirement" onClick={() => removeOne(r)}>
                            <Icon name="trash" />
                          </button>
                        </span>
                      </div>
                      <div className="dw-req-text">{r.text}</div>
                      {r.source_quote && <div className="dw-req-quote">“{r.source_quote}”</div>}
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
        </div>
      )}
      {/* The form opens HERE, in place of the trigger — opening it at the
          top of the panel put it off-screen behind the scroll list, which
          read as the button doing nothing. */}
      {adding ? (
        <div className="dw-req-add">
          <textarea className="pw-instruction" rows={2} autoFocus
            placeholder='e.g. "The system shall auto-assign GMP-qualified staff to open shifts."'
            value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
          <div className="dw-req-add-row">
            <select className="dw-select" value={draft.category}
              onChange={(e) => setDraft({ ...draft, category: e.target.value })}>
              {REQ_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <select className="dw-select" value={draft.moscow}
              onChange={(e) => setDraft({ ...draft, moscow: e.target.value })}>
              {Object.entries(MOSCOW_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <button className="btn solid" onClick={addOne} disabled={busy || !draft.text.trim()}>
              {busy ? 'Adding…' : 'Add'}
            </button>
            <button className="btn" onClick={() => setAdding(false)} disabled={busy}>Cancel</button>
          </div>
        </div>
      ) : (
        <button className="dw-req-addlink" onClick={() => { setAdding(true); setEditing(null); }}>
          <Icon name="plus" />Add requirement
        </button>
      )}
    </section>
  );
}

// ══════════════════════════════════════════════════════════════════════
export function CapabilityHeat({ domains }) {
  return (
    <div className="dw-capmap">
      {(domains || []).map((d) => (
        <div key={d.name} className="dw-cap-domain">
          <div className="dw-cap-domain-hd">{d.name}</div>
          <div className="dw-cap-grid">
            {(d.capabilities || []).map((c) => (
              <div key={c.name} className={`dw-cap-card dw-opp-${c.opportunity}`}>
                <div className="dw-cap-name">{c.name}</div>
                {c.note && <div className="dw-cap-note">{c.note}</div>}
                <div className="dw-cap-foot">
                  <span className="dw-maturity" title={`Maturity ${c.maturity}/5`}>
                    {[1, 2, 3, 4, 5].map((i) => (
                      <i key={i} className={i <= c.maturity ? 'on' : ''} />
                    ))}
                  </span>
                  <span className={`dw-opp-badge dw-opp-${c.opportunity}`}>
                    {c.opportunity === 'high' ? 'High opportunity' : c.opportunity === 'medium' ? 'Medium' : 'Low'}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function CapabilityMapPanel({ capmap, building, onBuild, hasInput }) {
  return (
    <section className="pw-panel dw-capmap-panel">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="target" /></span>
          <div>
            <div className="pw-h3">Business Capability Map{capmap ? ` — ${capmap.version || 'v1.0'}` : ''}</div>
            <div className="pw-sub">
              {capmap
                ? <>Heat-mapped by maturity &amp; optimization opportunity · generated {timeAgo(capmap.created_at)}</>
                : 'Current-state maturity per capability, heat-colored by how much this engagement could improve it'}
            </div>
          </div>
        </div>
        <div className="dw-capmap-actions">
          {capmap && <span className="pw-pill pw-pill-draft dw-panel-pill">Draft</span>}
          <button className="btn solid" onClick={onBuild} disabled={building || !hasInput}
            title={hasInput ? 'Generate from the captured requirements + the canvas selection' : 'Drag documents onto the Synthesis Canvas first'}>
            <Icon name="target" />{building ? 'Mapping…' : capmap ? 'Refresh' : 'Generate map'}
          </button>
        </div>
      </div>
      {capmap ? (
        <>
          <div className="dw-legend dw-legend-row">
            <i className="dw-opp-high" />High opportunity<i className="dw-opp-medium" />Medium<i className="dw-opp-low" />Low
          </div>
          <div className="dw-cap-scroll">
            <CapabilityHeat domains={capmap.domains} />
          </div>
        </>
      ) : (
        <div className="pw-empty">
          No capability map yet — generate one from the captured requirements and workshop documents.
        </div>
      )}
    </section>
  );
}

// ══════════════════════════════════════════════════════════════════════
function DiagramSection({ workshopId, artifacts, building, onBuild, canRun, onEdit }) {
  const withDiagram = artifacts.filter((a) => a.has_diagram);
  const [docId, setDocId] = useState(null);
  const [payload, setPayload] = useState(null);   // {xml, diagrams, title}

  // Default to the newest diagram-bearing artifact; the picker switches.
  const activeId = docId || (withDiagram.length ? withDiagram[withDiagram.length - 1].doc_id : null);

  useEffect(() => {
    if (!activeId) { setPayload(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const d = await apiGet(`/api/agents/document/${activeId}/diagram?workshop_id=${workshopId}`);
        if (!cancelled && d && d.ok) {
          const meta = withDiagram.find((a) => a.doc_id === activeId);
          setPayload({ xml: d.xml, diagrams: d.diagrams || [], title: (meta && meta.name) || 'Workflow' });
        }
      } catch { /* transient */ }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, workshopId, artifacts.length]);

  return (
    <section className="pw-panel dw-flow">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="flow" /></span>
          <div>
            <div className="pw-h3">Process Flow</div>
            <div className="pw-sub">Swimlane view of the workshop's process understanding — rendered natively,
              editable in draw.io</div>
          </div>
        </div>
        <div className="dw-flow-actions">
          {withDiagram.length > 1 && (
            <select className="dw-select" value={activeId || ''} onChange={(e) => setDocId(e.target.value)}>
              {withDiagram.map((a) => <option key={a.doc_id} value={a.doc_id}>{a.name}</option>)}
            </select>
          )}
          <button className="btn solid" onClick={onBuild} disabled={building || !canRun}
            title={canRun ? 'Map the processes in the canvas selection' : 'Drag documents onto the Synthesis Canvas first'}>
            <Icon name="flow" />{building ? 'Building…' : withDiagram.length ? 'Build another' : 'Build process flow'}
          </button>
        </div>
      </div>
      {payload && payload.diagrams.length ? (
        <DiagramCanvas diagrams={payload.diagrams} title={payload.title} xml={payload.xml} onEdit={onEdit} />
      ) : (
        <div className="pw-empty">
          No process flow yet — <b>Build process flow</b> mines every source and research document for the
          end-to-end processes worth mapping.
        </div>
      )}
    </section>
  );
}
