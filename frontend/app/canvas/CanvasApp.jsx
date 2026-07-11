'use client';

import { useEffect, useRef } from 'react';
import { initCanvasApp } from './canvasApp';
import './canvas.css';

// React owns ONLY this mount point. Everything inside the div is built
// and managed by the ported prototype engine (canvasApp.js) — vanilla
// DOM, exactly as the approved v0.5 prototype behaves. See the header
// comment in canvasApp.js for why this is deliberate and what changes
// in Phase 2.
export default function CanvasApp({ user, workshopId, projectId }) {
  const rootRef = useRef(null);
  const userRef = useRef(user);
  userRef.current = user;

  useEffect(() => {
    // user feeds artifact provenance ("approved by"); workshopId/projectId
    // scope every board/document/agent call server-side — see canvasApp.js.
    const cleanup = initCanvasApp(rootRef.current, { user: userRef.current, workshopId, projectId });
    return cleanup;
  }, [workshopId, projectId]);

  return <div ref={rootRef} className="aidc-canvas-root" />;
}
