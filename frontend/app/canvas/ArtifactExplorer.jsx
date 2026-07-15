'use client';

import { useEffect, useMemo, useState } from 'react';
import { apiGet } from '../lib/api';
import { Icon } from '../lib/icons';
import { STATUS_LABEL, agentIcon, fileType, isTranscript } from './artifactMeta';

// The left-hand Artifact Explorer — the engagement's table of contents.
// Two-level collapsible tree over everything the workshop holds:
//
//   SOURCES              (uploads + imported transcripts, phase-agnostic)
//     Transcripts / Documents
//   PRE-WORKSHOP         (generated artifacts, grouped by category)
//   DURING WORKSHOP
//   POST-WORKSHOP        (dimmed while empty — a roadmap cue, not noise)
//   PROPOSAL & PLANNING
//
// Outer grouping = the producing agent's `zone` (already on every
// generated-doc row); inner grouping = its `category`. Zero backend
// work — this is a pure re-projection of the two lists both dashboards
// already fetch. Expansion state persists per workshop in localStorage;
// the active phase starts expanded.

const PHASES = [
  { key: 'Pre-Workshop', dot: '#2f8f5b' },
  { key: 'During Workshop', dot: '#6d5ce8' },
  { key: 'Post-Workshop', dot: '#c9881f' },
  { key: 'Proposal & Planning', dot: '#1d968f' },
];

function loadExpanded(workshopId, activePhase) {
  try {
    const raw = window.localStorage.getItem(`aidc-explorer-${workshopId}`);
    if (raw) return JSON.parse(raw);
  } catch { /* fresh default below */ }
  return { sources: true, [activePhase]: true };
}

// Drag payload MIME type — matched by SynthesisCanvas's dropzone.
const DRAG_TYPE = 'application/x-aidc-artifact';

