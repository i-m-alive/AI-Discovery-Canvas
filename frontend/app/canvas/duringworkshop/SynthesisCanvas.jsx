'use client';

import { useState } from 'react';
import { Icon } from '../../lib/icons';
import { agentIcon, fileType, isTranscript } from '../artifactMeta';

// The Synthesis Canvas — the During-Workshop workbench. The facilitator
// composes a WORKING SET by dragging artifacts from the explorer (or
// clicking a row's "+ to canvas"), optionally types an instruction, and
// fires a generator. The run is scoped server-side to exactly this set
// (options.doc_ids -> agent_catalog._selection_files) and the output
// artifact records what built it (inputs_json).
//
// Items: [{kind: 'source'|'generated', doc_id, name}] — persisted per
// workshop in sessionStorage so the set survives tab switches (the
// natural flow "same set -> requirements -> capability map -> BRD").

export const CANVAS_DRAG_TYPE = 'application/x-aidc-artifact';

// Dependency order matters: capmap reads the requirements table, brd
// reads both — the pipeline always runs in THIS order regardless of the
// order outputs were toggled on.
export const GENERATORS = [
  { id: 'extract_reqs', label: 'Requirements', icon: 'list' },
  { id: 'workflow', label: 'Process flow', icon: 'flow' },
  { id: 'capmap', label: 'Capability map', icon: 'target' },
  { id: 'brd', label: 'BRD', icon: 'doc-text' },
];

export function loadCanvasSet(workshopId) {
  try {
    const raw = window.sessionStorage.getItem(`aidc-canvas-${workshopId}`);
    if (raw) return JSON.parse(raw);
  } catch { /* fresh set */ }
  return [];
}

