'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../lib/api';
import { Icon } from '../../lib/icons';
import { STATUS_LABEL, downloadDrawio, fileType, timeAgo } from '../artifactMeta';
import ArtifactExplorer from '../ArtifactExplorer';
import AnalysisModal from './AnalysisModal';
import DocumentViewer from './DocumentViewer';
import DrawioViewer from './DrawioViewer';
import '../../shared.css';

const RESEARCH_STEP_ORDER = ['ingest', 'extract', 'queries', 'search', 'synthesize'];
// The 'analyze' pipeline's steps (see agent_catalog._ANALYSIS_STEPS).
const ANALYSIS_STEP_ORDER = ['inventory', 'perdoc', 'synth', 'readiness'];
const ANALYSIS_STEP_LABELS = {
  inventory: 'Inventory documents', perdoc: 'Analyze each document',
  synth: 'Synthesize analysis', readiness: 'Score readiness',
};

// One consistent card per sidebar agent: icon + name, an eye button that
// reveals a plain-language explainer (what it does / reads / produces —
// for anyone who doesn't know the agent yet), then the agent's own
// controls as children.
export function AgentCard({ icon, title, info, children }) {
  const [showInfo, setShowInfo] = useState(false);
  return (
    <div className="pw-agent-card">
      <div className="pw-agent-card-head">
        <span className="pw-agent-card-ic"><Icon name={icon} /></span>
        <span className="pw-agent-card-ttl">{title}</span>
        <button className={'pw-info-btn' + (showInfo ? ' on' : '')}
          onClick={() => setShowInfo((v) => !v)}
          title={showInfo ? 'Hide explanation' : 'What does this agent do?'}>
          <Icon name="eye" />
        </button>
      </div>
      {showInfo && <div className="pw-agent-info">{info}</div>}
      {children}
    </div>
  );
}


