'use client';

import { useState } from 'react';
import DiagramCanvas from '../duringworkshop/DiagramCanvas';
import DrawioViewer from './DrawioViewer';

// "View diagram" everywhere now opens the NATIVE swimlane viewer (the
// same DiagramCanvas the During-Workshop Process Flow panel uses) —
// instant, styled, zoom/pan/fullscreen — with the draw.io editor one
// click away ("Edit in draw.io" / any tool-rail button flips to it).
// Falls back to the raw draw.io modal when only XML exists (old
// artifacts persisted before diagram JSON was stored).
export default function DiagramModal({ xml, diagrams, title, onClose }) {
  const [editing, setEditing] = useState(false);

  if (editing) {
    return <DrawioViewer xml={xml} title={title} onClose={() => setEditing(false)} />;
  }
  if (!diagrams || !diagrams.length) {
    return <DrawioViewer xml={xml} title={title} onClose={onClose} />;
  }
  return (
    <div className="pw-modal-backdrop" onClick={onClose}>
      <div className="dw-diagram-modal" onClick={(e) => e.stopPropagation()}>
        <DiagramCanvas diagrams={diagrams} title={title} xml={xml}
          onEdit={() => setEditing(true)} onClose={onClose} />
      </div>
    </div>
  );
}