export default function SynthesisCanvas({
  workshopId, items, onItemsChange, docs, artifacts,
  pipeline,             // [{id, label, status: 'pending'|'running'|'done'|'failed', note}] | null
  onGenerate,           // (agentIds[], prompt) -> Promise — runs the pipeline
  onImportTeams,
  lastResult,           // {label, docId?} | null — one-line outcome of the last run
  onOpenResult,         // (docId) -> open viewer
  transcriptsCount,
}) {
  const [prompt, setPrompt] = useState('');
  const [over, setOver] = useState(false);
  // Which outputs the Run button generates — all on by default ("run the
  // whole workshop"), individually toggleable for a narrower run.
  const [wanted, setWanted] = useState(() => Object.fromEntries(GENERATORS.map((g) => [g.id, true])));
  const running = !!pipeline && pipeline.some((s) => s.status === 'running' || s.status === 'pending');
  const wantedIds = GENERATORS.filter((g) => wanted[g.id]).map((g) => g.id);
  // NOTE: persistence lives in the parent (DuringWorkshopDashboard), not
  // here — a save-effect in this child would fire on first mount with the
  // empty initial list and wipe the stored set before the parent's
  // restore-effect ever reads it (child effects run before parent ones).

  function addItem(it) {
    if (!it || !it.doc_id) return;
    onItemsChange((prev) => prev.some((x) => x.doc_id === it.doc_id) ? prev : [...prev, it]);
  }
  function removeItem(docId) {
    onItemsChange((prev) => prev.filter((x) => x.doc_id !== docId));
  }
  function addAll() {
    const all = [
      ...docs.map((d) => ({ kind: 'source', doc_id: d.doc_id, name: d.name })),
      ...artifacts.map((a) => ({ kind: 'generated', doc_id: a.doc_id, name: a.name })),
    ];
    onItemsChange(all.slice(0, 12));
  }

  function onDrop(e) {
    e.preventDefault();
    setOver(false);
    try {
      const it = JSON.parse(e.dataTransfer.getData(CANVAS_DRAG_TYPE) || '{}');
      addItem(it);
    } catch { /* not one of ours */ }
  }

  const empty = items.length === 0;

  return (
    <section className="pw-panel sc">
      <div className="pw-panel-head">
        <div className="pw-panel-ttl">
          <span className="pw-ic pw-ic-accent"><Icon name="sparkles" /></span>
          <div>
            <div className="pw-h3">Synthesis Canvas</div>
            <div className="pw-sub">
              Drag artifacts from the left panel — generation reads only what's here.
              {transcriptsCount === 0 && ' No transcripts imported yet.'}
            </div>
          </div>
        </div>
        <button className="btn" onClick={onImportTeams}><Icon name="users" />Import from Teams</button>
      </div>

      <div className={'sc-drop' + (over ? ' over' : '') + (empty ? ' empty' : '')}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}>
        {empty ? (
          <div className="sc-empty">
            <Icon name="upload" />
            <div>Drop documents, transcripts or generated artifacts here</div>
            <button className="sc-addall" onClick={addAll} disabled={docs.length + artifacts.length === 0}>
              or add everything in the workshop
            </button>
          </div>
        ) : (
          <>
            {items.map((it) => {
              const ft = it.kind === 'source' ? fileType(it.name) : null;
              return (
                <span key={it.doc_id} className={'sc-chip' + (it.kind === 'generated' ? ' gen' : '')}>
                  <span className="sc-chip-ic" style={ft ? { background: ft.bg, color: ft.fg } : undefined}>
                    <Icon name={it.kind === 'source'
                      ? (isTranscript(it.name) ? 'users' : ft.icon)
                      : agentIcon(it.agent_id)} />
                  </span>
                  <span className="sc-chip-name" title={it.name}>{it.name}</span>
                  <button className="sc-chip-x" onClick={() => removeItem(it.doc_id)} title="Remove from canvas">
                    <Icon name="x" />
                  </button>
                </span>
              );
            })}
            <button className="sc-clear" onClick={() => onItemsChange([])} title="Clear the canvas">clear</button>
          </>
        )}
      </div>

      <textarea className="pw-instruction sc-prompt" rows={2}
        placeholder='Prompt (optional) — e.g. "focus on compliance and audit-trail needs"'
        value={prompt} onChange={(e) => setPrompt(e.target.value)} />

      <div className="sc-actions">
        <span className="sc-actions-lbl">Outputs</span>
        {GENERATORS.map((g) => (
          <button key={g.id}
            className={'sc-out-chip' + (wanted[g.id] ? ' on' : '')}
            disabled={running}
            aria-pressed={wanted[g.id]}
            title={wanted[g.id] ? `Will generate ${g.label} — click to skip it` : `Skipped — click to generate ${g.label} too`}
            onClick={() => setWanted((w) => ({ ...w, [g.id]: !w[g.id] }))}>
            <Icon name={g.icon} />{g.label}
            <span className="sc-out-mark">{wanted[g.id] ? '✓' : ''}</span>
          </button>
        ))}
        <button className="btn solid sc-run-btn"
          disabled={empty || running || wantedIds.length === 0}
          title={empty ? 'Drag documents onto the canvas first'
            : wantedIds.length === 0 ? 'Select at least one output'
            : `Generate ${wantedIds.length} output${wantedIds.length === 1 ? '' : 's'} from ${items.length} input${items.length === 1 ? '' : 's'}`}
          onClick={() => onGenerate(wantedIds, prompt)}>
          <Icon name="sparkles" />
          {running ? 'Running…' : wantedIds.length === GENERATORS.length ? 'Run all' : `Generate (${wantedIds.length})`}
        </button>
      </div>

      {pipeline && (
        <ul className="sc-pipe">
          {pipeline.map((s) => (
            <li key={s.id} className={`sc-pipe-step ${s.status}`}>
              <span className="sc-pipe-dot">
                {s.status === 'done' ? <Icon name="check" />
                  : s.status === 'failed' ? <Icon name="x" />
                  : s.status === 'running' ? <span className="dw-capture-dot" /> : null}
              </span>
              <span className="sc-pipe-lbl">{s.label}</span>
              {s.note && <span className="sc-pipe-note">{s.note}</span>}
            </li>
          ))}
        </ul>
      )}
      {!running && lastResult && (
        <div className="sc-status sc-status-done">
          <Icon name="check-circle" />{lastResult.label}
          {lastResult.docId && onOpenResult && (
            <button className="sc-open" onClick={() => onOpenResult(lastResult.docId)}>open</button>
          )}
        </div>
      )}
    </section>
  );
}
