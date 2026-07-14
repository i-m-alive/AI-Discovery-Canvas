'use client';

import { useState } from 'react';
import { apiPatch } from '../lib/api';
import { Icon } from '../lib/icons';
import HeaderSearch from './HeaderSearch';

// The one shared identity bar across ALL 4 phases — dark, purple-accented
// chrome matching the reference product this rebuild is modeled on
// (search bar, engagement selector, notification/theme/Copilot icons).
// Sits ABOVE PhaseTabs (see [workshopId]/page.js); the canvas's own
// internal .menubar/.toolbar (File/Edit/View/..., board rename,
// search-canvas, zoom, REC/Teams/Present) is untouched and stays
// canvas-specific, nested below this.
//
// Notifications / theme toggle are deliberately STUBBED (disabled, with
// a tooltip saying so) — same honest-stub convention already used
// elsewhere in this app (canvas.css's Share/Vote buttons). Search and
// Copilot are real: HeaderSearch queries this workshop's documents by
// name AND content (⌘K to focus), and `onOpenCopilot` (passed by
// [workshopId]/page.js) opens CopilotPanel.jsx, a context-grounded
// assistant available on every phase.
export default function AppHeader({ user, workshop, workshopId, projectId, onOpenCopilot, onRenamed }) {
  const initials = ((user && (user.name || user.email)) || '?').trim().slice(0, 1).toUpperCase();
  const projectInitial = ((workshop && workshop.project_name) || 'P').trim().slice(0, 1).toUpperCase();

  const [editing, setEditing] = useState(false);
  const [projectDraft, setProjectDraft] = useState('');
  const [workshopDraft, setWorkshopDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [renameError, setRenameError] = useState('');

  function startEdit() {
    setProjectDraft(workshop.project_name || '');
    setWorkshopDraft(workshop.name || '');
    setRenameError('');
    setEditing(true);
  }

  function cancelEdit() {
    setEditing(false);
    setRenameError('');
  }

  // Only PATCHes whichever of project/engagement name actually changed —
  // this is the same PATCH /api/projects/<id> + PATCH /api/workshops/<id>
  // pair the rest of the app uses (see app/routes/projects.py).
  async function saveEdit() {
    const nextWorkshopName = workshopDraft.trim();
    const nextProjectName = projectDraft.trim();
    if (!nextWorkshopName) {
      setRenameError('Engagement name is required');
      return;
    }
    setSaving(true);
    setRenameError('');
    try {
      const patch = {};
      if (nextWorkshopName !== (workshop.name || '')) {
        const res = await apiPatch(`/api/workshops/${workshopId}`, { name: nextWorkshopName });
        if (!res || !res.ok) throw new Error((res && res.error) || 'could not rename the engagement');
        patch.name = res.workshop.name;
      }
      if (projectId && nextProjectName && nextProjectName !== (workshop.project_name || '')) {
        const res = await apiPatch(`/api/projects/${projectId}`, { name: nextProjectName });
        if (!res || !res.ok) throw new Error((res && res.error) || 'could not rename the project');
        patch.project_name = res.project.name;
      }
      if (Object.keys(patch).length > 0 && onRenamed) onRenamed(patch);
      setEditing(false);
    } catch (err) {
      setRenameError(err.message || 'could not save changes');
    } finally {
      setSaving(false);
    }
  }

  function onEditKeyDown(e) {
    if (e.key === 'Enter') { e.preventDefault(); saveEdit(); }
    else if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
  }

  return (
    <div className="pw-topbar">
      <div className="pw-brand">
        <span className="pw-brand-mark"><Icon name="sparkles" /></span>
        <div>
          <div className="pw-brand-name"><span>AI Discovery</span> Canvas</div>
          <div className="pw-brand-tag">ENGAGEMENT INTELLIGENCE</div>
        </div>
      </div>

      {workshop && (editing ? (
        <div className="pw-engagement pw-engagement-editing">
          <span className="pw-eng-avatar">{projectInitial}</span>
          <div className="pw-eng-txt">
            <input
              className="pw-eng-edit-input" value={projectDraft} onChange={(e) => setProjectDraft(e.target.value)}
              onKeyDown={onEditKeyDown} placeholder="Project name" disabled={saving} autoFocus
            />
            <input
              className="pw-eng-edit-input pw-eng-edit-input-sub" value={workshopDraft}
              onChange={(e) => setWorkshopDraft(e.target.value)} onKeyDown={onEditKeyDown}
              placeholder="Engagement name" disabled={saving}
            />
            {renameError && <div className="pw-eng-edit-error">{renameError}</div>}
          </div>
          <button className="pw-eng-edit-btn" onClick={saveEdit} disabled={saving} title="Save">
            <Icon name="check" />
          </button>
          <button className="pw-eng-edit-btn" onClick={cancelEdit} disabled={saving} title="Cancel">
            <Icon name="x" />
          </button>
        </div>
      ) : (
        <div className="pw-engagement">
          <a className="pw-engagement-link" href={projectId ? `/projects/${projectId}` : '/projects'}>
            <span className="pw-eng-avatar">{projectInitial}</span>
            <div className="pw-eng-txt">
              <div className="pw-eng-name">{workshop.project_name || 'Project'}</div>
              <div className="pw-eng-sub">{workshop.name || 'Untitled Engagement'}</div>
            </div>
            <span className="pw-eng-chevron"><Icon name="chevronDown" /></span>
          </a>
          <button className="pw-eng-edit-trigger" onClick={startEdit} title="Rename project / engagement">
            <Icon name="edit" />
          </button>
        </div>
      ))}

      <HeaderSearch workshopId={workshopId} />

      <div className="pw-topbar-actions">
        <button className="pw-icon-btn" disabled title="Notifications — not built yet">
          <Icon name="bell" /><span className="pw-dot" />
        </button>
        <button className="pw-icon-btn" disabled title="Theme toggle — not built yet"><Icon name="moon" /></button>
        <button className="pw-copilot-btn" onClick={onOpenCopilot} title="Ask Copilot — the assistant, grounded in this engagement">
          <Icon name="sparkles" />Copilot
        </button>
        {user && <div className="pw-avatar" title={user.name || user.email}>{initials}</div>}
      </div>
    </div>
  );
}
