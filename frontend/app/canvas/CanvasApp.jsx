'use client';

import { useEffect, useRef } from 'react';
import { initCanvasApp } from './canvasApp';
import './canvas.css';

// React owns ONLY this mount point. Everything inside the div is built
// and managed by the ported prototype engine (canvasApp.js) — vanilla
// DOM, exactly as the approved v0.5 prototype behaves. See the header
// comment in canvasApp.js for why this is deliberate and what changes
// in Phase 2.
export default function CanvasApp({ user, workshopId, projectId, initialLens }) {
  const rootRef = useRef(null);
  const userRef = useRef(user);
  userRef.current = user;

  useEffect(() => {
    // user feeds artifact provenance ("approved by"); workshopId/projectId
    // scope every board/document/agent call server-side — see canvasApp.js.
    // initialLens (a REGION id — see PhaseTabs.jsx) lets the phase-tab bar
    // open the canvas already scoped to whichever phase was clicked; the
    // caller must change this component's `key` when initialLens changes
    // (see [workshopId]/page.js) since this effect only reads it once, at
    // mount.
    const cleanup = initCanvasApp(rootRef.current, { user: userRef.current, workshopId, projectId, initialLens });
    return cleanup;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workshopId, projectId]);

  return <div ref={rootRef} className="aidc-canvas-root" />;
}
