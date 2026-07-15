'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPatch, apiPost } from '../../lib/api';
import { Icon } from '../../lib/icons';
import DocumentViewer from '../preworkshop/DocumentViewer';
import { ArtifactsGrid, ConfirmDeleteModal } from '../preworkshop/PreWorkshopDashboard';
import ArtifactExplorer from '../ArtifactExplorer';
import SynthesisCanvas, { loadCanvasSet } from '../duringworkshop/SynthesisCanvas';
import MilestoneTimeline from './MilestoneTimeline';
import RoiChart from './RoiChart';
import RiskScatter from './RiskScatter';
import '../../shared.css';

// The Proposal & Planning dashboard — phase 4 of 4 ("SOW · ROI · Risk ·
// Team"). Same layout language as the other phase dashboards; the four
// generators persist structured payloads into generated_docs.
// proposal_json (routes/proposal.py serves the latest per agent):
//   sow  -> milestone timeline        roi  -> value chart + drivers
//   risk -> benefit-risk scatter      team -> squad table
// All four compose from the validated-scope chain the earlier phases
// produced (requirements + capmap + backlog + accepted opportunities).

const PROPOSAL_GENERATORS = [
  { id: 'sow', label: 'SOW', icon: 'doc-text' },
  { id: 'roi', label: 'ROI & value', icon: 'chart' },
  { id: 'risk', label: 'Benefit–risk', icon: 'shield' },
  { id: 'team', label: 'Team', icon: 'users' },
];

