'use client';

import { useState } from 'react';
import { apiPatch } from '../../lib/api';
import { Icon } from '../../lib/icons';

// The SOW milestone timeline — evenly-spaced flag nodes on a connector
// line, week + title under each (matching the reference design; even
// spacing, not week-proportional: 5 labels crammed proportionally
// collide long before the visual gains anything). Inline edit: click a
// milestone to change its week/title; PATCH /api/proposal/<doc_id>
// re-clamps server-side through the same coercer generation uses.

export default function MilestoneTimeline({ workshopId, sow, onChanged }) {
  const [editing, setEditing] = useState(null);   // {idx, week, title}
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const milestones = (sow && sow.milestones) || [];
  if (!milestones.length) return null;

  async function save() {
    if (!editing || !editing.title.trim()) return;
    setBusy(true); setErr('');
    const next = milestones.map((m, i) =>
      i === editing.idx ? { week: Number(editing.week) || m.week, title: editing.title } : m);
    try {
      const res = await apiPatch(`/api/proposal/${sow.doc_id}`, {
        workshop_id: workshopId, milestones: next,
      });
      if (!res.ok) { setErr(res.error || 'could not save'); return; }
      setEditing(null);
      onChanged();
    } catch (e) { setErr(e.message || 'could not save'); } finally { setBusy(false); }
  }

  async function removeOne(idx) {
    if (milestones.length <= 1) return;   // an empty timeline can't render — regenerate instead
    setErr('');
    try {
      const res = await apiPatch(`/api/proposal/${sow.doc_id}`, {
        workshop_id: workshopId, milestones: milestones.filter((_, i) => i !== idx),
      });
      if (!res.ok) { setErr(res.error || 'could not save'); return; }
      onChanged();
    } catch (e) { setErr(e.message || 'could not save'); }
  }

  return (
    <div className="pp-timeline-wrap">
      {err && <div className="app-error pw-err">⚠ {err}</div>}
      <div className="pp-timeline">
        {milestones.map((m, i) => (
          <div key={`${m.week}-${m.title}`} className="pp-ms">
            <div className="pp-ms-track">
              {i > 0 && <span className="pp-ms-line pp-ms-line-l" />}
              <span className="pp-ms-node"><Icon name="target" /></span>
              {i < milestones.length - 1 && <span className="pp-ms-line pp-ms-line-r" />}
            </div>
            {editing && editing.idx === i ? (
              <div className="pp-ms-edit">
                <input type="number" min="1" max="104" value={editing.week}
                  onChange={(e) => setEditing({ ...editing, week: e.target.value })} />
                <input type="text" value={editing.title} autoFocus
                  onChange={(e) => setEditing({ ...editing, title: e.target.value })} />
                <div className="pp-ms-edit-row">
                  <button className="btn solid" onClick={save} disabled={busy || !editing.title.trim()}>
                    {busy ? '…' : 'Save'}
                  </button>
                  <button className="btn" onClick={() => setEditing(null)} disabled={busy}>Cancel</button>
                </div>
              </div>
            ) : (
              <button className="pp-ms-body" title="Edit this milestone"
                onClick={() => setEditing({ idx: i, week: m.week, title: m.title })}>
                <span className="pp-ms-week">Wk {m.week}</span>
                <span className="pp-ms-title">{m.title}</span>
              </button>
            )}
            {milestones.length > 1 && !(editing && editing.idx === i) && (
              <button className="pw-view-btn pw-del-btn pp-ms-del" title="Remove milestone"
                onClick={() => removeOne(i)}>
                <Icon name="x" />
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
