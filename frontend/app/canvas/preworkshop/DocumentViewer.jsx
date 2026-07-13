'use client';

import { useEffect, useState } from 'react';
import { apiGet } from '../../lib/api';
import { Icon } from '../../lib/icons';

// Modal that renders a document in its actual format, not just extracted
// text: PDF via the browser's native renderer, DOCX via mammoth.js
// (client-side, dynamically imported so it never loads unless a .docx is
// actually opened), XLSX as a server-rendered HTML table (see
// backend/app/services/rag/file_extractor.py::xlsx_to_html — deliberately
// NOT a client-side xlsx parser; the well-known npm 'xlsx'/SheetJS
// package has unpatched prototype-pollution/ReDoS advisories, a real risk
// for a feature whose job is parsing files a client handed you), and
// generated docs (research briefs, workflow output) as their own
// formatted HTML. Anything else (PPTX, and any format with no real
// renderer) falls back to the extracted plain text — still useful, never
// a dead end.
export default function DocumentViewer({ workshopId, docId, onClose }) {
  const [state, setState] = useState({ loading: true });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true });
    (async () => {
      try {
        const data = await apiGet(`/api/agents/document/${docId}/view?workshop_id=${workshopId}`);
        if (cancelled) return;
        if (!data || !data.ok) {
          setState({ loading: false, error: (data && data.error) || 'could not load this document' });
          return;
        }
        if (data.kind === 'docx') {
          const res = await fetch(data.file_url, { credentials: 'same-origin' });
          if (!res.ok) throw new Error('could not download the original file');
          const buf = await res.arrayBuffer();
          const mammoth = (await import('mammoth')).default || (await import('mammoth'));
          const result = await mammoth.convertToHtml({ arrayBuffer: buf });
          if (!cancelled) setState({ loading: false, kind: 'html', name: data.name, html: result.value });
          return;
        }
        if (!cancelled) setState({ loading: false, ...data });
      } catch (err) {
        if (!cancelled) setState({ loading: false, error: err.message || 'could not render this document' });
      }
    })();
    return () => { cancelled = true; };
  }, [workshopId, docId]);

  return (
    <div className="pw-modal-backdrop" onClick={onClose}>
      <div className="pw-modal" onClick={(e) => e.stopPropagation()}>
        <div className="pw-modal-head">
          <span className="pw-modal-title">{state.name || 'Document'}</span>
          {state.kind === 'pdf' && (
            <a className="btn" href={state.file_url} target="_blank" rel="noopener noreferrer">
              <Icon name="upload" />Open in new tab
            </a>
          )}
          {state.origin === 'generated' && (
            <a className="btn" href={`/api/agents/document/${docId}/word?workshop_id=${workshopId}`} download>
              <Icon name="upload" />Download as Word
            </a>
          )}
          <button className="pw-modal-close" onClick={onClose} title="Close"><Icon name="x" /></button>
        </div>
        <div className="pw-modal-body">
          {state.loading && <div className="pw-empty">Loading preview…</div>}
          {state.error && <div className="app-error">⚠ {state.error}</div>}
          {state.kind === 'pdf' && (
            <iframe src={state.file_url} title={state.name} className="pw-pdf-frame" />
          )}
          {state.kind === 'html' && (
            <div className="pw-doc-html" dangerouslySetInnerHTML={{ __html: state.html }} />
          )}
          {state.kind === 'text' && (
            <pre className="pw-doc-text">{state.text || '(no extracted text)'}</pre>
          )}
        </div>
      </div>
    </div>
  );
}