export default function PreWorkshopDashboard({ user, workshopId }) {
  const [docs, setDocs] = useState([]);
  const [artifacts, setArtifacts] = useState([]);
  const [run, setRun] = useState(null);
  const [runningResearch, setRunningResearch] = useState(false);
  const [runningWorkflow, setRunningWorkflow] = useState(false);
  const [workflowResult, setWorkflowResult] = useState(null);
  const [runningSummary, setRunningSummary] = useState(false);
  const [runningArtifact, setRunningArtifact] = useState(false);
  const [runningAnalysis, setRunningAnalysis] = useState(false);
  // One optional instruction for the sidebar agents — "what specifically
  // you want" — sent as `extra` to whichever agent gets run (the backend
  // already threads it into every agent's prompt as EXTRA INPUT).
  const [agentPrompt, setAgentPrompt] = useState('');
  // The unified Artifact Analyst card's two toggles: corpus scope
  // (sources = client uploads only; all = uploads + generated docs) and
  // mode (answer = cited Q&A via artifact_analyst; assess = readiness
  // pipeline via analyze). One card, two pipelines underneath.
  const [analystScope, setAnalystScope] = useState('sources');
  const [analystMode, setAnalystMode] = useState('answer');
  const [analysisChain, setAnalysisChain] = useState(null);   // live steps while running
  const [analysisModal, setAnalysisModal] = useState(null);   // {name, analysis, docId}
  const [instruction, setInstruction] = useState('');
  const [error, setError] = useState('');
  const [viewerDocId, setViewerDocId] = useState(null);
  const [viewerDiagram, setViewerDiagram] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);   // {docId, name} while the dialog is open
  const [deleting, setDeleting] = useState(false);
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

  // `overrideText` lets the analysis scorecard's "Research this" hand a
  // gap topic straight into the normal deep-research flow. Guarded by a
  // typeof check because onClick handlers receive the event object.
  async function runResearch(overrideText) {
    const text = (typeof overrideText === 'string' ? overrideText : instruction).trim();
    if (typeof overrideText === 'string') setInstruction(overrideText);
    setRunningResearch(true);
    setError('');
    setRun({ status: 'running', steps: [], insights: [], confidence: null });
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'deepresearch', workshop_id: workshopId, context: { zone: 'Pre-Workshop' },
        extra: text || undefined,
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
    setWorkflowResult(null);
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'workflow', workshop_id: workshopId, context: { zone: 'Pre-Workshop' },
        extra: agentPrompt.trim() || undefined,
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

  async function runSummarize() {
    setRunningSummary(true);
    setError('');
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'summarize_docs', workshop_id: workshopId, context: { zone: 'Pre-Workshop' },
        extra: agentPrompt.trim() || undefined,
      });
      if (!res.ok) { setError(res.error || 'summary failed'); return; }
      loadArtifacts();
      if (res.draft && res.draft.node && res.draft.node.docId) setViewerDocId(res.draft.node.docId);
    } catch (err) {
      setError(err.message || 'summary failed');
    } finally {
      setRunningSummary(false);
    }
  }

  async function runArtifactAnalyst() {
    setRunningArtifact(true);
    setError('');
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'artifact_analyst', workshop_id: workshopId, context: { zone: 'Pre-Workshop' },
        extra: agentPrompt.trim() || undefined,
        options: { scope: analystScope },
      });
      if (!res.ok) { setError(res.error || 'artifact analysis failed'); return; }
      loadArtifacts();
      if (res.draft && res.draft.node && res.draft.node.docId) setViewerDocId(res.draft.node.docId);
    } catch (err) {
      setError(err.message || 'artifact analysis failed');
    } finally {
      setRunningArtifact(false);
    }
  }

  async function runAnalyze() {
    setRunningAnalysis(true);
    setError('');
    setAnalysisChain([]);
    // Live progress: the analyze pipeline logs its steps into the same
    // ledger the Research Chain uses (agent_id-separated) — poll while
    // the run is in flight so the sidebar shows real stages, not a spinner.
    const poll = setInterval(async () => {
      try {
        const data = await apiGet(`/api/agents/analysis-progress?workshop_id=${workshopId}`);
        if (data && data.ok && data.run) setAnalysisChain(data.run.steps || []);
      } catch { /* transient */ }
    }, 1500);
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: 'analyze', workshop_id: workshopId, context: { zone: 'Pre-Workshop' },
        extra: agentPrompt.trim() || undefined,
        options: { scope: analystScope },
      });
      if (!res.ok) { setError(res.error || 'analysis failed'); return; }
      loadArtifacts();
      setAnalysisModal({
        name: res.draft.title,
        analysis: res.draft.analysis || { gaps: [], readiness: [], research_topics: [] },
        docId: res.draft.node && res.draft.node.docId,
      });
    } catch (err) {
      setError(err.message || 'analysis failed');
    } finally {
      clearInterval(poll);
      setAnalysisChain(null);
      setRunningAnalysis(false);
    }
  }

  // Two-step delete: the trash button only opens the confirmation dialog
  // (a real modal, not window.confirm); performDelete does the work.
  function deleteArtifact(docId, name) {
    setConfirmDelete({ docId, name });
  }

  async function performDelete() {
    if (!confirmDelete) return;
    const { docId } = confirmDelete;
    setDeleting(true);
    setError('');
    try {
      const res = await apiDelete(`/api/agents/document/${docId}?workshop_id=${workshopId}`);
      if (!res.ok) { setError(res.error || 'delete failed'); return; }
      if (viewerDocId === docId) setViewerDocId(null);
      loadDocs();
      loadArtifacts();
    } catch (err) {
      setError(err.message || 'delete failed');
    } finally {
      setDeleting(false);
      setConfirmDelete(null);
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
      <ArtifactExplorer workshopId={workshopId} docs={docs} artifacts={artifacts}
        activePhase="Pre-Workshop"
        onAdd={() => fileInputRef.current?.click()}
        onView={setViewerDocId}
        onOpenDiagram={setViewerDiagram}
        onOpenAnalysis={setAnalysisModal}
        onDelete={deleteArtifact} />
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
          onViewDiagram={setViewerDiagram}
          onRunWorkflow={runWorkflow} runningWorkflow={runningWorkflow} workflowResult={workflowResult}
          onRunSummarize={runSummarize} runningSummary={runningSummary}
          onRunAnalyze={runAnalyze} runningAnalysis={runningAnalysis} analysisChain={analysisChain}
          onRunArtifact={runArtifactAnalyst} runningArtifact={runningArtifact}
          agentPrompt={agentPrompt} onAgentPromptChange={setAgentPrompt}
          analystScope={analystScope} onScopeChange={setAnalystScope}
          analystMode={analystMode} onModeChange={setAnalystMode}
        />

        <ArtifactsGrid docs={docs} artifacts={artifacts} onView={setViewerDocId} workshopId={workshopId}
          onViewDiagram={setViewerDiagram} onViewAnalysis={setAnalysisModal} onDelete={deleteArtifact}
          zone="Pre-Workshop" />
      </div>

      {viewerDocId && (
        <DocumentViewer workshopId={workshopId} docId={viewerDocId} onClose={() => setViewerDocId(null)} />
      )}
      {viewerDiagram && (
        <DrawioViewer xml={viewerDiagram.xml} title={viewerDiagram.title} onClose={() => setViewerDiagram(null)} />
      )}
      {analysisModal && (
        <AnalysisModal
          name={analysisModal.name}
          analysis={analysisModal.analysis}
          onClose={() => setAnalysisModal(null)}
          onResearch={(topic) => { setAnalysisModal(null); runResearch(topic); }}
          onOpenDoc={analysisModal.docId ? () => { setAnalysisModal(null); setViewerDocId(analysisModal.docId); } : null}
        />
      )}
      {confirmDelete && (
        <ConfirmDeleteModal
          name={confirmDelete.name}
          busy={deleting}
          onCancel={() => setConfirmDelete(null)}
          onConfirm={performDelete}
        />
      )}
    </div>
  );
}

