'use client';

import { useEffect, useRef } from 'react';
import { Icon } from '../../lib/icons';

// Inline draw.io viewer/editor for a workflow diagram — same
// embed.diagrams.net JSON-protocol integration canvasApp.js's
// openDrawioEditor already uses for the During Workshop 'drawflow' agent,
// so the Pre-Workshop 'workflow'/'deepresearch' diagrams open in the same
// real editor instead of only ever downloading a .drawio file the
// facilitator has to open elsewhere.
function slugify(name) {
  return (name || 'workflow').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'workflow';
}

export default function DrawioViewer({ xml, title, onClose }) {
  const frameRef = useRef(null);
  const label = title || 'Workflow diagram';

  useEffect(() => {
    function onMsg(ev) {
      if (!frameRef.current || ev.source !== frameRef.current.contentWindow) return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.event === 'init') {
        frameRef.current.contentWindow.postMessage(JSON.stringify({ action: 'load', xml, autosave: 0 }), '*');
      } else if (msg.event === 'exit') {
        onClose();
      }
    }
    window.addEventListener('message', onMsg);
    return () => window.removeEventListener('message', onMsg);
  }, [xml, onClose]);

  function download() {
    const blob = new Blob([xml], { type: 'application/xml' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${slugify(label)}.drawio`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="pw-modal-backdrop" onClick={onClose}>
      <div className="pw-modal pw-modal-drawio" onClick={(e) => e.stopPropagation()}>
        <div className="pw-modal-head">
          <div>
            <span className="pw-modal-title">{label}</span>
            <div className="pw-drawio-subtitle">Rendered in embedded draw.io — fully editable</div>
          </div>
          <button className="pw-modal-close" onClick={onClose} title="Close"><Icon name="x" /></button>
        </div>
        <div className="pw-drawio-filebar">
          <Icon name="flow" />
          <span className="pw-drawio-filename">{slugify(label)}.drawio</span>
          <span className="pw-tag pw-drawio-ai-tag">AI-generated</span>
          <button className="btn" onClick={download} style={{ marginLeft: 'auto' }}>
            <Icon name="upload" />Export
          </button>
        </div>
        <iframe
          ref={frameRef}
          className="pw-drawio-frame"
          title={label}
          src="https://embed.diagrams.net/?embed=1&ui=atlas&spin=1&proto=json"
        />
      </div>
    </div>
  );
}
