'use client';

import { use, useEffect, useState } from 'react';
import { useAuthedUser } from '../../lib/useAuthedUser';
import { apiGet } from '../../lib/api';
import CanvasApp from '../CanvasApp';
import AppHeader from '../AppHeader';
import CopilotPanel from '../CopilotPanel';
import PhaseTabs, { PHASES } from '../PhaseTabs';
import PreWorkshopDashboard from '../preworkshop/PreWorkshopDashboard';
import DuringWorkshopDashboard from '../duringworkshop/DuringWorkshopDashboard';
import { Icon } from '../../lib/icons';
import '../preworkshop/preworkshop.css';
import '../duringworkshop/duringworkshop.css';

// A Workshop IS the canvas board (see app/routes/projects.py +
// app/routes/canvas.py) — this page resolves :workshopId to its owning
// project (for the header breadcrumb) before handing off to either a
// phase dashboard (plain React pages, no canvas) or the ported
// prototype engine (CanvasApp -> canvasApp.js), same auth-gate pattern
// as every other top-level page (see useAuthedUser). Pre-Workshop and
// During Workshop are dashboards now; During Workshop additionally
// keeps a "Board view" toggle back to its canvas (explicit product
// decision — the canvas isn't retired for that phase, it's one toggle
// away). Post-Workshop/Proposal & Planning still render the canvas,
// scoped via `initialLens`.
export default function CanvasWorkshopPage({ params }) {
  const { workshopId } = use(params);
  const user = useAuthedUser();
  const [workshop, setWorkshop] = useState(null);
  const [error, setError] = useState(null);
  const [phase, setPhase] = useState('prepare');
  const [copilotOpen, setCopilotOpen] = useState(false);
  const [runBoardView, setRunBoardView] = useState(false);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await apiGet(`/api/workshops/${workshopId}`);
        if (cancelled) return;
        if (!data || !data.ok) {
          setError((data && data.error) || 'workshop not found');
          return;
        }
        setWorkshop(data.workshop);
      } catch (err) {
        if (!cancelled) setError(err.message || 'could not load this workshop');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [user, workshopId]);

  if (!user || (!workshop && !error)) {
    return (
      <main style={{ padding: 40, fontFamily: 'system-ui, sans-serif', color: '#6b7280', fontSize: 14 }}>
        Loading workshop…
      </main>
    );
  }

  if (error) {
    return (
      <main style={{ padding: 40, fontFamily: 'system-ui, sans-serif', fontSize: 14 }}>
        <p style={{ color: '#b91c1c', marginBottom: 12 }}>⚠ {error}</p>
        <a href="/projects" style={{ color: '#2563eb' }}>‹ Back to Projects</a>
      </main>
    );
  }

  const activePhase = PHASES.find((p) => p.id === phase);

  return (
    <div className="pw-shell">
      <AppHeader user={user} workshop={workshop} workshopId={Number(workshopId)}
        projectId={workshop.project_id} onOpenCopilot={() => setCopilotOpen(true)} />
      <PhaseTabs active={phase} onSelect={setPhase} />
      <CopilotPanel open={copilotOpen} onClose={() => setCopilotOpen(false)}
        workshopId={Number(workshopId)} zone={activePhase.label}
        contextName={workshop.project_name || workshop.name} />
      {phase === 'prepare' ? (
        <PreWorkshopDashboard
          user={user}
          workshopId={Number(workshopId)}
        />
      ) : phase === 'run' && !runBoardView ? (
        <DuringWorkshopDashboard
          user={user}
          workshopId={Number(workshopId)}
          onBoardView={() => setRunBoardView(true)}
        />
      ) : (
        <div className="pw-canvas-wrap">
          {phase === 'run' && (
            <button className="dw-back-dash" onClick={() => setRunBoardView(false)}
              title="Back to the During-Workshop dashboard">
              <Icon name="list" />Dashboard view
            </button>
          )}
          <CanvasApp
            key={phase}
            user={user}
            workshopId={Number(workshopId)}
            projectId={workshop.project_id}
            initialLens={phase}
          />
        </div>
      )}
    </div>
  );
}
