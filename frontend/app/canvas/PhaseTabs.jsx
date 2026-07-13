'use client';

// The 4-phase engagement-lifecycle tab bar (see agent_catalog.py's zone
// relabel + canvasApp.js's REGIONS for the same 4 phases/labels/order).
// `id` matches the canvas engine's region id exactly (prepare/run/
// synthesize/project) so selecting a canvas-backed tab can be passed
// straight through as CanvasApp's `initialLens`.
export const PHASES = [
  { id: 'prepare', label: 'Pre-Workshop', sub: 'Ingest · Internalize · Research', dashboard: true },
  { id: 'run', label: 'During Workshop', sub: 'Capture · Synthesize · Generate', dashboard: false },
  { id: 'synthesize', label: 'Post-Workshop', sub: 'Backlog · Opportunities · MoM', dashboard: false },
  { id: 'project', label: 'Proposal & Planning', sub: 'SOW · ROI · Risk · Team', dashboard: false },
];

export default function PhaseTabs({ active, onSelect }) {
  const activeIdx = PHASES.findIndex((p) => p.id === active);
  return (
    <div className="phasetabs">
      <div className="pw-phase-lbl">
        <div className="pw-phase-eyebrow">ENGAGEMENT</div>
        <div className="pw-phase-of">Phase {activeIdx + 1} of {PHASES.length}</div>
      </div>
      {PHASES.map((p, i) => (
        <button
          key={p.id}
          className={'ptab' + (p.id === active ? ' on' : '') + (i < activeIdx ? ' done' : '')}
          onClick={() => onSelect(p.id)}
          title={p.label}
        >
          <span className="pnum">{i < activeIdx ? '✓' : i + 1}</span>
          <span className="ptxt">
            <span className="plabel">{p.label}</span>
            <span className="psub">{p.sub}</span>
          </span>
        </button>
      ))}
    </div>
  );
}
