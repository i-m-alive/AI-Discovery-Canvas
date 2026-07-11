'use client';

/*
 * OrbitzApp — the workshop workspace rebuilt to match the NaviBA Orbitz
 * reference UI (jolly-rock-0ff51a803.7.azurestaticapps.net).
 *
 * WIRED to the real Flask backend:
 *   - board persistence      GET/PUT /api/canvas/board   (namespaced under board.orbitz)
 *   - source uploads         POST /api/agents/upload, GET /api/agents/prepare-docs,
 *                            GET/DELETE /api/agents/document/:id
 *   - BA Copilot chat        POST /api/agents/chat (reply OR agent dispatch)
 *   - agent drafts           POST /api/agents/run  (approve → Workshop Artifacts)
 *   - export                 POST /api/export/handoff (docx/md download)
 *   - Teams / Granola status GET /api/integrations/{teams,granola}/status
 *   - live capture           browser SpeechRecognition (same engine the old canvas used)
 *   - draw.io editor         embed.diagrams.net (JSON protocol), XML stored on the board
 *
 * NOT wired (kept visible to match the reference; hover shows "Not added"):
 * auto-extraction of requirements from the transcript, capability-map data,
 * live "Capabilities" counter, artifact completion %, GitHub/whiteboard
 * source connectors, Web+Docs grounding toggle, flow-viewer zoom.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiGet, apiPost, apiDelete } from '../lib/api';
import './orbitz.css';

const PHASES = [
  { key: 'pre', n: 1, title: 'Pre-Workshop', sub: 'Ingest · Internalize · Research' },
  { key: 'during', n: 2, title: 'During Workshop', sub: 'Capture · Synthesize · Generate' },
  { key: 'post', n: 3, title: 'Post-Workshop', sub: 'Backlog · Opportunities · MoM' },
  { key: 'proposal', n: 4, title: 'Proposal & Planning', sub: 'SOW · ROI · Risk · Team' },
];

// Sample rows shown only while a board has no real data yet — every sample
// element carries data-na so hovering explains it isn't wired.
const SAMPLE_REQS = [
  { code: 'REQ-01', category: 'Scheduling', priority: 'Must', text: 'The system shall generate certification-aware shift rosters that only assign qualified operators to regulated lines.', source: 'sample — auto-extraction not added' },
  { code: 'REQ-02', category: 'Scheduling', priority: 'Must', text: 'The system shall re-optimize deployment in real time when absence or changeover delay is detected.', source: 'sample — auto-extraction not added' },
  { code: 'REQ-03', category: 'Skills', priority: 'Must', text: 'The system shall maintain a unified competency register consolidating site-level registers into the group standard.', source: 'sample — auto-extraction not added' },
  { code: 'REQ-04', category: 'Analytics', priority: 'Should', text: 'The system shall report schedule adherence and overtime by line, shift and changeover event.', source: 'sample — auto-extraction not added' },
];

const CAP_MAP = [
  { domain: 'Demand & Planning', caps: [['Production Demand Forecasting', 'h', 2], ['Capacity Modelling', 'm', 3], ['Shift Pattern Design', 'm', 3]] },
  { domain: 'Workforce & Skills', caps: [['Competency / GMP Register', 'h', 2], ['Cross-site Skills Flexing', 'h', 1], ['Training & Qualification', 'l', 4]] },
  { domain: 'Scheduling & Deployment', caps: [['Roster Generation', 'h', 2], ['Real-time Reallocation', 'h', 1], ['Absence & Cover', 'm', 3]] },
  { domain: 'Performance & Compliance', caps: [['Schedule Adherence Analytics', 'm', 3], ['Overtime & Cost Control', 'm', 2], ['GMP Line Clearance', 'l', 4]] },
];

const SUGGESTED = [
  'Summarize the key risks discussed so far',
  'Draft 3 discovery questions for the next session',
  'What requirements are still ambiguous?',
];

const AGENT_QUICK = [
  ['drawflow', 'Draw process flow'], ['stories', 'Generate user stories'], ['bdd', 'Acceptance criteria'],
  ['mom', 'Minutes of Meeting'], ['sow', 'Draft SOW'], ['roi', 'Calculate ROI'],
];

let uid = 0;
const nextId = () => 'm' + (++uid) + '-' + Math.random().toString(36).slice(2, 7);

export default function OrbitzApp({ user, workshopId, projectId, workshopName }) {
  const [theme, setTheme] = useState('dark');
  const [phase, setPhase] = useState(1);
  const [projectName, setProjectName] = useState('');
  const [docs, setDocs] = useState([]);
  const [teams, setTeams] = useState(null);
  const [granola, setGranola] = useState(null);
  const [requirements, setRequirements] = useState([]);
  const [artifacts, setArtifacts] = useState([]);
  const [transcript, setTranscript] = useState([]);
  const [recording, setRecording] = useState(false);
  const [drawioXml, setDrawioXml] = useState('');
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [reqDraft, setReqDraft] = useState('');
  const [cmdk, setCmdk] = useState(false);
  const [cmdq, setCmdq] = useState('');
  const [dioOpen, setDioOpen] = useState(false);
  const [copilotMin, setCopilotMin] = useState(false);
  const [toast, setToast] = useState('');
  const [dragOver, setDragOver] = useState(false);

  const boardRef = useRef(null);        // full board JSON — non-orbitz keys preserved on save
  const loadedRef = useRef(false);
  const saveTimer = useRef(null);
  const docTexts = useRef({});          // doc_id -> extracted text (grounding)
  const recogRef = useRef(null);
  const fileInput = useRef(null);
  const threadRef = useRef(null);
  const dioFrame = useRef(null);
  const toastTimer = useRef(null);

  const showToast = useCallback((t) => {
    setToast(t);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(''), 2600);
  }, []);

  /* ── theme ── */
  useEffect(() => {
    try { const t = localStorage.getItem('orbitz-theme'); if (t === 'light' || t === 'dark') setTheme(t); } catch {}
  }, []);
  const flipTheme = () => {
    const t = theme === 'dark' ? 'light' : 'dark';
    setTheme(t);
    try { localStorage.setItem('orbitz-theme', t); } catch {}
  };

  /* ── initial load: board + docs + integrations + project name ── */
  useEffect(() => {
    let dead = false;
    (async () => {
      try {
        const b = await apiGet(`/api/canvas/board?workshop_id=${workshopId}`);
        if (dead) return;
        const board = (b && b.ok && b.board) || {};
        boardRef.current = board;
        const oz = board.orbitz || {};
        if (typeof oz.phase === 'number') setPhase(oz.phase);
        if (Array.isArray(oz.requirements)) setRequirements(oz.requirements);
        if (Array.isArray(oz.artifacts)) setArtifacts(oz.artifacts);
        if (Array.isArray(oz.transcript)) setTranscript(oz.transcript);
        if (typeof oz.drawioXml === 'string') setDrawioXml(oz.drawioXml);
      } catch { /* board persistence degrades gracefully (no Postgres) */ }
      loadedRef.current = true;
    })();
    (async () => {
      try { const r = await apiGet(`/api/agents/prepare-docs?workshop_id=${workshopId}`); if (!dead && r && r.ok) hydrateDocs(r.docs || []); } catch {}
    })();
    (async () => { try { const r = await apiGet('/api/integrations/teams/status'); if (!dead) setTeams(r); } catch {} })();
    (async () => { try { const r = await apiGet('/api/integrations/granola/status'); if (!dead) setGranola(r); } catch {} })();
    (async () => {
      if (!projectId) return;
      try { const r = await apiGet(`/api/projects/${projectId}`); if (!dead && r && r.ok) setProjectName(r.project.name); } catch {}
    })();
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workshopId, projectId]);

  function hydrateDocs(list) {
    setDocs(list);
    // Pull each doc's extracted text so chat/agents are grounded on it
    // (mirrors what the old canvas kept in memory after upload).
    list.slice(0, 8).forEach(async (d) => {
      if (docTexts.current[d.doc_id]) return;
      try {
        const r = await apiGet(`/api/agents/document/${d.doc_id}?workshop_id=${workshopId}`);
        if (r && r.ok && r.text) docTexts.current[d.doc_id] = String(r.text).slice(0, 20000);
      } catch {}
    });
  }

  /* ── debounced board save (namespaced; other board keys preserved) ── */
  useEffect(() => {
    if (!loadedRef.current) return;
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(async () => {
      const board = { ...(boardRef.current || {}), orbitz: { phase, requirements, artifacts, transcript: transcript.slice(-200), drawioXml } };
      boardRef.current = board;
      try {
        await fetch(`/api/canvas/board?workshop_id=${workshopId}`, {
          method: 'PUT', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(board),
        });
      } catch {}
    }, 900);
    return () => clearTimeout(saveTimer.current);
  }, [phase, requirements, artifacts, transcript, drawioXml, workshopId]);

  /* ── grounding context sent to chat/agents (same shape the backend expects) ── */
  const buildContext = useCallback(() => {
    let board = '';
    if (requirements.length) board += 'Requirements:\n' + requirements.map((r) => `- [${r.priority}] ${r.text}`.slice(0, 160)).join('\n') + '\n';
    if (artifacts.length) board += 'Approved artifacts:\n' + artifacts.map((a) => `- ${a.label} (${a.folder})`).join('\n') + '\n';
    return {
      zone: PHASES[phase - 1]?.title || 'During Workshop',
      scope: PHASES[phase - 1]?.title || 'During Workshop',
      board: board.slice(0, 6000),
      transcript: transcript.slice(-24),
      files: docs.map((d) => ({ name: d.name, text: docTexts.current[d.doc_id] || '' })).filter((f) => f.text),
    };
  }, [phase, requirements, artifacts, transcript, docs]);

  /* ── copilot chat ── */
  const scrollThread = () => requestAnimationFrame(() => { const el = threadRef.current; if (el) el.scrollTop = el.scrollHeight; });

  async function runAgent(agentId, extra) {
    const mid = nextId();
    setMessages((m) => [...m, { id: mid, role: 'bot', pending: true, text: 'Generating draft…' }]);
    scrollThread();
    let resp = null;
    try {
      resp = await apiPost('/api/agents/run', { agent_id: agentId, context: buildContext(), extra: extra || null, workshop_id: workshopId });
    } catch (e) { resp = { ok: false, error: e.message || 'request failed' }; }
    setMessages((m) => m.map((x) => x.id !== mid ? x : (
      resp && resp.ok
        ? { id: mid, role: 'bot', draft: resp.draft, agentId, draftState: 'open' }
        : { id: mid, role: 'bot', err: (resp && resp.error) || 'agent failed — check AWS Bedrock credentials in backend/.env' }
    )));
    scrollThread();
  }

  async function send(text) {
    const v = (text ?? input).trim();
    if (!v || busy) return;
    setInput('');
    setBusy(true);
    setCopilotMin(false);
    setMessages((m) => [...m, { id: nextId(), role: 'user', text: v }]);
    scrollThread();
    let resp = null;
    try {
      resp = await apiPost('/api/agents/chat', { message: v, context: buildContext(), workshop_id: workshopId });
    } catch (e) { resp = { ok: false, error: e.message || 'request failed' }; }
    if (resp && resp.ok && resp.kind === 'dispatch') {
      await runAgent(resp.agent_id, resp.extra);
    } else if (resp && resp.ok) {
      setMessages((m) => [...m, { id: nextId(), role: 'bot', text: resp.reply }]);
    } else {
      setMessages((m) => [...m, { id: nextId(), role: 'bot', err: (resp && resp.error) || 'backend unreachable' }]);
    }
    setBusy(false);
    scrollThread();
  }

  function approveDraft(msg) {
    const d = msg.draft;
    const item = {
      folder: d.folder || 'General',
      label: (d.node && d.node.label) || d.title,
      prov: `by ${user?.name || user?.email || 'you'} · from ${PHASES[phase - 1]?.title}`,
      body: d.body_html || '',
      at: new Date().toISOString().slice(0, 16).replace('T', ' '),
      ...(d.diagram && d.diagram.xml ? { xml: d.diagram.xml } : {}),
    };
    setArtifacts((a) => [...a, item]);
    if (d.diagram && d.diagram.xml) setDrawioXml(d.diagram.xml);
    setMessages((m) => m.map((x) => x.id === msg.id ? { ...x, draftState: 'approved' } : x));
    showToast(`${item.label} → filed in ${item.folder}`);
  }
  function rejectDraft(msg) {
    setMessages((m) => m.map((x) => x.id === msg.id ? { ...x, draftState: 'rejected' } : x));
  }

  /* ── uploads ── */
  async function uploadFiles(fileList) {
    for (const f of Array.from(fileList || [])) {
      showToast(`Uploading ${f.name}…`);
      const fd = new FormData();
      fd.append('workshop_id', String(workshopId));
      fd.append('file', f);
      try {
        const r = await fetch('/api/agents/upload', { method: 'POST', credentials: 'same-origin', body: fd });
        const j = await r.json();
        if (j && j.ok) {
          docTexts.current[j.doc_id] = String(j.text || '').slice(0, 20000);
          showToast(`${j.name} ingested (${j.chars.toLocaleString()} chars)`);
        } else showToast(`⚠ ${(j && j.error) || 'upload failed'}`);
      } catch { showToast(`⚠ upload failed: ${f.name}`); }
    }
    try { const r = await apiGet(`/api/agents/prepare-docs?workshop_id=${workshopId}`); if (r && r.ok) hydrateDocs(r.docs || []); } catch {}
  }
  async function deleteDoc(d) {
    if (!confirm(`Remove "${d.name}" from the grounding sources?`)) return;
    try {
      await apiDelete(`/api/agents/document/${d.doc_id}?workshop_id=${workshopId}`);
      delete docTexts.current[d.doc_id];
      setDocs((x) => x.filter((y) => y.doc_id !== d.doc_id));
      showToast('Source removed');
    } catch (e) { showToast(`⚠ ${e.message || 'could not remove'}`); }
  }

  /* ── live capture (browser SpeechRecognition — same engine as before) ── */
  function toggleRecording() {
    if (recording) {
      try { recogRef.current && recogRef.current.stop(); } catch {}
      recogRef.current = null;
      setRecording(false);
      return;
    }
    const SR = typeof window !== 'undefined' && (window.SpeechRecognition || window.webkitSpeechRecognition);
    if (!SR) { showToast('⚠ No speech recognition in this browser — use Chrome/Edge'); return; }
    const rec = new SR();
    rec.continuous = true; rec.interimResults = false; rec.lang = 'en-US';
    rec.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) {
          const t = e.results[i][0].transcript.trim();
          if (t) setTranscript((tr) => [...tr, `${new Date().toTimeString().slice(0, 5)} — ${t}`]);
        }
      }
    };
    rec.onerror = (e) => { if (e.error === 'not-allowed') { showToast('⚠ Microphone permission denied'); setRecording(false); recogRef.current = null; } };
    rec.onend = () => { if (recogRef.current === rec) { try { rec.start(); } catch {} } };  // keep-alive
    try { rec.start(); recogRef.current = rec; setRecording(true); showToast('Live capture started'); } catch { showToast('⚠ could not start capture'); }
  }
  useEffect(() => () => { try { recogRef.current && recogRef.current.stop(); } catch {} }, []);

  /* ── export ── */
  async function exportHandoff(fmt) {
    if (!artifacts.length) { showToast('⚠ No approved artifacts yet — approve a copilot draft first'); return; }
    showToast('Assembling handoff package…');
    try {
      const grouped = {};
      artifacts.forEach((a) => { (grouped[a.folder] = grouped[a.folder] || []).push(a); });
      const payload = Object.entries(grouped).map(([folder, items]) => ({ folder, items }));
      const r = await fetch('/api/export/handoff', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ format: fmt, board_name: workshopName || 'Workshop', artifacts: payload }),
      });
      if (!r.ok) { const j = await r.json().catch(() => null); throw new Error((j && j.error) || 'export failed'); }
      const blob = await r.blob();
      const u = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = u; a.download = `handoff-package.${fmt}`; a.click();
      setTimeout(() => URL.revokeObjectURL(u), 4000);
      showToast('Handoff package downloaded');
    } catch (e) { showToast(`⚠ ${e.message || 'export failed'}`); }
  }

  /* ── draw.io embed (JSON protocol; XML persisted on the board) ── */
  useEffect(() => {
    if (!dioOpen) return;
    function onMsg(ev) {
      const frame = dioFrame.current;
      if (!frame || ev.source !== frame.contentWindow) return;
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.event === 'init') frame.contentWindow.postMessage(JSON.stringify({ action: 'load', xml: drawioXml || '' }), '*');
      else if (msg.event === 'save') { setDrawioXml(msg.xml); showToast('Diagram saved to the board'); frame.contentWindow.postMessage(JSON.stringify({ action: 'exit' }), '*'); }
      else if (msg.event === 'exit') setDioOpen(false);
    }
    window.addEventListener('message', onMsg);
    return () => window.removeEventListener('message', onMsg);
  }, [dioOpen, drawioXml, showToast]);

  /* ── ⌘K ── */
  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setCmdk((v) => !v); setCmdq(''); }
      if (e.key === 'Escape') { setCmdk(false); setDioOpen(false); }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const commands = useMemo(() => [
    ...PHASES.map((p, i) => ({ g: 'Go to phase', t: p.title, ic: String(p.n), run: () => setPhase(i + 1) })),
    { g: 'Actions', t: recording ? 'Stop live capture' : 'Start live capture', ic: '●', run: toggleRecording },
    { g: 'Actions', t: `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`, ic: '◐', run: flipTheme },
    { g: 'Actions', t: 'Open draw.io editor', ic: '⧉', run: () => setDioOpen(true) },
    { g: 'Actions', t: 'Export handoff (DOCX)', ic: '⇪', run: () => exportHandoff('docx') },
    { g: 'Actions', t: 'Export handoff (Markdown)', ic: '⇪', run: () => exportHandoff('md') },
    ...AGENT_QUICK.map(([id, label]) => ({ g: 'Run agent', t: label, ic: '✦', run: () => { setCopilotMin(false); runAgent(id); } })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [recording, theme, artifacts, requirements, transcript, docs, phase]);
  const filtered = commands.filter((c) => !cmdq || (c.t + ' ' + c.g).toLowerCase().includes(cmdq.toLowerCase()));

  /* ── derived ── */
  const initials = ((user && (user.name || user.email)) || '?').trim().split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
  const showSampleReqs = requirements.length === 0;
  const reqRows = showSampleReqs ? SAMPLE_REQS : requirements;
  const teamsConnected = teams && teams.connected;
  const teamsConfigured = teams && teams.configured;

  function addRequirement() {
    const t = reqDraft.trim();
    if (!t) return;
    const code = 'REQ-' + String(requirements.length + 1).padStart(2, '0');
    setRequirements((r) => [...r, { code, category: 'General', priority: 'Must', text: t, source: `added by ${user?.name || 'you'}` }]);
    setReqDraft('');
  }

  return (
    <div className="orbitz-root" data-obtheme={theme}>
      {/* ── Header ── */}
      <header className="ob-header">
        <div className="ob-logo">
          <span className="mark">N</span>
          <div>
            <div className="nm">Navi<b>BA</b> Orbitz</div>
            <div className="tag">Engagement Intelligence</div>
          </div>
        </div>
        <div className="ob-ctx">
          <span className="t">{projectName || 'Project'}</span>
          <span className="s">{workshopName || 'Workshop'} · <span className="code">WS-{workshopId}</span></span>
        </div>
        <div className="ob-hspace" />
        <button className="ob-hbtn" onClick={() => { setCmdk(true); setCmdq(''); }}>⌘K <kbd>ctrl K</kbd></button>
        <button className="ob-hbtn" onClick={flipTheme} title="Toggle theme">{theme === 'dark' ? '☀' : '☾'}</button>
        <button className="ob-hbtn" onClick={() => setCopilotMin((v) => !v)}>✦ Copilot</button>
        <button className="ob-hbtn primary" onClick={() => exportHandoff('docx')}>⇪ Export</button>
        <span className="ob-av" title={user?.name || user?.email}>{initials}</span>
      </header>

      {/* ── Phase stepper ── */}
      <nav className="ob-phases" aria-label="Engagement phases">
        <div className="cap">
          <span className="a">Engagement</span>
          <span className="b">Phase <em>{phase}</em> of <em>4</em></span>
        </div>
        {PHASES.map((p, i) => (
          <button key={p.key} className={'ob-phase' + (phase === i + 1 ? ' on' : phase > i + 1 ? ' done' : '')} onClick={() => setPhase(i + 1)}>
            <span className="num">{phase > i + 1 ? '✓' : p.n}</span>
            <span style={{ minWidth: 0 }}>
              <span className="tt" style={{ display: 'block' }}>{p.title}</span>
              <span className="dd" style={{ display: 'block' }}>{p.sub}</span>
            </span>
          </button>
        ))}
      </nav>

      {/* ── Body ── */}
      <div className="ob-body">
        {/* LEFT — Source Artifacts */}
        <div className="ob-col left">
          <section className="ob-panel">
            <div className="ob-phead">
              <span className="ico">⛁</span>
              <div><div className="tt">Source Artifacts</div><div className="ss">{docs.length} ingested · grounding the copilot</div></div>
              <span className="sp" />
              <button className="ob-mini solid" onClick={() => fileInput.current && fileInput.current.click()}>+ Add</button>
            </div>
            <div
              className={'ob-drop' + (dragOver ? ' over' : '')}
              onClick={() => fileInput.current && fileInput.current.click()}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => { e.preventDefault(); setDragOver(false); uploadFiles(e.dataTransfer.files); }}
            >
              <div className="a">Drop docs, PDFs, transcripts, links</div>
              SharePoint · Teams · Granola · GitHub · draw.io
            </div>
            <input ref={fileInput} type="file" multiple style={{ display: 'none' }} onChange={(e) => { uploadFiles(e.target.files); e.target.value = ''; }} />

            {docs.map((d) => (
              <div className="ob-src" key={d.doc_id}>
                <span className="fic">📄</span>
                <div className="bd">
                  <div className="nm">{d.name}</div>
                  <div className="mt">uploaded{d.uploaded_by ? ` by ${d.uploaded_by}` : ''}</div>
                </div>
                <span className="ob-pill good">Ingested</span>
                <button className="del" onClick={() => deleteDoc(d)} title="Remove source">✕</button>
              </div>
            ))}

            <div className="ob-src">
              <span className="fic">👥</span>
              <div className="bd">
                <div className="nm">Microsoft Teams — meeting transcripts</div>
                <div className="mt">{teamsConnected ? `connected as ${teams.account || 'your account'}` : teamsConfigured ? 'configured · not connected' : 'TEAMS_TENANT_ID not set in backend/.env'}</div>
              </div>
              <span className={'ob-pill ' + (teamsConnected ? 'good' : 'dim')}>{teamsConnected ? 'Connected' : 'Not connected'}</span>
            </div>
            <div className="ob-src">
              <span className="fic">🥣</span>
              <div className="bd">
                <div className="nm">Granola — interview recordings</div>
                <div className="mt">{granola && granola.connected ? 'connected' : 'needs a Granola Business API key'}</div>
              </div>
              <span className={'ob-pill ' + (granola && granola.connected ? 'good' : 'dim')}>{granola && granola.connected ? 'Connected' : 'Not connected'}</span>
            </div>
            <div className="ob-src" data-na="1">
              <span className="fic">⌥</span>
              <div className="bd">
                <div className="nm">GitHub repository scan</div>
                <div className="mt">read-only legacy-code scanning</div>
              </div>
              <span className="ob-pill dim">Sample</span>
            </div>
            <div className="ob-src" data-na="1">
              <span className="fic">🖼</span>
              <div className="bd">
                <div className="nm">Whiteboard photo — handwriting parse</div>
                <div className="mt">value-stream capture from images</div>
              </div>
              <span className="ob-pill dim">Sample</span>
            </div>
          </section>
        </div>

        {/* CENTER */}
        <div className="ob-col">
          {/* Live synthesis banner */}
          <section className="ob-live">
            <div className="top">
              <div>
                <div className="tt">Live Workshop Synthesis</div>
                <div className="dd">Capture the room — transcript, recordings, documents — and fuse it with pre-workshop context to generate requirements, artifacts and process flows as the conversation happens.</div>
              </div>
              <button className={'ob-switch' + (recording ? ' on' : '')} onClick={toggleRecording}>
                {recording ? 'ON' : 'OFF'} · Live capture <span className="knob" />
              </button>
            </div>
            <div className="ob-stats">
              <div className="ob-stat"><div className="v">{reqRows.length}</div><div className="k">Requirements</div></div>
              <div className="ob-stat" data-na="1"><div className="v">12</div><div className="k">Capabilities</div></div>
              <div className="ob-stat"><div className="v">{artifacts.length}</div><div className="k">Artifacts live</div></div>
            </div>
            <div className={'ob-recline' + (recording ? ' on' : '')}>
              <span className="dot" />
              {recording
                ? <span><b>Recording</b> — copilot transcript is capturing in real time ({transcript.length} lines so far).</span>
                : <span>Capture is off. {transcript.length ? `${transcript.length} transcript lines stored on this board.` : 'Turn on Live capture to transcribe via your microphone.'}</span>}
            </div>
          </section>

          {/* Requirements */}
          <section className="ob-panel">
            <div className="ob-phead">
              <span className="ico">☰</span>
              <div><div className="tt">Business Requirements — Live</div><div className="ss">{showSampleReqs ? 'sample rows — add real ones below' : 'stored on this workshop board'}</div></div>
              <span className="sp" />
              {showSampleReqs && <span className="ob-pill dim" data-na="1">Auto-extraction</span>}
              <span className="ob-pill warn">In review</span>
            </div>
            {reqRows.map((r, i) => (
              <div className="ob-req" key={r.code + i} {...(showSampleReqs ? { 'data-na': '1' } : {})}>
                <span className="code">{r.code}</span>
                <div className="bd">
                  <div className="tx">{r.text}</div>
                  <div className="meta">
                    <span className="ob-pill brand">{r.category}</span>
                    <span className={'ob-pill ' + (r.priority === 'Must' ? 'bad' : 'warn')}>{r.priority}</span>
                    <span>{r.source}</span>
                  </div>
                </div>
                {!showSampleReqs && (
                  <button className="del" title="Remove requirement" onClick={() => setRequirements((x) => x.filter((_, j) => j !== i))}>✕</button>
                )}
              </div>
            ))}
            <div className="ob-addreq">
              <input
                value={reqDraft}
                onChange={(e) => setReqDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') addRequirement(); }}
                placeholder="Add a requirement — e.g. 'The system shall …'"
              />
              <button className="ob-mini solid" onClick={addRequirement}>+ Add requirement</button>
            </div>
          </section>

          {/* Capability map */}
          <section className="ob-panel">
            <div className="ob-phead">
              <span className="ico">▦</span>
              <div><div className="tt">Business Capability Map — v1.0</div><div className="ss">heat-mapped by maturity & optimization opportunity</div></div>
              <span className="sp" />
              <span className="ob-pill dim" data-na="1">Sample data</span>
            </div>
            <div className="ob-capgrid" data-na="1">
              {CAP_MAP.map((d) => (
                <div className="ob-capdom" key={d.domain}>
                  <div className="dn">{d.domain}</div>
                  {d.caps.map(([nm, heat, mat]) => (
                    <div className={'ob-cap ' + heat} key={nm}>
                      {nm}
                      <span className="bars">{[1, 2, 3, 4, 5].map((n) => <i key={n} className={n <= mat ? 'f' : ''} />)}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
            <div className="ob-legend">
              <span>Opportunity:</span>
              <span><span className="sw" style={{ background: 'var(--danger)' }} />High</span>
              <span><span className="sw" style={{ background: 'var(--warning)' }} />Medium</span>
              <span><span className="sw" style={{ background: 'var(--positive)' }} />Low</span>
              <span style={{ marginLeft: 'auto' }}>Bars = maturity (1–5)</span>
            </div>
          </section>

          {/* Process flow */}
          <section className="ob-panel">
            <div className="ob-phead">
              <span className="ico">⧉</span>
              <div><div className="tt">Business Process Flow</div><div className="ss">editable in embedded draw.io — XML stored on this board</div></div>
              <span className="sp" />
              {drawioXml && <span className="ob-pill good">Saved</span>}
              <button className="ob-mini" onClick={() => runAgent('drawflow')}>✦ Generate with AI</button>
              <button className="ob-mini solid" onClick={() => setDioOpen(true)}>Edit in draw.io</button>
            </div>
            <div className="ob-flowbar">
              <span className="fn">{drawioXml ? 'process-flow.drawio' : 'no diagram yet'}</span>
              <span className="sp" style={{ flex: 1 }} />
              <button className="ob-mini" data-na="1">−</button>
              <button className="ob-mini" data-na="1">100%</button>
              <button className="ob-mini" data-na="1">+</button>
              <button
                className="ob-mini"
                onClick={() => {
                  if (!drawioXml) { showToast('⚠ No diagram yet — generate or draw one first'); return; }
                  const b = new Blob([drawioXml], { type: 'application/xml' });
                  const u = URL.createObjectURL(b); const a = document.createElement('a');
                  a.href = u; a.download = 'process-flow.drawio'; a.click();
                  setTimeout(() => URL.revokeObjectURL(u), 4000);
                }}
              >⇪ Export XML</button>
            </div>
            <div className="ob-flowbody">
              {drawioXml ? (
                <div className="ob-flowempty">A diagram is stored on this board — open <b>Edit in draw.io</b> to view and refine it.</div>
              ) : (
                <>
                  <div className="ob-lane"><span className="ln">Planner</span><span className="steps"><span className="ob-step">Demand forecast</span><span className="ob-arrow">→</span><span className="ob-step">Draft roster</span><span className="ob-arrow">→</span><span className="ob-step hl">Publish shift plan</span></span></div>
                  <div className="ob-lane"><span className="ln">Supervisor</span><span className="steps"><span className="ob-step">Review coverage</span><span className="ob-arrow">→</span><span className="ob-step">Swap / cover requests</span></span></div>
                  <div className="ob-lane"><span className="ln">QA</span><span className="steps"><span className="ob-step">Qualification check</span><span className="ob-arrow">→</span><span className="ob-step">Line clearance</span></span></div>
                  <div className="ob-flowempty" style={{ paddingTop: 10 }}>Sample swimlane — click <b>✦ Generate with AI</b> to draft a real one from this workshop's context.</div>
                </>
              )}
            </div>
          </section>
        </div>

        {/* RIGHT — Workshop Artifacts */}
        <div className="ob-col right">
          <section className="ob-panel" style={{ border: 'none', background: 'transparent' }}>
            <div className="ob-phead" style={{ border: 'none', paddingLeft: 2 }}>
              <span className="ico">▣</span>
              <div><div className="tt">Workshop Artifacts</div><div className="ss">{artifacts.length ? `${artifacts.length} approved · included in Export` : 'approve copilot drafts to fill this'}</div></div>
            </div>
            {artifacts.length === 0 && (
              <>
                {[['BRD', 'Business Requirements Document', 'Live BRD assembled from pre-workshop context + discovery capture, traced to source utterances.', 79],
                  ['Capability Map', 'Business Capability Map — v1.0', 'Heat-mapped by maturity and optimization opportunity, reconciled with workshop findings.', 82],
                  ['Process Flow', 'Shift Planning (draw.io)', 'Current-state swimlane generated as an editable draw.io diagram.', 71]].map(([k, t, d, p]) => (
                    <div className="ob-art" key={k} data-na="1">
                      <div className="top"><span className="kind">{k}</span><span className="sp" style={{ flex: 1 }} /><span className="ob-pill dim">Sample</span></div>
                      <div className="tt">{t}</div>
                      <div className="dd">{d}</div>
                      <div className="foot">BA Copilot · sample<span className="ob-ring" style={{ '--p': p }}><i>{p}%</i></span></div>
                    </div>
                  ))}
              </>
            )}
            {artifacts.map((a, i) => (
              <div className="ob-art" key={i}>
                <div className="top">
                  <span className="kind">{a.folder}</span>
                  <span className="sp" style={{ flex: 1 }} />
                  <span className="ob-pill good">Approved</span>
                </div>
                <div className="tt">{a.label}</div>
                <div className="dd">{a.prov}</div>
                <div className="foot">
                  {a.at}
                  <span className="ob-ring" style={{ '--p': 100 }} data-na="1"><i>100%</i></span>
                  <button className="ob-mini" style={{ marginLeft: 6 }} title="Remove artifact" onClick={() => setArtifacts((x) => x.filter((_, j) => j !== i))}>✕</button>
                </div>
              </div>
            ))}
            {artifacts.length > 0 && (
              <div style={{ display: 'flex', gap: 8, padding: '4px 2px' }}>
                <button className="ob-mini solid" onClick={() => exportHandoff('docx')}>⇪ Export DOCX</button>
                <button className="ob-mini" onClick={() => exportHandoff('md')}>Export MD</button>
              </div>
            )}
          </section>
        </div>
      </div>

      {/* ── BA Copilot dock ── */}
      <section className={'ob-copilot' + (copilotMin ? ' min' : '')}>
        <div className="ob-chead" onClick={() => setCopilotMin((v) => !v)}>
          <span className="badge">✦</span>
          <span className="tt">BA Copilot</span>
          <span className="ss">Grounded on {docs.length} source{docs.length === 1 ? '' : 's'} · {projectName || 'workshop'} context</span>
          <span className="sp" style={{ flex: 1 }} />
          <span className="ob-pill dim" data-na="1" onClick={(e) => e.stopPropagation()}>🌐 Web + Docs</span>
          <span className="ob-pill brand">{copilotMin ? 'expand ▴' : 'collapse ▾'}</span>
        </div>
        <div className="ob-cbody">
          <div className="ob-thread" ref={threadRef}>
            {messages.length === 0 && (
              <div className="ob-msg bot">
                <div className="who">✦ Copilot</div>
                <div className="body">
                  I'm grounded on this workshop's uploaded sources{docs.length ? ` (${docs.length} so far)` : ' — none uploaded yet'} and the live transcript.
                  Ask me anything, or ask for an artifact ("draw the process flow", "draft user stories") and I'll produce a reviewable draft.
                </div>
              </div>
            )}
            {messages.map((m) => (
              <div key={m.id} className={'ob-msg ' + m.role}>
                {m.role === 'bot' && <div className="who">✦ Copilot</div>}
                {m.err && <div className="body err">⚠ {m.err}</div>}
                {m.pending && <div className="body" style={{ color: 'var(--ink-faint)' }}>{m.text}</div>}
                {!m.err && !m.pending && !m.draft && <div className="body" style={{ whiteSpace: 'pre-wrap' }}>{m.text}</div>}
                {m.draft && (
                  <div className="ob-draft">
                    <div className="dh">✦ {m.draft.title}</div>
                    {/* body_html is sanitised server-side (agent_catalog.sanitize_html) */}
                    <div className="db" dangerouslySetInnerHTML={{ __html: m.draft.body_html || '' }} />
                    <div className="da">
                      {m.draftState === 'open' && (
                        <>
                          <button className="ob-mini solid" onClick={() => approveDraft(m)}>✓ Approve → Artifacts</button>
                          {m.draft.diagram && m.draft.diagram.xml && (
                            <button className="ob-mini" onClick={() => { setDrawioXml(m.draft.diagram.xml); setDioOpen(true); }}>Edit in draw.io</button>
                          )}
                          <button className="ob-mini" onClick={() => rejectDraft(m)}>✕ Discard</button>
                        </>
                      )}
                      {m.draftState === 'approved' && <span className="ob-pill good">✓ Filed in {m.draft.folder || 'General'}</span>}
                      {m.draftState === 'rejected' && <span className="ob-pill dim">Discarded — nothing filed</span>}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
        <div className="ob-chips">
          {SUGGESTED.map((s) => <button key={s} className="ob-chip" onClick={() => send(s)}>{s}</button>)}
          {AGENT_QUICK.slice(0, 3).map(([id, label]) => (
            <button key={id} className="ob-chip" onClick={() => { setCopilotMin(false); runAgent(id); }}>✦ {label}</button>
          ))}
        </div>
        <div className="ob-composer">
          <div className="box">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
              placeholder={busy ? 'Copilot is thinking…' : 'Ask the copilot, or request an artifact…'}
              disabled={busy}
            />
            <button className="send" onClick={() => send()} disabled={busy} aria-label="Send">➤</button>
          </div>
        </div>
        <div className="ob-cfoot">Responses are grounded on ingested sources first, then this board's requirements & transcript.</div>
      </section>

      {/* ── ⌘K palette ── */}
      {cmdk && (
        <div className="ob-cmdk-bg" onClick={(e) => { if (e.target === e.currentTarget) setCmdk(false); }}>
          <div className="ob-cmdk">
            <div className="in">
              <span>🔍</span>
              <input autoFocus value={cmdq} onChange={(e) => setCmdq(e.target.value)} placeholder="Type a command…" />
              <kbd style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--ink-faint)' }}>esc</kbd>
            </div>
            <div className="ls">
              {['Go to phase', 'Actions', 'Run agent'].map((g) => {
                const items = filtered.filter((c) => c.g === g);
                if (!items.length) return null;
                return (
                  <div key={g}>
                    <div className="gr">{g}</div>
                    {items.map((c) => (
                      <div key={c.t} className="it" onClick={() => { setCmdk(false); c.run(); }}>
                        <span className="ic">{c.ic}</span>{c.t}
                      </div>
                    ))}
                  </div>
                );
              })}
              {!filtered.length && <div className="gr" style={{ padding: 14 }}>No matching command</div>}
            </div>
          </div>
        </div>
      )}

      {/* ── draw.io modal ── */}
      {dioOpen && (
        <div className="ob-dio-bg">
          <div className="ob-dio">
            <div className="h">
              <b>draw.io editor</b>
              <span style={{ color: 'var(--ink-faint)' }}>embed.diagrams.net — Save writes the XML back onto this board</span>
              <span style={{ flex: 1 }} />
              <button className="ob-mini" onClick={() => setDioOpen(false)}>✕ Close</button>
            </div>
            <iframe ref={dioFrame} title="draw.io" src="https://embed.diagrams.net/?embed=1&ui=atlas&spin=1&proto=json" />
          </div>
        </div>
      )}

      {toast && <div className="ob-toast">{toast}</div>}
    </div>
  );
}
