'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiGet, apiPost } from '../../lib/api';
import { Icon } from '../../lib/icons';
import DocumentViewer from './DocumentViewer';
import '../../shared.css';

const STATUS_LABEL = { queued: 'Queued', parsing: 'Parsing', ingested: 'Ingested', failed: 'Failed' };
const RESEARCH_STEP_ORDER = ['ingest', 'extract', 'queries', 'search', 'synthesize'];

function timeAgo(unixSeconds) {
  if (!unixSeconds) return '';
  const diff = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  return `${Math.floor(diff / 86400)} d ago`;
}

function extOf(name) {
  const m = /\.([a-z0-9]+)$/i.exec(name || '');
  return m ? m[1].toLowerCase() : '';
}

// One badge label + icon + accent color per file type — "proper icons
// for each component" extends to per-file-type recognition too, not
// just section headers.
const FILE_TYPE = {
  pdf: { label: 'PDF', icon: 'doc-text', bg: '#fbecea', fg: '#c0463b' },
  docx: { label: 'DOCX', icon: 'doc-text', bg: '#eaf2fb', fg: '#2f6fb3' },
  doc: { label: 'DOC', icon: 'doc-text', bg: '#eaf2fb', fg: '#2f6fb3' },
  xlsx: { label: 'XLSX', icon: 'list', bg: '#eaf6f0', fg: '#2f8f5b' },
  xls: { label: 'XLS', icon: 'list', bg: '#eaf6f0', fg: '#2f8f5b' },
  csv: { label: 'CSV', icon: 'list', bg: '#eaf6f0', fg: '#2f8f5b' },
  pptx: { label: 'PPTX', icon: 'flow', bg: '#faf1e1', fg: '#c9881f' },
  ppt: { label: 'PPT', icon: 'flow', bg: '#faf1e1', fg: '#c9881f' },
  txt: { label: 'TXT', icon: 'doc-text', bg: '#eef1f5', fg: '#6b7280' },
  md: { label: 'MD', icon: 'doc-text', bg: '#eef1f5', fg: '#6b7280' },
  html: { label: 'HTML', icon: 'doc-text', bg: '#efedfd', fg: '#6d5ce8' },
  zip: { label: 'ZIP', icon: 'folder', bg: '#efedfd', fg: '#6d5ce8' },
};
function fileType(name) {
  const ext = extOf(name);
  return FILE_TYPE[ext] || { label: ext ? ext.toUpperCase() : 'FILE', icon: 'doc-text', bg: '#eef1f5', fg: '#6b7280' };
}