// ══════════════════════════════════════════════════════════════════════
export default function ProposalPlanningDashboard({ user, workshopId, onBoardView }) {
  const [docs, setDocs] = useState([]);
  const [artifacts, setArtifacts] = useState([]);
  const [proposal, setProposal] = useState(null);   // {sow, roi, risk, team} | null while loading
  const [error, setError] = useState('');

  const [canvasItems, setCanvasItems] = useState([]);
  const [pipeline, setPipeline] = useState(null);
  const [lastResult, setLastResult] = useState(null);
  const [weeks, setWeeks] = useState(20);           // engagement length — feeds sow milestones + roi horizon phrasing
  const runningGen = pipeline ? (pipeline.find((s) => s.status === 'running') || {}).id || null : null;

  const [viewerDocId, setViewerDocId] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);
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

  const loadProposal = useCallback(async () => {
    try {
      const d = await apiGet(`/api/proposal?workshop_id=${workshopId}`);
      if (d && d.ok) setProposal({ sow: d.sow, roi: d.roi, risk: d.risk, team: d.team });
    } catch { /* transient */ }
  }, [workshopId]);

  const refreshAll = useCallback(() => {
    loadDocs(); loadArtifacts(); loadProposal();
  }, [loadDocs, loadArtifacts, loadProposal]);

  useEffect(() => { refreshAll(); }, [refreshAll]);
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
    fd.append('phase', 'Proposal & Planning');
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

  // One generator call — empty canvas is a valid run (the requirements
  // table, capability map, backlog tree and accepted opportunities ride
  // along server-side; see _proposal_context). The engagement length is
  // folded into the run's extra input, which the sow/roi prompts read.
  async function runOne(agentId, prompt) {
    const doc_ids = canvasItems.length ? {
      sources: canvasItems.filter((i) => i.kind === 'source').map((i) => i.doc_id),
      generated: canvasItems.filter((i) => i.kind === 'generated').map((i) => i.doc_id),
    } : undefined;
    const extra = [`Engagement length: ${weeks} weeks.`, (prompt || '').trim()]
      .filter(Boolean).join(' ');
    const res = await apiPost('/api/agents/run', {
      agent_id: agentId, workshop_id: workshopId, context: { zone: 'Proposal & Planning' },
      extra,
      ...(doc_ids ? { options: { doc_ids } } : {}),
    });
    if (!res.ok) throw new Error(res.error || 'failed');
    const draft = res.draft || {};
    loadProposal();
    const p = draft.proposal || {};
    const note = agentId === 'sow' ? `${(p.milestones || []).length} milestones`
      : agentId === 'roi' ? (p.net_value_label ? `net ${p.net_value_label}` : 'estimate (see basis)')
      : agentId === 'risk' ? `${(p.items || []).length} items scored`
      : `${(p.roles || []).reduce((n, r) => n + r.count, 0)} people`;
    return { note, docId: draft.node && draft.node.docId };
  }

  const pipelineBusy = useRef(false);
  async function runPipeline(agentIds, prompt) {
    if (pipelineBusy.current) return;
    pipelineBusy.current = true;
    try {
      const order = PROPOSAL_GENERATORS.filter((g) => agentIds.includes(g.id));
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
    } finally {
      pipelineBusy.current = false;
    }
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

  const { sow = null, roi = null, risk = null, team = null } = proposal || {};
  const teamSize = team ? team.roles.reduce((n, r) => n + r.count, 0) : null;
  const currency = (roi && roi.currency) || '£';

  return (
    <div className="pw-dash">
      <ArtifactExplorer workshopId={workshopId} docs={docs} artifacts={artifacts}
        activePhase="Proposal & Planning"
        onAdd={() => fileInputRef.current?.click()}
        onView={setViewerDocId}
        onDelete={deleteArtifact}
        onAddToCanvas={addToCanvas} />
      <input ref={fileInputRef} type="file" style={{ display: 'none' }} onChange={handleUpload}
        accept=".pdf,.docx,.xlsx,.pptx,.csv,.html,.txt,.md,.vtt,.zip" />

      <div className="pw-scroll">
        <header className="pw-hero">
          <div className="pw-hero-txt">
            <h1>Proposal &amp; Delivery Planning</h1>
            <p>Convert the validated scope into a client-ready proposal: statement of work, a
              3-year ROI model, benefit–risk analysis, and a recommended delivery team shaped to
              the solution's complexity.</p>
            {onBoardView && (
              <button className="dw-board-toggle" onClick={onBoardView}
                title="Open the freeform canvas board for this phase">
                <Icon name="flow" />Board view
              </button>
            )}
          </div>
          <div className="pw-stats">
            <div className="pw-stat"><div className="pw-stat-num">{roi && roi.net_value_label ? roi.net_value_label : '—'}</div><div className="pw-stat-lbl">{roi ? `${roi.horizon_years}-yr net value` : 'Net value'}</div></div>
            <div className="pw-stat"><div className="pw-stat-num">{roi && roi.payback_months != null ? `${roi.payback_months} mo` : '—'}</div><div className="pw-stat-lbl">Payback</div></div>
            <div className="pw-stat"><div className="pw-stat-num">{teamSize ?? '—'}</div><div className="pw-stat-lbl">Team size</div></div>
            <div className="pw-stat"><div className="pw-stat-num">{sow ? sow.milestones.length : '—'}</div><div className="pw-stat-lbl">Milestones</div></div>
          </div>
        </header>

        {error && <div className="app-error pw-err">⚠ {error}</div>}

        <SynthesisCanvas workshopId={workshopId} items={canvasItems} onItemsChange={updateCanvasItems}
          docs={docs} artifacts={artifacts}
          pipeline={pipeline} onGenerate={runPipeline}
          lastResult={lastResult} onOpenResult={setViewerDocId}
          generators={PROPOSAL_GENERATORS}
          title="Proposal Generator"
          subtitle={(
            <>
              Generates from the validated scope (requirements · capability map · backlog ·
              accepted opportunities) — optionally scope by dragging documents here.
              <span className="pp-weeks">
                Engagement length
                <input type="number" min="2" max="104" value={weeks}
                  onChange={(e) => setWeeks(Math.max(2, Math.min(104, Number(e.target.value) || 20)))} />
                weeks
              </span>
            </>
          )}
          emptyOk />

        <SowPanel workshopId={workshopId} sow={sow} onChanged={loadProposal}
          generating={runningGen === 'sow'} onGenerate={() => runPipeline(['sow'], '')}
          onOpenDoc={setViewerDocId} />

        <div className="dw-cols pp-cols-roi">
          <RoiPanel roi={roi} currency={currency} generating={runningGen === 'roi'}
            onGenerate={() => runPipeline(['roi'], '')} onOpenDoc={setViewerDocId} />
          <DriversPanel roi={roi} />
        </div>

        <div className="dw-cols">
          <RiskPanel risk={risk} generating={runningGen === 'risk'}
            onGenerate={() => runPipeline(['risk'], '')} onOpenDoc={setViewerDocId} />
          <TeamPanel workshopId={workshopId} team={team} onChanged={loadProposal}
            generating={runningGen === 'team'} onGenerate={() => runPipeline(['team'], '')} />
        </div>

        <ArtifactsGrid docs={docs} artifacts={artifacts} onView={setViewerDocId} workshopId={workshopId}
          onViewDiagram={() => {}} onViewAnalysis={() => {}} onDelete={deleteArtifact}
          title="Proposal Artifacts" zone="Proposal & Planning" showSources={false} />
      </div>

      {viewerDocId && (
        <DocumentViewer workshopId={workshopId} docId={viewerDocId} onClose={() => setViewerDocId(null)} />
      )}
      {confirmDelete && (
        <ConfirmDeleteModal name={confirmDelete.name} busy={deleting}
          onCancel={() => setConfirmDelete(null)} onConfirm={performDelete} />
      )}
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════
function PanelHead({ icon, title, sub, children }) {
  return (
    <div className="pw-panel-head">
      <div className="pw-panel-ttl">
        <span className="pw-ic pw-ic-accent"><Icon name={icon} /></span>
        <div>
          <div className="pw-h3">{title}</div>
          <div className="pw-sub">{sub}</div>
        </div>
      </div>
      <div className="pp-panel-actions">{children}</div>
    </div>
  );
}

function SowPanel({ workshopId, sow, onChanged, generating, onGenerate, onOpenDoc }) {
  return (
    <section className="pw-panel pp-sow">
      <PanelHead icon="doc-text" title="Statement of Work — Milestones"
        sub="Derived from the validated backlog & capability map">
        {sow && <span className="pw-pill pw-pill-draft dw-panel-pill">Draft</span>}
        {sow && (
          <>
            <button className="pw-view-btn" onClick={() => onOpenDoc(sow.doc_id)} title="Open the full SOW document">
              <Icon name="eye" />Open
            </button>
            <a className="pw-view-btn" href={`/api/agents/document/${sow.doc_id}/word?workshop_id=${workshopId}`}
              title="Download as Word"><Icon name="upload" />Word</a>
          </>
        )}
        <button className="btn solid" onClick={onGenerate} disabled={generating}
          title="Draft the SOW from the validated scope (regenerating replaces the milestones)">
          <Icon name="sparkles" />{generating ? 'Drafting…' : sow ? 'Redraft' : 'Draft SOW'}
        </button>
      </PanelHead>
      {sow ? (
        <MilestoneTimeline workshopId={workshopId} sow={sow} onChanged={onChanged} />
      ) : (
        <div className="pw-empty">
          No SOW yet — <b>Draft SOW</b> turns the validated backlog and capability map into a
          milestone-timed statement of work.
        </div>
      )}
    </section>
  );
}

function RoiPanel({ roi, currency, generating, onGenerate, onOpenDoc }) {
  return (
    <section className="pw-panel pp-roi">
      <PanelHead icon="chart" title="ROI & Value Estimate"
        sub={`Cumulative value vs. delivery cost (${currency}M, ${roi ? roi.horizon_years : 3}-year) · estimate`}>
        {roi && roi.net_value_label && (
          <span className="pw-pill pw-pill-ingested dw-panel-pill">Net +{roi.net_value_label.replace(/^\+/, '')}</span>
        )}
        {roi && (
          <button className="pw-view-btn" onClick={() => onOpenDoc(roi.doc_id)} title="Open the value narrative">
            <Icon name="eye" />Open
          </button>
        )}
        <button className="btn solid" onClick={onGenerate} disabled={generating}
          title="Model the return from the captured requirements and pain points — every figure is a labeled estimate">
          <Icon name="sparkles" />{generating ? 'Modeling…' : roi ? 'Remodel' : 'Model ROI'}
        </button>
      </PanelHead>
      {roi && roi.series && roi.series.length > 0 ? (
        <>
          <RoiChart series={roi.series} currency={currency} />
          {roi.basis && <div className="pp-basis">Basis: {roi.basis}</div>}
        </>
      ) : roi ? (
        <div className="pw-empty">
          The material gave too little cost/value signal for a chart —
          {roi.basis ? <> {roi.basis}</> : ' add costed documents and remodel.'}
        </div>
      ) : (
        <div className="pw-empty">
          No ROI model yet — <b>Model ROI</b> estimates the 3-year return from the captured
          requirements and pain points, honestly labeled.
        </div>
      )}
    </section>
  );
}

function DriversPanel({ roi }) {
  const drivers = (roi && roi.drivers) || [];
  return (
    <section className="pw-panel pp-drivers">
      <PanelHead icon="target" title="Value Drivers" sub="Where the return comes from" />
      {drivers.length === 0 ? (
        <div className="pw-empty">Drivers appear with the ROI model.</div>
      ) : (
        <ul className="pp-driver-list">
          {drivers.map((d) => (
            <li key={d.name} className="pp-driver">
              <div className="pp-driver-row">
                <span className="pp-driver-name">{d.name}</span>
                <span className="pp-driver-pct">{d.pct}%</span>
              </div>
              <div className="pp-driver-bar"><i style={{ width: `${d.pct}%` }} /></div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function RiskPanel({ risk, generating, onGenerate, onOpenDoc }) {
  return (
    <section className="pw-panel pp-risk">
      <PanelHead icon="shield" title="Benefit–Risk Analysis"
        sub="Weighted benefit vs. delivery & adoption risk">
        {risk && (
          <button className="pw-view-btn" onClick={() => onOpenDoc(risk.doc_id)} title="Open the full analysis">
            <Icon name="eye" />Open
          </button>
        )}
        <button className="btn solid" onClick={onGenerate} disabled={generating}
          title="Score the named risks and levers from the discovery material">
          <Icon name="sparkles" />{generating ? 'Scoring…' : risk ? 'Rescore' : 'Analyze'}
        </button>
      </PanelHead>
      {risk && risk.items && risk.items.length > 0 ? (
        <RiskScatter items={risk.items} />
      ) : (
        <div className="pw-empty">
          No analysis yet — <b>Analyze</b> scores the engagement's named risks and levers on
          benefit vs. delivery &amp; adoption risk.
        </div>
      )}
    </section>
  );
}

function TeamPanel({ workshopId, team, onChanged, generating, onGenerate }) {
  const [editing, setEditing] = useState(null);   // {idx, count, allocation_pct}
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const roles = (team && team.roles) || [];

  async function save() {
    if (!editing) return;
    setBusy(true); setErr('');
    const next = roles.map((r, i) => i === editing.idx
      ? { ...r, count: Number(editing.count) || r.count, allocation_pct: Number(editing.allocation_pct) || r.allocation_pct }
      : r);
    try {
      const res = await apiPatch(`/api/proposal/${team.doc_id}`, { workshop_id: workshopId, roles: next });
      if (!res.ok) { setErr(res.error || 'could not save'); return; }
      setEditing(null);
      onChanged();
    } catch (e) { setErr(e.message || 'could not save'); } finally { setBusy(false); }
  }

  async function removeOne(idx) {
    if (roles.length <= 1) return;
    setErr('');
    try {
      const res = await apiPatch(`/api/proposal/${team.doc_id}`, {
        workshop_id: workshopId, roles: roles.filter((_, i) => i !== idx),
      });
      if (!res.ok) { setErr(res.error || 'could not save'); return; }
      onChanged();
    } catch (e) { setErr(e.message || 'could not save'); }
  }

  const teamSize = roles.reduce((n, r) => n + r.count, 0);

  return (
    <section className="pw-panel pp-team">
      <PanelHead icon="users" title="Suggested Team Composition"
        sub={roles.length ? `Recommended squad · ${teamSize} people` : 'Recommended squad shaped to the scope'}>
        <button className="btn solid" onClick={onGenerate} disabled={generating}
          title="Size the squad from the backlog breadth and technical shape">
          <Icon name="sparkles" />{generating ? 'Sizing…' : roles.length ? 'Resize' : 'Suggest team'}
        </button>
      </PanelHead>
      {err && <div className="app-error pw-err">⚠ {err}</div>}
      {roles.length === 0 ? (
        <div className="pw-empty">
          No team yet — <b>Suggest team</b> recommends roles, counts and allocations from the
          validated scope.
        </div>
      ) : (
        <table className="pp-team-table">
          <thead>
            <tr><th>Role</th><th>Count</th><th>Allocation</th><th /></tr>
          </thead>
          <tbody>
            {roles.map((r, i) => (
              <tr key={r.role} title={r.why || undefined}>
                <td className="pp-team-role">{r.role}</td>
                {editing && editing.idx === i ? (
                  <>
                    <td><input className="pp-team-input" type="number" min="1" max="6" value={editing.count}
                      onChange={(e) => setEditing({ ...editing, count: e.target.value })} /></td>
                    <td><input className="pp-team-input" type="number" min="5" max="100" step="5" value={editing.allocation_pct}
                      onChange={(e) => setEditing({ ...editing, allocation_pct: e.target.value })} />%</td>
                    <td className="pp-team-tools">
                      <button className="btn solid" onClick={save} disabled={busy}>{busy ? '…' : 'Save'}</button>
                      <button className="btn" onClick={() => setEditing(null)} disabled={busy}>Cancel</button>
                    </td>
                  </>
                ) : (
                  <>
                    <td><span className="pp-team-count">{r.count}</span></td>
                    <td className="pp-team-alloc">{r.allocation_pct}%</td>
                    <td className="pp-team-tools">
                      <button className="pw-view-btn" title="Edit count / allocation"
                        onClick={() => setEditing({ idx: i, count: r.count, allocation_pct: r.allocation_pct })}>
                        <Icon name="check" />
                      </button>
                      {roles.length > 1 && (
                        <button className="pw-view-btn pw-del-btn" title="Remove role" onClick={() => removeOne(i)}>
                          <Icon name="trash" />
                        </button>
                      )}
                    </td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
