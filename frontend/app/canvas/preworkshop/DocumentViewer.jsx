'use client';

import { useEffect, useMemo, useState } from 'react';
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
//
// Clickable citations: generated documents (Artifact Analyst answers,
// research briefs) cite their sources as [Doc Name] / [generated: Doc
// Name]. Any citation that resolves to a real document in this workshop
// becomes a chip that opens that document IN this viewer (with a Back
// button) — one-click verification of every claim. Citations that don't
// resolve stay plain text; nothing is invented client-side either.
function normalizeCiteLabel(s) {
  return (s || '').toLowerCase().replace(/^generated:\s*/, '').trim();
}

function buildResolver(index) {
  return (label) => {
    const norm = normalizeCiteLabel(label);
    if (norm.length < 4) return null;
    const exact = index.find((d) => d.norm === norm);
    if (exact) return exact.doc_id;
    // Citations sometimes truncate long names (or drop an extension) —
    // accept a prefix match either way, but only for labels long enough
    // that a false hit is implausible.
    if (norm.length >= 8) {
      const pref = index.find((d) => d.norm.startsWith(norm) || norm.startsWith(d.norm));
      if (pref) return pref.doc_id;
    }
    return null;
  };
}

function linkifyCitations(html, resolver) {
  return html.replace(/\[([^\[\]<>]{4,140})\]/g, (whole, label) => {
    const id = resolver(label);
    if (!id) return whole;
    return `<button type="button" class="pw-cite-link" data-doc="${id}" title="Open the cited document">${label}</button>`;
  });
}

export default function DocumentViewer({ workshopId, docId, onClose }) {
  const [state, setState] = useState({ loading: true });
  const [activeDocId, setActiveDocId] = useState(docId);
  const [backStack, setBackStack] = useState([]);
  const [docIndex, setDocIndex] = useState(null);   // [{doc_id, norm}] for citation resolution

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [src, gen] = await Promise.all([
          apiGet(`/api/agents/prepare-docs?workshop_id=${workshopId}`),
          apiGet(`/api/agents/generated-docs?workshop_id=${workshopId}`),
        ]);
        if (cancelled) return;
        const index = [];
        for (const d of (src && src.ok && src.docs) || []) {
          index.push({ doc_id: d.doc_id, norm: normalizeCiteLabel(d.name) });
        }
        for (const d of (gen && gen.ok && gen.docs) || []) {
          index.push({ doc_id: d.doc_id, norm: normalizeCiteLabel(d.name) });
        }
        setDocIndex(index);
      } catch { /* citations just stay plain text */ }
    })();
    return () => { cancelled = true; };
  }, [workshopId]);

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true });
    (async () => {
      try {
        const data = await apiGet(`/api/agents/document/${activeDocId}/view?workshop_id=${workshopId}`);
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
          if (!cancelled) setState({ loading: false, kind: 'html', name: data.name, html: result.value, origin: data.origin });
          return;
        }
        if (!cancelled) setState({ loading: false, ...data });
      } catch (err) {
        if (!cancelled) setState({ loading: false, error: err.message || 'could not render this document' });
      }
    })();
    return () => { cancelled = true; };
  }, [workshopId, activeDocId]);

  const displayHtml = useMemo(() => {
    if (state.kind !== 'html' || !state.html) return state.html;
    if (!docIndex || !docIndex.length) return state.html;
    return linkifyCitations(state.html, buildResolver(docIndex));
  }, [state.kind, state.html, docIndex]);

  function onBodyClick(e) {
    const link = e.target.closest && e.target.closest('.pw-cite-link');
    if (!link || !link.dataset.doc) return;
    setBackStack((s) => [...s, activeDocId]);
    setActiveDocId(link.dataset.doc);
  }

  function goBack() {
    setBackStack((s) => {
      if (!s.length) return s;
      const next = s.slice();
      setActiveDocId(next.pop());
      return next;
    });
  }

  return (
    <div className="pw-modal-backdrop" onClick={onClose}>
      <div className="pw-modal" onClick={(e) => e.stopPropagation()}>
        <div className="pw-modal-head">
          {backStack.length > 0 && (
            <button className="btn" onClick={goBack} title="Back to the citing document">
              <Icon name="caretL" />Back
            </button>
          )}
          <span className="pw-modal-title">{state.name || 'Document'}</span>
          {state.kind === 'pdf' && (
            <a className="btn" href={state.file_url} target="_blank" rel="noopener noreferrer">
              <Icon name="upload" />Open in new tab
            </a>
          )}
          {state.origin === 'generated' && (
            <a className="btn" href={`/api/agents/document/${activeDocId}/word?workshop_id=${workshopId}`} download>
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
            <div className="pw-doc-html" onClick={onBodyClick} dangerouslySetInnerHTML={{ __html: displayHtml }} />
          )}
          {state.kind === 'text' && (
            <pre className="pw-doc-text">{state.text || '(no extracted text)'}</pre>
          )}
        </div>
      </div>
    </div>
  );
}