function downloadDrawio(diagram, name) {
  if (!diagram || !diagram.xml) return;
  const blob = new Blob([diagram.xml], { type: 'application/xml' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${(name || 'workflow').replace(/[^a-z0-9-_]+/gi, '_')}.drawio`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export default function PreWorkshopDashboard({ user, workshopId }) {
  const [docs, setDocs] = useState([]);
  const [artifacts, setArtifacts] = useState([]);
  const [run, setRun] = useState(null);
  const [runningResearch, setRunningResearch] = useState(false);
  const [runningWorkflow, setRunningWorkflow] = useState(false);
  const [workflowResult, setWorkflowResult] = useState(null);
  const [instruction, setInstruction] = useState('');
  const [error, setError] = useState('');
  const [viewerDocId, setViewerDocId] = useState(null);
  const fileInputRef = useRef(null);

  const loadDocs = useCallback(async () => {
    try {
      const data = await apiGet(`/api/agents/prepare-docs?workshop_id=${workshopId}`);
      if (data && data.ok) setDocs(data.docs || []);
    } catch { /* transient — next poll/retry picks it up */ }
  }, [workshopId]);

  const loadArtifacts = useCallback(async () => {
    try {
      const data = await apiGet(`/api/agents/generated-docs?workshop_id=${workshopId}`);
      if (data && data.ok) setArtifacts(data.docs || []);
    } catch { /* transient */ }
  }, [workshopId]);

  const loadChain = useCallback(async () => {
    try {
      const data = await apiGet(`/api/agents/research-chain?workshop_id=${workshopId}`);
      if (data && data.ok) setRun(data.run);
    } catch { /* transient */ }
  }, [workshopId]);

  useEffect(() => { loadDocs(); loadArtifacts(); loadChain(); }, [loadDocs, loadArtifacts, loadChain]);

  // Poll ingestion status while any source doc is still in flight.
  useEffect(() => {
    const pending = docs.some((d) => d.status === 'queued' || d.status === 'parsing');
    if (!pending) return;
    const t = setInterval(loadDocs, 4000);
    return () => clearInterval(t);
  }, [docs, loadDocs]);

  // Poll the research chain while a run is in flight.
  useEffect(() => {
    if (!run || run.status !== 'running') return;
    const t = setInterval(loadChain, 2000);
    return () => clearInterval(t);
  }, [run, loadChain]);

  const prevRunStatus = useRef(null);
  useEffect(() => {
    if (run && run.status === 'done' && prevRunStatus.current !== 'done') loadArtifacts();
    prevRunStatus.current = run ? run.status : null;
  }, [run, loadArtifacts]);

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
      loadDocs();
    } catch (err) {
      setError(err.message || 'upload failed');
    }
  }

  async function runResearch() {
    setRunningResearch(true);
    setError('');
    setRun({ status: 'running', steps: [], insights: [], confidence: null });
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'deepresearch', workshop_id: workshopId, context: { zone: 'Pre-Workshop' },
        extra: instruction.trim() || undefined,
      });
      if (!res.ok) setError(res.error || 'research failed');
      await loadChain();
      await loadArtifacts();
    } catch (err) {
      setError(err.message || 'research failed');
    } finally {
      setRunningResearch(false);
    }
  }

  async function runWorkflow() {
    setRunningWorkflow(true);
    setError('');
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'workflow', workshop_id: workshopId, context: { zone: 'Pre-Workshop' },
      });
      if (!res.ok) { setError(res.error || 'workflow build failed'); return; }
      setWorkflowResult(res.draft);
      loadArtifacts();
    } catch (err) {
      setError(err.message || 'workflow build failed');
    } finally {
      setRunningWorkflow(false);
    }
  }

  const insights = (run && run.insights) || [];
  // Real, server-computed count (see research_runs.web_count) — falls
  // back to counting distinct cited web labels only for older runs
  // recorded before that column existed.
  let webCount = run && run.web_count != null ? run.web_count : null;
  if (webCount == null) {
    const webLabels = new Set();
    insights.forEach((i) => (i.source_refs || []).forEach((r) => { if (r.type === 'web') webLabels.add(r.label); }));
    webCount = webLabels.size;
  }
  const confidence = run && run.confidence != null ? run.confidence : null;

  return (
    <div className="pw-dash">
      <SourceArtifactsPanel docs={docs} onAdd={() => fileInputRef.current?.click()} onView={setViewerDocId} />
      <input ref={fileInputRef} type="file" style={{ display: 'none' }} onChange={handleUpload}
            accept=".pdf,.docx,.xlsx,.pptx,.csv,.html,.txt,.md,.zip" />

      <div className="pw-scroll">
        <header className="pw-hero">
          <div className="pw-hero-txt">
            <h1>Pre-Workshop Intelligence</h1>
            <p>Ingest everything the client shared, internalize it, then run context-grounded web
              research. The research agent reasons from the ingested artifacts — not generic queries.</p>
          </div>
          <div className="pw-stats">
            <div className="pw-stat"><div className="pw-stat-num">{docs.length}</div><div className="pw-stat-lbl">Sources ingested</div></div>
            <div className="pw-stat"><div className="pw-stat-num">{webCount}</div><div className="pw-stat-lbl">Web sources</div></div>
            <div className="pw-stat"><div className="pw-stat-num">{confidence != null ? confidence + '%' : '—'}</div><div className="pw-stat-lbl">Research confidence</div></div>
            <div className="pw-stat"><div className="pw-stat-num">{artifacts.length}</div><div className="pw-stat-lbl">Draft artifacts</div></div>
          </div>
        </header>

        {error && <div className="app-error pw-err">⚠ {error}</div>}

        <ResearchPanel
          run={run} onRun={runResearch} running={runningResearch} insights={insights}
          instruction={instruction} onInstructionChange={setInstruction} workshopId={workshopId}
        />

        <ArtifactsGrid docs={docs} artifacts={artifacts} onView={setViewerDocId} workshopId={workshopId} />

        <WorkflowPanel onRun={runWorkflow} running={runningWorkflow} result={workflowResult} />
      </div>

      {viewerDocId && (
        <DocumentViewer workshopId={workshopId} docId={viewerDocId} onClose={() => setViewerDocId(null)} />
      )}
    </div>
  );
}

function SourceArtifactsPanel({ docs, onAdd, onView }) {
  const ingestedCount = docs.filter((d) => d.status === 'ingested').length;
  return (
    <section className="pw-sources">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="upload" /></span>
          <div>
            <div className="pw-h3">Source Artifacts</div>
            <div className="pw-sub">{ingestedCount}/{docs.length} ingested · grounding the copilot</div>
          </div>
        </div>
        <button className="btn" onClick={onAdd}><Icon name="plus" />Add</button>
      </div>
      <div className="pw-dropzone" onClick={onAdd}>
        <Icon name="upload" />
        <div className="pw-dz-txt">Drop docs, PDFs, videos, links, transcripts</div>
        <div className="pw-dz-sub">SharePoint · Teams · Granola · GitHub · draw.io</div>
      </div>
      {docs.length === 0 ? (
        <div className="pw-empty">No sources yet for this phase.<br />Ingest client artifacts to ground the research.</div>
      ) : (
        <ul className="pw-source-list">
          {docs.map((d) => {
            const ft = fileType(d.name);
            return (
              <li key={d.doc_id} className="pw-source-item">
                <span className="pw-source-icon" style={{ background: ft.bg, color: ft.fg }}>
                  <Icon name={ft.icon} />
                </span>
                <div className="pw-source-main">
                  <div className="pw-source-name">{d.name}</div>
                  <div className="pw-source-meta">{ft.label} · {d.chars} chars</div>
                </div>
                <span className={`pw-pill pw-pill-${d.status}`}>{STATUS_LABEL[d.status] || d.status}</span>
                <button className="pw-view-btn" onClick={() => onView(d.doc_id)} title="View document">
                  <Icon name="search" />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function AskResearchAgent({ workshopId }) {
  const [open, setOpen] = useState(false);
  const [question, setQuestion] = useState('');
  const [asking, setAsking] = useState(false);
  const [reply, setReply] = useState('');
  const [err, setErr] = useState('');

  async function ask() {
    if (!question.trim()) return;
    setAsking(true); setErr(''); setReply('');
    try {
      const res = await apiPost('/api/agents/chat', {
        message: question, workshop_id: workshopId, context: { zone: 'Pre-Workshop' },
      });
      if (!res.ok) { setErr(res.error || 'could not get a reply'); return; }
      setReply(res.reply || '');
    } catch (e) {
      setErr(e.message || 'could not get a reply');
    } finally {
      setAsking(false);
    }
  }

  return (
    <div className="pw-ask-agent">
      <button className="pw-ask-btn" onClick={() => setOpen((o) => !o)}>
        <Icon name="sparkles" />Ask the research agent<span className="pw-ask-arrow">→</span>
      </button>
      {open && (
        <div className="pw-ask-panel">
          <textarea
            className="pw-instruction" rows={2}
            placeholder="Ask about the ingested documents or the research findings so far…"
            value={question} onChange={(e) => setQuestion(e.target.value)}
          />
          <button className="btn solid" onClick={ask} disabled={asking || !question.trim()}>
            {asking ? 'Asking…' : 'Ask'}
          </button>
          {err && <div className="app-error" style={{ marginTop: 8 }}>⚠ {err}</div>}
          {reply && <div className="pw-ask-reply">{reply}</div>}
        </div>
      )}
    </div>
  );
}

function ResearchPanel({ run, onRun, running, insights, instruction, onInstructionChange, workshopId }) {
  const steps = (run && run.steps) || [];
  const stepByKey = Object.fromEntries(steps.map((s) => [s.step, s]));
  const status = run ? run.status : null;

  return (
    <section className="pw-panel pw-research">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="globe" /></span>
          <div>
            <div className="pw-h3">Context-Grounded Web Research</div>
            <div className="pw-sub">Agentic research chained on the ingested client artifacts</div>
          </div>
        </div>
        {status === 'done' ? (
          <span className="pw-status pw-status-done"><Icon name="check-circle" /> Complete</span>
        ) : status === 'running' ? (
          <span className="pw-status pw-status-running">Running…</span>
        ) : null}
      </div>

      <label className="pw-instruction-lbl" htmlFor="pw-instruction">
        What should the research agent focus on? <span>(optional — leave blank to let it infer from the ingested documents)</span>
      </label>
      <textarea
        id="pw-instruction"
        className="pw-instruction"
        placeholder='e.g. "Focus on GMP-qualified labour constraints and how competitors handle cross-site skills flexing"'
        value={instruction}
        onChange={(e) => onInstructionChange(e.target.value)}
        rows={2}
      />

      <button className="btn solid pw-run-btn" onClick={onRun} disabled={running}>
        <Icon name="sparkles" />{running ? 'Researching…' : status ? 'Run again' : 'Run Deep Research'}
      </button>

      <div className="pw-research-grid">
        <div className="pw-insights">
          {insights.length === 0 ? (
            <div className="pw-empty">No research yet — run Deep Research to generate cited insights.</div>
          ) : (
            insights.map((ins, i) => (
              <div className="pw-insight-card" key={i}>
                <span className="pw-ic pw-ic-teal"><Icon name="sparkles" /></span>
                <div>
                  <div className="pw-insight-ttl">{ins.title}</div>
                  <div className="pw-insight-desc">{ins.description}</div>
                  <div className="pw-cites">
                    {(ins.source_refs || []).map((r, j) => (
                      r.url ? (
                        <a key={j} className={`pw-cite pw-cite-${r.type}`} href={r.url} target="_blank" rel="noopener noreferrer">
                          <Icon name="doc-text" />{r.label}
                        </a>
                      ) : (
                        <span key={j} className={`pw-cite pw-cite-${r.type}`}><Icon name="doc-text" />{r.label}</span>
                      )
                    ))}
                  </div>
                </div>
              </div>
            ))
          )}
          {(run && (run.diagram || (run.next_steps || []).length > 0)) && (
            <div className="pw-research-workflow">
              <div className="pw-h3" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <Icon name="flow" />Workflow — from this research
              </div>
              <div className="pw-sub" style={{ marginBottom: 10 }}>
                The instruction above asked for a workflow, so this run also produced one — no
                separate "Build workflow" step needed.
              </div>
              {run.diagram && (
                <button className="btn" onClick={() => downloadDrawio(run.diagram, 'research-workflow')}>
                  <Icon name="upload" />Open diagram (.drawio)
                </button>
              )}
              {(run.next_steps || []).length > 0 && (
                <ul className="pw-checklist" style={{ marginTop: 10 }}>
                  {run.next_steps.map((s, i) => (
                    <li key={i}>
                      <span className="pw-check-box" />
                      <div>
                        <div className="pw-check-step">{s.step}</div>
                        <div className="pw-check-why">{s.why}</div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <div className="pw-chain">
          <div className="pw-chain-ttl">Research Chain</div>
          {RESEARCH_STEP_ORDER.map((key, i) => {
            const s = stepByKey[key];
            const isCurrent = !s && status === 'running' && (i === 0 || stepByKey[RESEARCH_STEP_ORDER[i - 1]]);
            return (
              <div key={key} className={'pw-chain-step' + (s ? ' done' : isCurrent ? ' pending' : '')}>
                <span className="pw-chain-dot">{i + 1}</span>
                <div>
                  <div className="pw-chain-label">{s ? s.label : key}</div>
                  <div className="pw-chain-detail">{s ? s.detail : isCurrent ? 'in progress…' : '—'}</div>
                </div>
              </div>
            );
          })}
          <AskResearchAgent workshopId={workshopId} />
        </div>
      </div>
    </section>
  );
}

function ArtifactsGrid({ docs, artifacts, onView, workshopId }) {
  const isEmpty = docs.length === 0 && artifacts.length === 0;
  return (
    <section className="pw-artifacts">
      <div className="pw-h3 pw-artifacts-ttl"><Icon name="list" />Pre-Workshop Artifacts</div>
      {isEmpty ? (
        <div className="pw-empty">Nothing here yet — ingest a document or run an agent above.</div>
      ) : (
        <div className="pw-artifact-grid">
          {docs.map((d) => {
            const ft = fileType(d.name);
            return (
              <div className="pw-artifact-card" key={`doc-${d.doc_id}`}>
                <div className="pw-artifact-top">
                  <span className="pw-ic" style={{ background: ft.bg, color: ft.fg }}><Icon name={ft.icon} /></span>
                  <span className={`pw-pill pw-pill-${d.status}`}>{STATUS_LABEL[d.status] || d.status}</span>
                </div>
                <div className="pw-artifact-cat">SOURCE · {ft.label}</div>
                <div className="pw-artifact-name">{d.name}</div>
                <div className="pw-artifact-desc">{d.chars} characters extracted</div>
                <div className="pw-artifact-foot">
                  <span>{d.uploaded_by || 'you'} · {timeAgo(d.uploaded_at)}</span>
                  <button className="pw-view-btn" onClick={() => onView(d.doc_id)} title="View document"><Icon name="search" />View</button>
                </div>
              </div>
            );
          })}
          {artifacts.map((a) => (
            <div className="pw-artifact-card" key={`gen-${a.doc_id}`}>
              <div className="pw-artifact-top">
                <span className="pw-ic pw-ic-accent"><Icon name={a.agent_id === 'workflow' ? 'flow' : 'search'} /></span>
                <span className={`pw-pill pw-pill-${a.status}`}>{a.status === 'final' ? 'Final' : a.status === 'in_review' ? 'In review' : 'Draft'}</span>
              </div>
              <div className="pw-artifact-cat">{(a.category || a.agent_id || '').toUpperCase()}</div>
              <div className="pw-artifact-name">{a.name}</div>
              {a.description && <div className="pw-artifact-desc">{a.description}</div>}
              <div className="pw-artifact-tags">
                {(a.tags || []).map((t) => <span key={t} className="pw-tag">{t}</span>)}
              </div>
              <div className="pw-artifact-foot">
                <span>{a.author || 'BA Copilot'} · {timeAgo(a.created_at)}</span>
                <span className="pw-progress">
                  <span className="pw-progress-track"><span className="pw-progress-fill" style={{ width: `${a.completion_pct || 0}%` }} /></span>
                  {a.completion_pct || 0}%
                </span>
              </div>
              <div className="pw-artifact-actions">
                <button className="pw-view-btn" onClick={() => onView(a.doc_id)} title="View document"><Icon name="search" />View</button>
                <a className="pw-view-btn" href={`/api/agents/document/${a.doc_id}/word?workshop_id=${workshopId}`}
                  download title="Download as Word (.docx)">
                  <Icon name="upload" />Word
                </a>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function WorkflowPanel({ onRun, running, result }) {
  return (
    <section className="pw-panel pw-workflow">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-indigo"><Icon name="flow" /></span>
          <div>
            <div className="pw-h3">Build Workflow</div>
            <div className="pw-sub">Grounded on ingested documents + the latest research findings</div>
          </div>
        </div>
      </div>
      <button className="btn solid pw-run-btn" onClick={onRun} disabled={running}>
        <Icon name="flow" />{running ? 'Building…' : 'Build workflow'}
      </button>
      {result && (
        <div className="pw-workflow-result">
          <div className="pw-h3">{result.title}</div>
          {result.diagram && (
            <button className="btn" onClick={() => downloadDrawio(result.diagram, result.title)}>
              <Icon name="upload" />Open diagram (.drawio)
            </button>
          )}
          {(result.next_steps || []).length > 0 && (
            <ul className="pw-checklist">
              {result.next_steps.map((s, i) => (
                <li key={i}>
                  <span className="pw-check-box" />
                  <div>
                    <div className="pw-check-step">{s.step}</div>
                    <div className="pw-check-why">{s.why}</div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
