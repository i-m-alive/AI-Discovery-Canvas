'use client';

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
export default function AppHeader({ user, workshop, workshopId, projectId, onOpenCopilot }) {
  const initials = ((user && (user.name || user.email)) || '?').trim().slice(0, 1).toUpperCase();
  const projectInitial = ((workshop && workshop.project_name) || 'P').trim().slice(0, 1).toUpperCase();

  return (
    <div className="pw-topbar">
      <div className="pw-brand">
        <span className="pw-brand-mark"><Icon name="sparkles" /></span>
        <div>
          <div className="pw-brand-name"><span>AI Discovery</span> Canvas</div>
          <div className="pw-brand-tag">ENGAGEMENT INTELLIGENCE</div>
        </div>
      </div>

      {workshop && (
        <a className="pw-engagement" href={projectId ? `/projects/${projectId}` : '/projects'}>
          <span className="pw-eng-avatar">{projectInitial}</span>
          <div className="pw-eng-txt">
            <div className="pw-eng-name">{workshop.project_name || 'Project'}</div>
            <div className="pw-eng-sub">{workshop.name || 'Untitled Engagement'}</div>
          </div>
          <span className="pw-eng-chevron"><Icon name="chevronDown" /></span>
        </a>
      )}

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
