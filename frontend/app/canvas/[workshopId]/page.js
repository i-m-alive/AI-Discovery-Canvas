'use client';

import { use, useEffect, useState } from 'react';
import { useAuthedUser } from '../../lib/useAuthedUser';
import { apiGet } from '../../lib/api';
import CanvasApp from '../CanvasApp';

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

  return <CanvasApp user={user} workshopId={Number(workshopId)} projectId={workshop.project_id} />;
}
