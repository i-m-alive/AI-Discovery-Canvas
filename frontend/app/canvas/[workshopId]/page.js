'use client';

import { use, useEffect, useState } from 'react';
import { useAuthedUser } from '../../lib/useAuthedUser';
import { apiGet } from '../../lib/api';
import CanvasApp from '../CanvasApp';
import AppHeader from '../AppHeader';
import CopilotPanel from '../CopilotPanel';
import PhaseTabs, { PHASES } from '../PhaseTabs';
import PreWorkshopDashboard from '../preworkshop/PreWorkshopDashboard';
import '../preworkshop/preworkshop.css';

// A Workshop IS the canvas board (see app/routes/projects.py +
// app/routes/canvas.py) — this page resolves :workshopId to its owning
// project (for the header breadcrumb) before handing off to either the
// Pre-Workshop dashboard (a plain React page, no canvas) or the ported
// prototype engine (CanvasApp -> canvasApp.js) for the other 3 phases,
// same auth-gate pattern as every other top-level page (see
// useAuthedUser). Only Pre-Workshop has been rebuilt as a dashboard so
// far — During Workshop/Post-Workshop/Proposal & Planning still render
// the existing canvas, scoped to that phase's region via `initialLens`.
export default function CanvasWorkshopPage({ params }) {
  const { workshopId } = use(params);
  const user = useAuthedUser();
  const [workshop, setWorkshop] = useState(null);
  const [error, setError] = useState(null);
  const [phase, setPhase] = useState('prepare');
  const [copilotOpen, setCopilotOpen] = useState(false);

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
      {activePhase.dashboard ? (
        <PreWorkshopDashboard
          user={user}
          workshopId={Number(workshopId)}
        />
      ) : (
        <div className="pw-canvas-wrap">
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