export default function ArtifactExplorer({
  workshopId, docs, artifacts, activePhase,
  onAdd, extraAction,
  onView,                 // (docId) -> open DocumentViewer
  onOpenDiagram,          // ({xml, title}) -> open diagram viewer/editor
  onOpenAnalysis,         // ({name, analysis, docId}) -> scorecard modal
  onOpenCapmap,           // ({name, domains, version}) -> heat-map modal
  onDelete,               // (docId, name) -> confirm-delete dialog
  onAddToCanvas,          // (item {kind, doc_id, name, agent_id?}) -> Synthesis Canvas
}) {
  const [expanded, setExpanded] = useState(() =>
    typeof window === 'undefined' ? { sources: true, [activePhase]: true }
      : loadExpanded(workshopId, activePhase));
  const [filter, setFilter] = useState('');
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    try { window.localStorage.setItem(`aidc-explorer-${workshopId}`, JSON.stringify(expanded)); }
    catch { /* private mode etc. — expansion just won't persist */ }
  }, [expanded, workshopId]);

  const q = filter.trim().toLowerCase();
  const filtering = q.length > 0;
  const matches = (name) => !filtering || (name || '').toLowerCase().includes(q);

  function toggle(key) {
    setExpanded((e) => ({ ...e, [key]: !isOpen(key) }));
  }
  function isOpen(key) {
    if (filtering) return true;             // a search always shows its hits
    return !!expanded[key];
  }

  // ── source grouping ─────────────────────────────────────────────────
  const srcTranscripts = docs.filter((d) => isTranscript(d.name) && matches(d.name));
  const srcDocuments = docs.filter((d) => !isTranscript(d.name) && matches(d.name));
  const srcCount = srcTranscripts.length + srcDocuments.length;

  // ── generated grouping: zone -> category -> items ───────────────────
  const byPhase = useMemo(() => {
    const out = Object.fromEntries(PHASES.map((p) => [p.key, {}]));
    artifacts.forEach((a) => {
      const zone = out[a.zone] ? a.zone : 'Pre-Workshop';   // legacy rows had no zone
      const cat = a.category || a.agent_id || 'Other';
      (out[zone][cat] = out[zone][cat] || []).push(a);
    });
    return out;
  }, [artifacts]);

  async function openDiagram(a) {
    try {
      const d = await apiGet(`/api/agents/document/${a.doc_id}/diagram?workshop_id=${workshopId}`);
      if (d && d.ok && onOpenDiagram) onOpenDiagram({ xml: d.xml, diagrams: d.diagrams, title: a.name });
    } catch { /* no diagram, or transient */ }
  }
  async function openAnalysis(a) {
    try {
      const d = await apiGet(`/api/agents/document/${a.doc_id}/analysis?workshop_id=${workshopId}`);
      if (d && d.ok && onOpenAnalysis) {
        onOpenAnalysis({ name: a.name, docId: a.doc_id,
          analysis: { gaps: d.gaps || [], readiness: d.readiness || [], research_topics: d.research_topics || [] } });
      }
    } catch { /* no analysis, or transient */ }
  }
  async function openCapmap(a) {
    try {
      const d = await apiGet(`/api/agents/document/${a.doc_id}/capmap?workshop_id=${workshopId}`);
      if (d && d.ok && onOpenCapmap) onOpenCapmap({ name: a.name, domains: d.domains || [], version: d.version });
    } catch { /* no capmap, or transient */ }
  }

  function dragProps(item) {
    return {
      draggable: true,
      onDragStart: (e) => {
        e.dataTransfer.setData(DRAG_TYPE, JSON.stringify(item));
        e.dataTransfer.effectAllowed = 'copy';
      },
    };
  }

  function SourceRow({ d }) {
    const ft = fileType(d.name);
    const item = { kind: 'source', doc_id: d.doc_id, name: d.name };
    return (
      <li className="ax-row" {...dragProps(item)}>
        <span className="ax-row-ic" style={{ background: ft.bg, color: ft.fg }}><Icon name={ft.icon} /></span>
        <button className="ax-row-name" onClick={() => onView && onView(d.doc_id)} title={d.name}>{d.name}</button>
        <span className={`pw-pill pw-pill-${d.status} ax-pill`}>{STATUS_LABEL[d.status] || d.status}</span>
        <span className="ax-row-tools">
          {onAddToCanvas && (
            <button className="pw-view-btn ax-canvas-btn" onClick={() => onAddToCanvas(item)}
              title="Add to the Synthesis Canvas"><Icon name="plus" /></button>
          )}
          <button className="pw-view-btn" onClick={() => onView && onView(d.doc_id)} title="View"><Icon name="search" /></button>
          {onDelete && (
            <button className="pw-view-btn pw-del-btn" onClick={() => onDelete(d.doc_id, d.name)} title="Delete"><Icon name="trash" /></button>
          )}
        </span>
      </li>
    );
  }

  function ArtifactRow({ a }) {
    const item = { kind: 'generated', doc_id: a.doc_id, name: a.name, agent_id: a.agent_id };
    return (
      <li className="ax-row" {...dragProps(item)}>
        <span className="ax-row-ic ax-ic-gen"><Icon name={agentIcon(a.agent_id)} /></span>
        <button className="ax-row-name" onClick={() => onView && onView(a.doc_id)} title={a.name}>{a.name}</button>
        <span className={`pw-pill pw-pill-${a.status} ax-pill`}>
          {a.status === 'final' ? 'Final' : a.status === 'in_review' ? 'In review' : 'Draft'}
        </span>
        <span className="ax-row-tools">
          {onAddToCanvas && (
            <button className="pw-view-btn ax-canvas-btn" onClick={() => onAddToCanvas(item)}
              title="Add to the Synthesis Canvas"><Icon name="plus" /></button>
          )}
          {a.has_diagram && onOpenDiagram && (
            <button className="pw-view-btn" onClick={() => openDiagram(a)} title="View diagram"><Icon name="flow" /></button>
          )}
          {a.has_analysis && onOpenAnalysis && (
            <button className="pw-view-btn" onClick={() => openAnalysis(a)} title="Readiness scorecard"><Icon name="target" /></button>
          )}
          {a.has_capmap && onOpenCapmap && (
            <button className="pw-view-btn" onClick={() => openCapmap(a)} title="Capability heat map"><Icon name="target" /></button>
          )}
          <a className="pw-view-btn" href={`/api/agents/document/${a.doc_id}/word?workshop_id=${workshopId}`}
            download title="Download as Word"><Icon name="upload" /></a>
          {onDelete && (
            <button className="pw-view-btn pw-del-btn" onClick={() => onDelete(a.doc_id, a.name)} title="Delete"><Icon name="trash" /></button>
          )}
        </span>
      </li>
    );
  }

  function SubGroup({ label, items, children }) {
    if (!items.length) return null;
    return (
      <div className="ax-sub">
        <div className="ax-sub-hd">{label}<span className="ax-count">{items.length}</span></div>
        <ul className="ax-list">{children}</ul>
      </div>
    );
  }

  return (
    <div className={'pw-sources-wrap' + (collapsed ? ' collapsed' : '')}>
      <button className={'pw-collapse-btn' + (collapsed ? ' flipped' : '')} onClick={() => setCollapsed((v) => !v)}
        title={collapsed ? 'Expand Artifacts' : 'Collapse Artifacts'}>
        <Icon name="caretL" />
      </button>
      <section className={'pw-sources ax' + (collapsed ? ' collapsed' : '')}>
      <div className="pw-panel-head">
        {!collapsed && (
          <div className="pw-panel-ttl">
            <span className="pw-ic pw-ic-accent"><Icon name="folder" /></span>
            <div>
              <div className="pw-h3">Artifacts</div>
              <div className="pw-sub">{docs.length} source{docs.length === 1 ? '' : 's'} · {artifacts.length} generated</div>
            </div>
          </div>
        )}
        {collapsed && <span className="pw-ic pw-ic-accent"><Icon name="folder" /></span>}
        {!collapsed && onAdd && <button className="btn" onClick={onAdd}><Icon name="plus" />Add</button>}
      </div>
      {!collapsed && (
        <>
      {extraAction}
      {onAdd && (
        <div className="pw-dropzone ax-drop" onClick={onAdd}>
          <Icon name="upload" />
          <div className="pw-dz-txt">Drop docs, PDFs, notes, transcripts</div>
        </div>
      )}

      <div className="ax-filter">
        <Icon name="search" />
        <input value={filter} onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter artifacts…" aria-label="Filter artifacts by name" />
        {filtering && (
          <button className="ax-filter-x" onClick={() => setFilter('')} title="Clear"><Icon name="x" /></button>
        )}
      </div>

      <div className="ax-tree">
        {/* Sources — pinned first: they're the inputs every phase shares */}
        {(!filtering || srcCount > 0) && (
          <div className="ax-group">
            <button className="ax-group-hd" onClick={() => toggle('sources')} aria-expanded={isOpen('sources')}>
              <span className={'ax-chev' + (isOpen('sources') ? ' open' : '')}>▸</span>
              <span className="ax-dot" style={{ background: '#8a91a8' }} />
              Sources<span className="ax-count">{srcCount}</span>
            </button>
            {isOpen('sources') && (
              srcCount === 0 ? (
                <div className="ax-empty">No sources yet — upload a document or import a transcript.</div>
              ) : (
                <>
                  <SubGroup label="Transcripts" items={srcTranscripts}>
                    {srcTranscripts.map((d) => <SourceRow key={d.doc_id} d={d} />)}
                  </SubGroup>
                  <SubGroup label="Documents" items={srcDocuments}>
                    {srcDocuments.map((d) => <SourceRow key={d.doc_id} d={d} />)}
                  </SubGroup>
                </>
              )
            )}
          </div>
        )}

        {PHASES.map((p) => {
          const cats = byPhase[p.key];
          const catNames = Object.keys(cats).sort();
          const visible = catNames.map((c) => [c, cats[c].filter((a) => matches(a.name))])
            .filter(([, items]) => items.length > 0);
          const total = visible.reduce((n, [, items]) => n + items.length, 0);
          if (filtering && total === 0) return null;
          const empty = catNames.length === 0;
          return (
            <div key={p.key} className={'ax-group' + (empty ? ' ax-dim' : '')}>
              <button className="ax-group-hd" onClick={() => !empty && toggle(p.key)}
                aria-expanded={isOpen(p.key)} disabled={empty}
                title={empty ? `Nothing generated in ${p.key} yet` : undefined}>
                <span className={'ax-chev' + (isOpen(p.key) && !empty ? ' open' : '')}>▸</span>
                <span className="ax-dot" style={{ background: p.dot }} />
                {p.key}<span className="ax-count">{filtering ? total : catNames.reduce((n, c) => n + cats[c].length, 0)}</span>
              </button>
              {!empty && isOpen(p.key) && visible.map(([cat, items]) => (
                <SubGroup key={cat} label={cat} items={items}>
                  {items.map((a) => <ArtifactRow key={a.doc_id} a={a} />)}
                </SubGroup>
              ))}
            </div>
          );
        })}
      </div>
        </>
      )}
      </section>
    </div>
  );
}
