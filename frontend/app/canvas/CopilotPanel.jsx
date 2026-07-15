'use client';

import { useEffect, useRef, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../lib/api';
import { Icon } from '../lib/icons';
import DocumentViewer from './preworkshop/DocumentViewer';
import DrawioViewer from './preworkshop/DrawioViewer';

// Same 5 steps the Pre-Workshop dashboard's Research Chain shows (see
// agent_catalog._RESEARCH_STEPS) — deepresearch always runs all of them,
// analysing every ingested document AND searching the web, regardless of
// whether the documents alone would already answer the question.
const RESEARCH_STEP_ORDER = ['ingest', 'extract', 'queries', 'search', 'synthesize'];
const RESEARCH_STEP_LABELS = {
  ingest: 'Ingest client docs', extract: 'Extract context', queries: 'Formulate queries',
  search: 'Search & reconcile', synthesize: 'Synthesize document',
};

// The one Copilot — a context-grounded assistant available from AppHeader
// on every phase (see [workshopId]/page.js), backed by the same
// /api/agents/chat + /api/agents/run routes the canvas's own composer
// already uses (agent_catalog.route_chat / run_agent). Capabilities:
//   - Answers grounded in the workshop's indexed documents (RAG).
//   - Follows up correctly: recent turns are persisted server-side (one
//     thread per workshop — app/services/copilot_thread.py) and sent
//     back on every call, so "what about the second one?" resolves.
//   - A real web-search tool (agent_catalog._web_search_reply, Tavily)
//     for questions the ingested documents can't answer — routed to
//     automatically, cited with source chips.
//   - Can run any of the 20 registered agents via a "Run /agent" button
//     (never auto-run — a misfire shouldn't burn an LLM call silently).
//     deepresearch is the general-purpose pick when the ask is "build me
//     a document" and doesn't match a narrower agent by name.
//   - Renders a workflow's diagram inline (a compact node/edge preview,
//     Claude-artifact style) with a button to open the full interactive
//     draw.io editor — not just a bare download link.
// Where results land: `doc: true` agents persist to generated_docs
// regardless of zone — this panel opens that doc/diagram directly. For
// zones OTHER than Pre-Workshop the only current viewer for generated
// docs is still the Pre-Workshop Artifacts grid (there's no equivalent
// grid on the canvas-based phases yet), so the copy says exactly that
// rather than implying it was filed somewhere phase-specific it wasn't.
// `doc: false` agents (drawflow, findgaps, ingest, decisions,
// opportunities, roi, risk, team) only ever produce a canvas card —
// Copilot has no canvas node to place one on (that would need a bridge
// into canvasApp.js's own board state, not built yet), so those render
// inline as a plain draft with an honest note that nothing was saved.
// Drag-to-resize: the panel sits flush against .cop-backdrop's right
// padding (20px — see preworkshop.css), so its left edge tracks
// `window.innerWidth - 20 - clientX` while dragging. Width persists in
// localStorage (same pattern ArtifactExplorer uses for its expand state).
const COP_WIDTH_KEY = 'aidc-copilot-width';
const COP_MIN_WIDTH = 360;
const COP_BACKDROP_PAD = 20;

function clampCopWidth(w) {
  const max = Math.min(900, (typeof window !== 'undefined' ? window.innerWidth : 1200) - 80);
  return Math.max(COP_MIN_WIDTH, Math.min(w, max));
}

function loadCopWidth() {
  try {
    const n = parseInt(window.localStorage.getItem(COP_WIDTH_KEY), 10);
    if (Number.isFinite(n)) return clampCopWidth(n);
  } catch { /* fresh default below */ }
  return 420;
}

export default function CopilotPanel({ open, onClose, workshopId, zone, contextName }) {
  const [messages, setMessages] = useState([]);
  const [panelWidth, setPanelWidth] = useState(() => (typeof window === 'undefined' ? 420 : loadCopWidth()));
  const widthRef = useRef(panelWidth);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [runningId, setRunningId] = useState(null); // index of the dispatch message currently running
  const [liveChain, setLiveChain] = useState(null); // deepresearch's live step trace while runningId is set
  const [error, setError] = useState('');
  const [meta, setMeta] = useState({ doc_count: 0, suggestions: [] });
  const [viewerDocId, setViewerDocId] = useState(null);
  const [viewerDiagram, setViewerDiagram] = useState(null);
  const listRef = useRef(null);

  useEffect(() => {
    if (!workshopId) return;
    (async () => {
      try {
        const data = await apiGet(`/api/agents/copilot/messages?workshop_id=${workshopId}`);
        if (data && data.ok) setMessages(data.messages || []);
      } catch { /* fresh conversation is a fine fallback */ }
      try {
        // Header source-count + corpus-specific suggestion chips — from
        // the workshop-context cache, so this costs no LLM call.
        const m = await apiGet(`/api/agents/copilot/meta?workshop_id=${workshopId}`);
        if (m && m.ok) setMeta({ doc_count: m.doc_count || 0, suggestions: m.suggestions || [] });
      } catch { /* chips just don't render */ }
    })();
  }, [workshopId]);

  if (!open) return null;

  function scrollDown() {
    requestAnimationFrame(() => { if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight; });
  }

  function persist(message) {
    if (!workshopId) return;
    apiPost('/api/agents/copilot/messages', { workshop_id: workshopId, message }).catch(() => {});
  }

  function pushMessage(message) {
    setMessages((m) => [...m, message]);
    persist(message);
  }

  async function clearHistory() {
    setMessages([]);
    if (!workshopId) return;
    try { await apiDelete(`/api/agents/copilot/messages?workshop_id=${workshopId}`); } catch { /* best-effort */ }
  }

  // Streaming send: reads NDJSON frames from /api/agents/chat/stream and
  // grows the assistant bubble token-by-token. Returns false when the
  // stream endpoint is unusable BEFORE any text arrived (older backend,
  // proxy that can't stream, network error) so send() can fall back to
  // the blocking route — streaming is a latency upgrade, never a
  // functionality gate.
  async function sendStreaming(text) {
    const res = await fetch('/api/agents/chat/stream', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, workshop_id: workshopId, context: { zone: zone || 'Engagement' } }),
    });
    if (!res.ok || !res.body) return false;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let started = false;   // an assistant bubble exists and is growing
    let full = '';
    let sources = [];

    const appendDelta = (chunk) => {
      full += chunk;
      if (!started) {
        started = true;
        setMessages((m) => [...m, { role: 'assistant', kind: 'text', text: full, sources }]);
      } else {
        setMessages((m) => {
          const next = m.slice();
          next[next.length - 1] = { ...next[next.length - 1], text: full };
          return next;
        });
      }
      scrollDown();
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let frame;
        try { frame = JSON.parse(line); } catch { continue; }
        if (frame.type === 'meta') {
          if (frame.kind === 'dispatch') {
            pushMessage({ role: 'assistant', kind: 'dispatch', agentId: frame.agent_id, extra: frame.extra || '' });
            return true;
          }
          sources = frame.sources || [];
        } else if (frame.type === 'delta') {
          appendDelta(frame.text || '');
        } else if (frame.type === 'done') {
          const finalText = (frame.reply || full).trim();
          if (started) {
            setMessages((m) => {
              const next = m.slice();
              next[next.length - 1] = { ...next[next.length - 1], text: finalText };
              return next;
            });
          } else {
            setMessages((m) => [...m, { role: 'assistant', kind: 'text', text: finalText, sources }]);
          }
          persist({ role: 'assistant', kind: 'text', text: finalText, sources });
          return true;
        } else if (frame.type === 'error') {
          if (started) {
            // Partial answer already on screen — keep it, surface the break.
            setError(frame.error || 'the reply was cut off');
            persist({ role: 'assistant', kind: 'text', text: full, sources });
            return true;
          }
          return false;   // nothing shown yet — let the blocking route retry
        }
      }
    }
    if (started) {
      persist({ role: 'assistant', kind: 'text', text: full, sources });
      return true;
    }
    return false;
  }

  async function sendBlocking(text) {
    const res = await apiPost('/api/agents/chat', {
      message: text, workshop_id: workshopId, context: { zone: zone || 'Engagement' },
    });
    if (!res.ok) {
      setError(res.error || 'could not get a reply');
    } else if (res.kind === 'dispatch') {
      pushMessage({ role: 'assistant', kind: 'dispatch', agentId: res.agent_id, extra: res.extra || '' });
    } else {
      pushMessage({ role: 'assistant', kind: 'text', text: res.reply || '', sources: res.sources || [] });
    }
  }

  async function submitMessage(raw) {
    const text = (raw || '').trim();
    if (!text || sending) return;
    setInput('');
    setError('');
    pushMessage({ role: 'user', kind: 'text', text });
    setSending(true);
    try {
      let handled = false;
      try {
        handled = await sendStreaming(text);
      } catch { /* stream endpoint unusable — blocking fallback below */ }
      if (!handled) await sendBlocking(text);
    } catch (err) {
      setError(err.message || 'could not get a reply');
    } finally {
      setSending(false);
      scrollDown();
    }
  }

  function send() { submitMessage(input); }

  async function runDispatch(index, agentId, extra) {
    setRunningId(index);
    setError('');
    // deepresearch is the only agent with a live, polled progress trace
    // (research_runs — see the Pre-Workshop dashboard's Research Chain).
    // Reusing it here is what actually SHOWS the facilitator it's doing
    // the real multi-step thing (analyse every ingested doc, then search
    // the web, then synthesize) instead of a bare spinner they have to
    // trust blindly.
    let pollTimer = null;
    if (agentId === 'deepresearch') {
      setLiveChain([]);
      pollTimer = setInterval(async () => {
        try {
          const data = await apiGet(`/api/agents/research-chain?workshop_id=${workshopId}`);
          if (data && data.ok && data.run) setLiveChain(data.run.steps || []);
        } catch { /* transient — next tick retries */ }
      }, 1200);
    }
    try {
      const res = await apiPost('/api/agents/run', {
        agent_id: agentId, workshop_id: workshopId, context: { zone: zone || 'Engagement' },
        extra: extra || undefined,
      });
      if (!res.ok) {
        pushMessage({ role: 'assistant', kind: 'text', text: `⚠ /${agentId} failed — ${res.error || 'try again'}` });
      } else {
        const d = res.draft;
        pushMessage({
          role: 'assistant', kind: 'result', agentId, title: d.title, zone: d.zone,
          docId: d.node && d.node.docId, diagram: d.diagram || null,
          bodyHtml: d.body_html,
        });
      }
    } catch (err) {
      pushMessage({ role: 'assistant', kind: 'text', text: `⚠ /${agentId} failed — ${err.message || 'try again'}` });
    } finally {
      if (pollTimer) clearInterval(pollTimer);
      setLiveChain(null);
      setRunningId(null);
      scrollDown();
    }
  }

  function onKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  }

  function startResize(e) {
    e.preventDefault();
    const onMove = (ev) => {
      const w = clampCopWidth(window.innerWidth - COP_BACKDROP_PAD - ev.clientX);
      widthRef.current = w;
      setPanelWidth(w);
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      try { window.localStorage.setItem(COP_WIDTH_KEY, String(widthRef.current)); } catch { /* private mode etc. */ }
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  return (
    <div className="cop-backdrop" onClick={onClose}>
      <div className="cop-panel" style={{ width: panelWidth }} onClick={(e) => e.stopPropagation()}>
        <div className="cop-resize-handle" onMouseDown={startResize} title="Drag to resize" />
        <div className="cop-head">
          <span className="cop-ic"><Icon name="sparkles" /><span className="cop-online" /></span>
          <div>
            <div className="cop-title">BA Copilot</div>
            <div className="cop-sub">
              {meta.doc_count > 0
                ? `Grounded on ${meta.doc_count} source${meta.doc_count === 1 ? '' : 's'} · ${contextName || zone || 'this engagement'} context`
                : `${contextName || zone || 'This engagement'} — no sources ingested yet`}
            </div>
          </div>
          <span className="cop-mode-pill" title="Answers ground on ingested documents first, then live web search when needed">
            <Icon name="globe" />Web + Docs
          </span>
          <button className="cop-clear" onClick={clearHistory} title="Clear conversation" disabled={messages.length === 0}>
            <Icon name="trash" />
          </button>
          <button className="cop-close" onClick={onClose} title="Close"><Icon name="x" /></button>
        </div>

        <div className="cop-thread" ref={listRef}>
          {messages.length === 0 && (
            <div className="cop-empty">
              Ask about the ingested documents, the research so far, or something the web would
              know — or ask for a work product directly ("write the user stories", "draft the
              SOW", "what's the risk here") and I'll offer to run it.
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`cop-msg cop-msg-${m.role}`}>
              {m.role === 'assistant' && <span className="cop-msg-who"><Icon name="sparkles" />Copilot</span>}

              {m.kind === 'text' && (
                <div className="cop-msg-body">
                  {m.text}
                  {(m.sources || []).length > 0 && (
                    <div className="cop-sources">
                      {m.sources.map((s, j) => (
                        s.url ? (
                          <a key={j} className="cop-source-chip" href={s.url} target="_blank" rel="noopener noreferrer">
                            <Icon name="globe" />{s.label}
                          </a>
                        ) : (
                          <span key={j} className="cop-source-chip"
                            title={s.kind === 'generated' ? 'From a document Copilot generated earlier' : 'From an uploaded source document'}>
                            <Icon name={s.kind === 'generated' ? 'sparkles' : 'doc-text'} />{s.label}
                          </span>
                        )
                      ))}
                    </div>
                  )}
                </div>
              )}

              {m.kind === 'dispatch' && (
                <div className="cop-msg-body">
                  That sounds like a job for <b>/{m.agentId}</b>{m.extra ? ` ("${m.extra}")` : ''}.
                  {m.agentId === 'deepresearch' && (
                    <div className="cop-plan">
                      This will analyze every ingested document, search the web for anything they
                      don't cover, and synthesize a cited document — not just one or the other.
                    </div>
                  )}
                  {runningId === i && liveChain ? (
                    <ul className="cop-chain">
                      {RESEARCH_STEP_ORDER.map((key, si) => {
                        const s = liveChain.find((x) => x.step === key);
                        const isCurrent = !s && (si === 0 || liveChain.find((x) => x.step === RESEARCH_STEP_ORDER[si - 1]));
                        return (
                          <li key={key} className={s ? 'done' : isCurrent ? 'pending' : ''}>
                            <span className="cop-chain-dot">{s ? <Icon name="check" /> : si + 1}</span>
                            <div>
                              <div className="cop-chain-label">{RESEARCH_STEP_LABELS[key]}</div>
                              {s && <div className="cop-chain-detail">{s.detail}</div>}
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  ) : (
                    <div className="cop-msg-actions">
                      <button className="btn solid" onClick={() => runDispatch(i, m.agentId, m.extra)} disabled={runningId === i}>
                        <Icon name="sparkles" />{runningId === i ? 'Running…' : `Run /${m.agentId}`}
                      </button>
                    </div>
                  )}
                </div>
              )}

              {m.kind === 'result' && (
                <div className="cop-msg-body">
                  <div className="cop-result-ttl"><Icon name="check-circle" />{m.title}</div>

                  {m.diagram && <DiagramPreview diagrams={m.diagram.diagrams} />}

                  {(m.docId || m.diagram) ? (
                    <>
                      <div className="cop-landed">
                        Saved as a {m.zone} document{m.zone !== 'Pre-Workshop' ? ' — find it later under Pre-Workshop → Artifacts' : ''}.
                      </div>
                      <div className="cop-msg-actions">
                        {m.docId && (
                          <button className="btn" onClick={() => setViewerDocId(m.docId)}><Icon name="search" />Open document</button>
                        )}
                        {m.diagram && (
                          <button className="btn" onClick={() => setViewerDiagram({ xml: m.diagram.xml, title: m.title })}>
                            <Icon name="flow" />Open full diagram
                          </button>
                        )}
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="cop-nowhere">
                        /{m.agentId} only places a card on the {m.zone} canvas board — Copilot can't do
                        that from here yet, so nothing was saved. Copy what you need below, or run
                        /{m.agentId} from the {m.zone} tab directly.
                      </div>
                      <div className="cop-draft-html" dangerouslySetInnerHTML={{ __html: m.bodyHtml }} />
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
          {sending && <div className="cop-msg cop-msg-assistant"><span className="cop-msg-who"><Icon name="sparkles" />Copilot</span><div className="cop-msg-body cop-thinking">Thinking…</div></div>}
          {error && <div className="app-error" style={{ margin: '8px 16px' }}>⚠ {error}</div>}
        </div>

        <div className="cop-foot">
          {meta.suggestions.length > 0 && (
            <div className="cop-suggestions">
              {meta.suggestions.map((q, i) => (
                <button key={i} className="cop-suggestion" onClick={() => submitMessage(q)} disabled={sending} title={q}>
                  {q}
                </button>
              ))}
            </div>
          )}
          <div className="cop-composer">
            <div className="cop-input-wrap">
              <span className="cop-input-ic"><Icon name="doc-text" /></span>
              <textarea
                rows={1}
                placeholder="Ask the copilot — grounded on this engagement…"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                disabled={sending}
              />
            </div>
            <button className="cop-send" onClick={send} disabled={sending || !input.trim()} title="Send">
              <Icon name="send" />
            </button>
          </div>
          <div className="cop-grounding-note">
            <Icon name="sparkles" />Responses are grounded on ingested sources first, then external web research.
          </div>
        </div>
      </div>

      {viewerDocId && (
        <DocumentViewer workshopId={workshopId} docId={viewerDocId} onClose={() => setViewerDocId(null)} />
      )}
      {viewerDiagram && (
        <DrawioViewer xml={viewerDiagram.xml} title={viewerDiagram.title} onClose={() => setViewerDiagram(null)} />
      )}
    </div>
  );
}

// Compact inline preview of a workflow diagram's process(es) — labeled
// chips connected by arrows, same visual language as the canvas's own
// seed-flow strip (see canvasApp.js's diagramPreviewHtml). Not the real
// diagram (that needs the full draw.io editor — see "Open full
// diagram"), just enough of a glance to know what got built without
// leaving the chat, the way an inline artifact preview would.
function DiagramPreview({ diagrams }) {
  return (
    <div className="cop-diagram-preview">
      {(diagrams || []).slice(0, 4).map((dg, i) => (
        <div key={i} className="cop-diagram-proc">
          {dg.title && <div className="cop-diagram-proc-ttl">{dg.title}</div>}
          <div className="cop-diagram-chips">
            {(dg.nodes || []).map((n, j) => (
              <span key={n.id || j} className="cop-chip-wrap">
                {j > 0 && <span className="cop-chip-arrow">→</span>}
                <span className={`cop-chip cop-chip-${n.type || 'process'}`}>{n.label}</span>
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
