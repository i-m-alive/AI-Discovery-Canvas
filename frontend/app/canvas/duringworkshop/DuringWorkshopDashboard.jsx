'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPatch, apiPost } from '../../lib/api';
import { Icon } from '../../lib/icons';
import DocumentViewer from '../preworkshop/DocumentViewer';
import DrawioViewer from '../preworkshop/DrawioViewer';
import {
  ArtifactsGrid, ConfirmDeleteModal, SourceArtifactsPanel, timeAgo,
} from '../preworkshop/PreWorkshopDashboard';
import DiagramCanvas from './DiagramCanvas';
import TeamsImportModal from './TeamsImportModal';
import '../../shared.css';

const MOSCOW_LABEL = { must: 'Must', should: 'Should', could: 'Could', wont: "Won't" };
const REQ_CATEGORIES = ['Process', 'Data', 'Integration', 'Reporting', 'Security', 'UX', 'Compliance', 'Other'];

function isTranscript(name) {
  return /^teams\s*—|^teams\s*--|\.vtt$|transcript/i.test(name || '');
}

// ══════════════════════════════════════════════════════════════════════
export default function DuringWorkshopDashboard({ user, workshopId, onBoardView }) {
  const [docs, setDocs] = useState([]);
  const [artifacts, setArtifacts] = useState([]);
  const [stats, setStats] = useState(null);
  const [reqs, setReqs] = useState([]);
  const [capmap, setCapmap] = useState(null);
  const [error, setError] = useState('');

  const [teamsOpen, setTeamsOpen] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [lastExtract, setLastExtract] = useState(null);   // {added, total}
  const [buildingMap, setBuildingMap] = useState(false);
  const [buildingBrd, setBuildingBrd] = useState(false);
  const [buildingFlow, setBuildingFlow] = useState(false);

  const [viewerDocId, setViewerDocId] = useState(null);
  const [editDiagram, setEditDiagram] = useState(null);    // {xml,title} -> DrawioViewer
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

  async function runExtract() {
    setExtracting(true);
    setError('');
    setLastExtract(null);
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'extract_reqs', workshop_id: workshopId, context: { zone: 'During Workshop' },
      });
      if (!res.ok) { setError(res.error || 'extraction failed'); return; }
      setLastExtract({ added: (res.draft.requirements || []).length, total: res.draft.extracted_count || 0 });
      loadReqs(); loadStats();
    } catch (err) {
      setError(err.message || 'extraction failed');
    } finally {
      setExtracting(false);
    }
  }

  async function runCapmap() {
    setBuildingMap(true);
    setError('');
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'capmap', workshop_id: workshopId, context: { zone: 'During Workshop' },
      });
      if (!res.ok) { setError(res.error || 'capability map failed'); return; }
      loadCapmap(); loadArtifacts(); loadStats();
    } catch (err) {
      setError(err.message || 'capability map failed');
    } finally {
      setBuildingMap(false);
    }
  }

  async function runBrd() {
    setBuildingBrd(true);
    setError('');
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'brd', workshop_id: workshopId, context: { zone: 'During Workshop' },
      });
      if (!res.ok) { setError(res.error || 'BRD assembly failed'); return; }
      loadArtifacts(); loadStats();
      if (res.draft && res.draft.node && res.draft.node.docId) setViewerDocId(res.draft.node.docId);
    } catch (err) {
      setError(err.message || 'BRD assembly failed');
    } finally {
      setBuildingBrd(false);
    }
  }

  async function runFlow() {
    setBuildingFlow(true);
    setError('');
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'workflow', workshop_id: workshopId, context: { zone: 'During Workshop' },
      });
      if (!res.ok) { setError(res.error || 'process flow failed'); return; }
      loadArtifacts(); loadStats();
    } catch (err) {
      setError(err.message || 'process flow failed');
    } finally {
      setBuildingFlow(false);
    }
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
      <SourceArtifactsPanel docs={docs} onAdd={() => fileInputRef.current?.click()}
        onView={setViewerDocId} onDelete={deleteArtifact}
        extraAction={(
          <button className="btn solid dw-teams-cta" onClick={() => setTeamsOpen(true)}>
            <Icon name="users" />Import from Teams
          </button>
        )}
        emptyText={(<>No sources yet for this workshop.<br />Import a Teams transcript or upload a document.</>)} />
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

        <CaptureBanner transcripts={transcripts} extracting={extracting} lastExtract={lastExtract}
          onImport={() => setTeamsOpen(true)} onExtract={runExtract} />

        <RequirementsPanel workshopId={workshopId} reqs={reqs} onChanged={() => { loadReqs(); loadStats(); }}
          extracting={extracting} onExtract={runExtract} hasTranscripts={transcripts.length > 0} />

        <CapabilityMapPanel capmap={capmap} building={buildingMap} onBuild={runCapmap}
          hasInput={reqs.length > 0 || docs.length > 0} />

        <DiagramSection workshopId={workshopId} artifacts={artifacts}
          building={buildingFlow} onBuild={runFlow} onEdit={setEditDiagram} />

        <section className="pw-panel dw-generate">
          <div className="pw-panel-ttl">
            <span className="pw-ic pw-ic-accent"><Icon name="doc-text" /></span>
            <div>
              <div className="pw-h3">Assemble the BRD</div>
              <div className="pw-sub">Composes the captured requirements, the capability map and the workshop
                context into a formal Business Requirements Document — Word-exportable, like every artifact.</div>
            </div>
            <button className="btn solid dw-brd-btn" onClick={runBrd} disabled={buildingBrd || reqs.length === 0}
              title={reqs.length === 0 ? 'Capture requirements first' : 'Assemble the BRD'}>
              <Icon name="doc-text" />{buildingBrd ? 'Assembling…' : 'Assemble BRD'}
            </button>
          </div>
        </section>

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
        <DrawioViewer xml={editDiagram.xml} title={editDiagram.title} onClose={() => setEditDiagram(null)} />
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
function CaptureBanner({ transcripts, extracting, lastExtract, onImport, onExtract }) {
  if (extracting) {
    return (
      <div className="dw-capture dw-capture-busy">
        <span className="dw-capture-dot" />
        <div className="dw-capture-txt">
          <b>Extracting requirements…</b> reading {transcripts.length || 'the'} transcript{transcripts.length === 1 ? '' : 's'} and
          the pre-workshop context — new requirements land in the live table below.
        </div>
      </div>
    );
  }
  if (transcripts.length === 0) {
    return (
      <div className="dw-capture dw-capture-empty">
        <span className="pw-ic pw-ic-accent"><Icon name="users" /></span>
        <div className="dw-capture-txt">
          <b>No capture yet.</b> Import a Teams meeting transcript to start turning discussion into
          requirements. Uploads (notes, whiteboard exports) work too.
        </div>
        <button className="btn solid" onClick={onImport}><Icon name="users" />Import from Teams</button>
      </div>
    );
  }
  const latest = transcripts[transcripts.length - 1];
  return (
    <div className="dw-capture">
      <span className="dw-capture-check"><Icon name="check-circle" /></span>
      <div className="dw-capture-txt">
        <b>{transcripts.length} transcript{transcripts.length === 1 ? '' : 's'} imported</b>
        {latest ? <> · latest: {latest.name} ({timeAgo(latest.uploaded_at)})</> : null}
        {lastExtract && (
          <span className="dw-capture-note"> · last extraction added {lastExtract.added} new
            {lastExtract.total > lastExtract.added ? ` (${lastExtract.total - lastExtract.added} duplicates skipped)` : ''}</span>
        )}
      </div>
      <button className="btn" onClick={onImport}><Icon name="users" />Import more</button>
      <button className="btn solid" onClick={onExtract}><Icon name="sparkles" />Extract requirements</button>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════
function RequirementsPanel({ workshopId, reqs, onChanged, extracting, onExtract, hasTranscripts }) {
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
            <div className="pw-sub">{reqs.length} captured · each traced to the transcript or document it came from</div>
          </div>
        </div>
        <div className="dw-reqs-actions">
          <button className="btn" onClick={() => { setAdding((a) => !a); setEditing(null); }}>
            <Icon name="plus" />Add requirement
          </button>
          <button className="btn solid" onClick={onExtract} disabled={extracting || !hasTranscripts}
            title={hasTranscripts ? 'Mine the imported transcripts for requirements' : 'Import a transcript first'}>
            <Icon name="sparkles" />{extracting ? 'Extracting…' : 'Extract requirements'}
          </button>
        </div>
      </div>

      {err && <div className="app-error pw-err">⚠ {err}</div>}

      {adding && (
        <div className="dw-req-add">
          <textarea className="pw-instruction" rows={2} placeholder='e.g. "The system shall auto-assign GMP-qualified staff to open shifts."'
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
            <button className="btn" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </div>
      )}

      {reqs.length === 0 ? (
        <div className="pw-empty">
          No requirements yet — import a transcript and run <b>Extract requirements</b>, or add one manually.
        </div>
      ) : (
        groups.map((label) => (
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
        ))
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
            <div className="pw-h3">Business Capability Map</div>
            <div className="pw-sub">
              {capmap
                ? <>{capmap.version || 'v1.0'} · generated {timeAgo(capmap.created_at)} · maturity 1–5, heat = improvement opportunity</>
                : 'Current-state maturity per capability, heat-colored by how much this engagement could improve it'}
            </div>
          </div>
        </div>
        <div className="dw-capmap-actions">
          {capmap && (
            <span className="dw-legend">
              <i className="dw-opp-high" />High<i className="dw-opp-medium" />Medium<i className="dw-opp-low" />Low
            </span>
          )}
          <button className="btn solid" onClick={onBuild} disabled={building || !hasInput}
            title={hasInput ? 'Generate from the captured requirements + workshop documents' : 'Capture requirements or add sources first'}>
            <Icon name="target" />{building ? 'Mapping…' : capmap ? 'Refresh map' : 'Generate map'}
          </button>
        </div>
      </div>
      {capmap ? (
        <CapabilityHeat domains={capmap.domains} />
      ) : (
        <div className="pw-empty">
          No capability map yet — generate one from the captured requirements and workshop documents.
        </div>
      )}
    </section>
  );
}

// ══════════════════════════════════════════════════════════════════════
function DiagramSection({ workshopId, artifacts, building, onBuild, onEdit }) {
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
          <button className="btn solid" onClick={onBuild} disabled={building}>
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
