'use client';

import { use, useEffect, useState } from 'react';
import { useAuthedUser } from '../../lib/useAuthedUser';
import { apiGet } from '../../lib/api';
import OrbitzApp from '../OrbitzApp';

// A Workshop IS the canvas board (see app/routes/projects.py +
// app/routes/canvas.py) — this page resolves :workshopId to its owning
// project (for the header breadcrumb) before handing off to the ported
// prototype engine (CanvasApp -> canvasApp.js), same auth-gate pattern
// as every other top-level page (see useAuthedUser).
export default function CanvasWorkshopPage({ params }) {
  const { workshopId } = use(params);
  const user = useAuthedUser();
  const [workshop, setWorkshop] = useState(null);
  const [error, setError] = useState(null);

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

  const shellStyle = {
    minHeight: '100vh', padding: 40, fontFamily: '"Inter", system-ui, sans-serif',
    fontSize: 14, background: '#080b14', color: '#94a3b8',
  };

  if (!user || (!workshop && !error)) {
    return <main style={shellStyle}>Loading workshop…</main>;
  }

  if (error) {
    return (
      <main style={shellStyle}>
        <p style={{ color: '#f87171', marginBottom: 12 }}>⚠ {error}</p>
        <a href="/projects" style={{ color: '#8178ff' }}>‹ Back to Projects</a>
      </main>
    );
  }

  return (
    <OrbitzApp
      user={user}
      workshopId={Number(workshopId)}
      projectId={workshop.project_id}
      workshopName={workshop.name}
    />
  );
}