// Delete confirmation — a real dialog in the app's own visual language
// instead of window.confirm: names the document, states exactly what
// deletion means, and makes Cancel the easy path.
export function ConfirmDeleteModal({ name, busy, onCancel, onConfirm }) {
  return (
    <div className="pw-modal-backdrop pw-confirm-backdrop" onClick={busy ? undefined : onCancel}>
      <div className="pw-confirm" onClick={(e) => e.stopPropagation()}>
        <div className="pw-confirm-ic"><Icon name="trash" /></div>
        <div className="pw-confirm-ttl">Delete “{name}”?</div>
        <div className="pw-confirm-txt">
          It will be removed from this workshop, from search, and from what grounds
          Copilot and the agents. This can't be undone.
        </div>
        <div className="pw-confirm-actions">
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn pw-btn-danger" onClick={onConfirm} disabled={busy}>
            <Icon name="trash" />{busy ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </div>
    </div>
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

function ResearchPanel({
  run, onRun, running, insights, instruction, onInstructionChange, workshopId, onViewDiagram,
  onRunWorkflow, runningWorkflow, workflowResult, onRunSummarize, runningSummary,
  onRunAnalyze, runningAnalysis, analysisChain,
  onRunArtifact, runningArtifact, agentPrompt, onAgentPromptChange,
  analystScope, onScopeChange, analystMode, onModeChange,
}) {
  const steps = (run && run.steps) || [];
  const stepByKey = Object.fromEntries(steps.map((s) => [s.step, s]));
  const status = run ? run.status : null;

  return (
    <section className="pw-panel pw-research">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="globe" /></span>
          <div>
            <div className="pw-h3">Grounded Web Researcher</div>
            <div className="pw-sub">Open web, anchored to your artifacts — market, competitor, regulatory and benchmark signal tied to this engagement, never generic</div>
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
                <div className="pw-diagram-actions">
                  <button className="btn solid" onClick={() => onViewDiagram({ xml: run.diagram.xml, title: 'Workflow — from this research' })}>
                    <Icon name="flow" />View diagram
                  </button>
                  <button className="btn" onClick={() => downloadDrawio(run.diagram, 'research-workflow')}>
                    <Icon name="upload" />Download .drawio
                  </button>
                </div>
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

          <div className="pw-chain-agents">
            <div className="pw-chain-agents-ttl">More agents</div>

            <label className="pw-agent-prompt-lbl" htmlFor="pw-agent-prompt">
              Instruction <span>(optional — applies to whichever agent you run)</span>
            </label>
            <textarea
              id="pw-agent-prompt"
              className="pw-instruction pw-agent-prompt" rows={2}
              placeholder='e.g. "compare the two audit findings"'
              value={agentPrompt}
              onChange={(e) => onAgentPromptChange(e.target.value)}
            />

            <AgentCard icon="doc-text" title="Artifact Analyst" info={(
              <>
                <div className="pw-agent-info-row"><b>What it does</b>Answers questions about — or assesses the readiness of — this workshop's documents. Type a question in the instruction box above, or run it blank for a full digest.</div>
                <div className="pw-agent-info-row"><b>Scope</b>“Sources only” reads client uploads exclusively. “All artifacts” also reads AI-generated documents, always cited with a “generated:” prefix so provenance stays visible.</div>
                <div className="pw-agent-info-row"><b>Mode</b>“Answer” gives a cited reply and refuses anything the documents don't cover. “Assess” finds gaps, scores readiness, and routes each gap to an action.</div>
                <div className="pw-agent-info-row"><b>Produces</b>A saved, Word-exportable document — plus a scorecard in Assess mode.</div>
              </>
            )}>
              <div className="pw-seg" role="group" aria-label="Corpus scope">
                <button className={'pw-seg-btn' + (analystScope === 'sources' ? ' on' : '')}
                  onClick={() => onScopeChange('sources')} title="Client uploads only — nothing we generated">
                  Sources only
                </button>
                <button className={'pw-seg-btn' + (analystScope === 'all' ? ' on' : '')}
                  onClick={() => onScopeChange('all')} title="Uploads + every generated artifact in this workshop">
                  All artifacts
                </button>
              </div>
              <div className="pw-seg" role="group" aria-label="Mode">
                <button className={'pw-seg-btn' + (analystMode === 'answer' ? ' on' : '')}
                  onClick={() => onModeChange('answer')} title="Q&A / digest — cited, refuses beyond the corpus">
                  Answer
                </button>
                <button className={'pw-seg-btn' + (analystMode === 'assess' ? ' on' : '')}
                  onClick={() => onModeChange('assess')} title="Readiness assessment — gaps, scorecard, routed actions">
                  Assess
                </button>
              </div>
              <button className="btn solid pw-chain-agent-btn"
                onClick={analystMode === 'answer' ? onRunArtifact : onRunAnalyze}
                disabled={analystMode === 'answer' ? runningArtifact : runningAnalysis}>
                <Icon name={analystMode === 'answer' ? 'doc-text' : 'target'} />
                {analystMode === 'answer'
                  ? (runningArtifact ? 'Answering…' : 'Run Analyst')
                  : (runningAnalysis ? 'Assessing…' : 'Run Assessment')}
              </button>
              <div className="pw-chain-agent-sub">
                {analystMode === 'answer'
                  ? (analystScope === 'sources'
                    ? 'Cited answers, strictly from the uploaded client documents.'
                    : 'Cited answers from uploads + generated artifacts (provenance-marked).')
                  : (analystScope === 'sources'
                    ? 'Gaps, scorecard and routed actions — client corpus alone.'
                    : 'Gaps, scorecard and routed actions — prior research included as labeled evidence.')}
              </div>
              {runningAnalysis && analysisChain && (
                <ul className="cop-chain pw-mini-chain">
                  {ANALYSIS_STEP_ORDER.map((key, si) => {
                    const s = analysisChain.find((x) => x.step === key);
                    const isCurrent = !s && (si === 0 || analysisChain.find((x) => x.step === ANALYSIS_STEP_ORDER[si - 1]));
                    return (
                      <li key={key} className={s ? 'done' : isCurrent ? 'pending' : ''}>
                        <span className="cop-chain-dot">{s ? <Icon name="check" /> : si + 1}</span>
                        <div>
                          <div className="cop-chain-label">{ANALYSIS_STEP_LABELS[key]}</div>
                          {s && s.detail && <div className="cop-chain-detail">{s.detail}</div>}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </AgentCard>

            <AgentCard icon="flow" title="Build Workflow" info={(
              <>
                <div className="pw-agent-info-row"><b>What it does</b>Proposes the process worth automating or streamlining, drawn from everything this workshop knows.</div>
                <div className="pw-agent-info-row"><b>Reads</b>Every ingested document plus every research document produced so far.</div>
                <div className="pw-agent-info-row"><b>Produces</b>A swimlane diagram (view/edit in draw.io) and an ordered next-steps checklist, saved to Artifacts.</div>
              </>
            )}>
              <button className="btn solid pw-chain-agent-btn" onClick={onRunWorkflow} disabled={runningWorkflow}>
                <Icon name="flow" />{runningWorkflow ? 'Building…' : 'Build workflow'}
              </button>
              {workflowResult && (
                <div className="pw-chain-agent-result">
                  <div className="pw-chain-agent-result-ttl"><Icon name="check-circle" />{workflowResult.title}</div>
                  {workflowResult.diagram && (
                    <button className="pw-view-btn" onClick={() => onViewDiagram({ xml: workflowResult.diagram.xml, title: workflowResult.title })}>
                      <Icon name="flow" />View diagram
                    </button>
                  )}
                  {(workflowResult.next_steps || []).length > 0 && (
                    <ul className="pw-checklist pw-checklist-compact">
                      {workflowResult.next_steps.map((s, i) => (
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
            </AgentCard>

            <AgentCard icon="list" title="Summarize Documents" info={(
              <>
                <div className="pw-agent-info-row"><b>What it does</b>Condenses everything into one consolidated summary — key points across all sources, with what's still unknown.</div>
                <div className="pw-agent-info-row"><b>Reads</b>Every ingested document plus every research document produced so far.</div>
                <div className="pw-agent-info-row"><b>Produces</b>A saved, Word-exportable summary in Artifacts below.</div>
              </>
            )}>
              <button className="btn solid pw-chain-agent-btn" onClick={onRunSummarize} disabled={runningSummary}>
                <Icon name="list" />{runningSummary ? 'Summarizing…' : 'Summarize documents'}
              </button>
            </AgentCard>
          </div>
        </div>
      </div>
    </section>
  );
}

function artifactBucket(a) {
  return a.category || a.agent_id || 'Other';
}

export function ArtifactsGrid({ docs, artifacts, onView, workshopId, onViewDiagram, onViewAnalysis,
                                onDelete, onViewCapmap, title = 'Pre-Workshop Artifacts',
                                zone, showSources = true }) {
  const [filter, setFilter] = useState('all');
  // `zone` scopes the grid to one engagement phase's generated artifacts
  // (list_docs now carries the producing agent's zone). Legacy rows with
  // no zone stay visible on the Pre-Workshop grid only — that's where
  // everything lived before phases had their own grids.
  if (zone) {
    artifacts = artifacts.filter((a) => a.zone === zone || (!a.zone && zone === 'Pre-Workshop'));
  }
  if (!showSources) docs = [];
  const isEmpty = docs.length === 0 && artifacts.length === 0;

  async function viewDiagram(a) {
    try {
      const data = await apiGet(`/api/agents/document/${a.doc_id}/diagram?workshop_id=${workshopId}`);
      if (data && data.ok) onViewDiagram({ xml: data.xml, title: a.name });
    } catch { /* no diagram, or transient — the button just does nothing */ }
  }

  async function viewAnalysis(a) {
    try {
      const data = await apiGet(`/api/agents/document/${a.doc_id}/analysis?workshop_id=${workshopId}`);
      if (data && data.ok) {
        onViewAnalysis({
          name: a.name,
          analysis: { gaps: data.gaps || [], readiness: data.readiness || [], research_topics: data.research_topics || [] },
          docId: a.doc_id,
        });
      }
    } catch { /* no analysis, or transient */ }
  }

  const bucketCounts = {};
  artifacts.forEach((a) => { const b = artifactBucket(a); bucketCounts[b] = (bucketCounts[b] || 0) + 1; });
  const buckets = Object.keys(bucketCounts).sort();
  const filters = [
    { key: 'all', label: 'All', count: docs.length + artifacts.length },
    ...(showSources ? [{ key: 'source', label: 'Source', count: docs.length }] : []),
    ...buckets.map((b) => ({ key: b, label: b, count: bucketCounts[b] })),
  ];

  const visibleDocs = filter === 'all' || filter === 'source' ? docs : [];
  const visibleArtifacts = filter === 'all' ? artifacts
    : filter === 'source' ? []
    : artifacts.filter((a) => artifactBucket(a) === filter);
  const isFilteredEmpty = visibleDocs.length === 0 && visibleArtifacts.length === 0;

  return (
    <section className="pw-artifacts">
      <div className="pw-artifacts-head">
        <div className="pw-h3 pw-artifacts-ttl"><Icon name="list" />{title}</div>
        {!isEmpty && (
          <div className="pw-artifact-filters">
            {filters.map((f) => (
              <button key={f.key} className={'pw-filter-chip' + (filter === f.key ? ' on' : '')}
                onClick={() => setFilter(f.key)}>
                {f.label}<span className="pw-filter-count">{f.count}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      {isEmpty ? (
        <div className="pw-empty">Nothing here yet — ingest a document or run an agent above.</div>
      ) : isFilteredEmpty ? (
        <div className="pw-empty">Nothing matches this filter.</div>
      ) : (
        <div className="pw-artifact-grid">
          {visibleDocs.map((d) => {
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
                </div>
                <div className="pw-artifact-actions">
                  <button className="pw-view-btn" onClick={() => onView(d.doc_id)} title="View document"><Icon name="search" />View</button>
                  <button className="pw-view-btn pw-del-btn" onClick={() => onDelete(d.doc_id, d.name)} title="Delete document"><Icon name="trash" /></button>
                </div>
              </div>
            );
          })}
          {visibleArtifacts.map((a) => (
            <div className="pw-artifact-card" key={`gen-${a.doc_id}`}>
              <div className="pw-artifact-top">
                <span className="pw-ic pw-ic-accent">
                  <Icon name={(a.agent_id === 'workflow' || a.agent_id === 'drawflow') ? 'flow'
                    : (a.agent_id === 'summarize_docs' || a.agent_id === 'artifact_analyst' || a.agent_id === 'brd') ? 'doc-text'
                    : (a.agent_id === 'analyze' || a.agent_id === 'capmap') ? 'target' : 'search'} />
                </span>
                <span className={`pw-pill pw-pill-${a.status}`}>{a.status === 'final' ? 'Final' : a.status === 'in_review' ? 'In review' : 'Draft'}</span>
              </div>
              <div className="pw-artifact-cat">{(a.category || a.agent_id || '').toUpperCase()}</div>
              <div className="pw-artifact-name">{a.name}</div>
              {a.description && <div className="pw-artifact-desc">{a.description}</div>}
              <div className="pw-artifact-tags">
                {(a.tags || []).map((t) => <span key={t} className="pw-tag">{t}</span>)}
                {(a.inputs || []).length > 0 && (
                  <span className="pw-tag pw-tag-inputs"
                    title={'Built from: ' + a.inputs.map((i) => i.name).join(', ')}>
                    from {a.inputs.length} input{a.inputs.length === 1 ? '' : 's'}
                  </span>
                )}
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
                {a.has_diagram && (
                  <button className="pw-view-btn" onClick={() => viewDiagram(a)} title="View workflow diagram">
                    <Icon name="flow" />Diagram
                  </button>
                )}
                {a.has_analysis && (
                  <button className="pw-view-btn" onClick={() => viewAnalysis(a)} title="Readiness scorecard & routed gaps">
                    <Icon name="target" />Scorecard
                  </button>
                )}
                {a.has_capmap && onViewCapmap && (
                  <button className="pw-view-btn" onClick={() => onViewCapmap(a)} title="Open the capability heat map">
                    <Icon name="target" />Map
                  </button>
                )}
                <a className="pw-view-btn" href={`/api/agents/document/${a.doc_id}/word?workshop_id=${workshopId}`}
                  download title="Download as Word (.docx)">
                  <Icon name="upload" />Word
                </a>
                <button className="pw-view-btn pw-del-btn" onClick={() => onDelete(a.doc_id, a.name)} title="Delete artifact">
                  <Icon name="trash" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

