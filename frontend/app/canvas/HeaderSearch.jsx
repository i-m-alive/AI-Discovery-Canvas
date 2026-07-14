'use client';

import { useEffect, useRef, useState } from 'react';
import { apiGet } from '../lib/api';
import { Icon } from '../lib/icons';
import DocumentViewer from './preworkshop/DocumentViewer';

// The AppHeader search bar, made real (it shipped as a disabled stub).
// One query, two matchers server-side (see routes/agents.py::search_artifacts):
// substring on document names, plus a semantic content match over the
// same workshop-scoped vector index Copilot grounds on — so "duplicate
// case handling" finds the right document even when no filename says so.
// ⌘K / Ctrl-K focuses it from anywhere; picking a result opens the
// document in the same viewer the dashboard uses.
export default function HeaderSearch({ workshopId }) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState([]);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [viewerDocId, setViewerDocId] = useState(null);
  const inputRef = useRef(null);
  const boxRef = useRef(null);
  const debounceRef = useRef(null);
  const seqRef = useRef(0);   // drop out-of-order responses

  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        inputRef.current?.focus();
      }
      if (e.key === 'Escape') setOpen(false);
    }
    function onClickAway(e) {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    }
    window.addEventListener('keydown', onKey);
    window.addEventListener('mousedown', onClickAway);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('mousedown', onClickAway);
    };
  }, []);

  function onChange(e) {
    const value = e.target.value;
    setQ(value);
    clearTimeout(debounceRef.current);
    if (value.trim().length < 2) {
      setResults([]);
      setOpen(false);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      const seq = ++seqRef.current;
      setSearching(true);
      try {
        const data = await apiGet(`/api/agents/search?workshop_id=${workshopId}&q=${encodeURIComponent(value.trim())}`);
        if (seq !== seqRef.current) return;   // a newer query superseded this one
        if (data && data.ok) {
          setResults(data.results || []);
          setOpen(true);
        }
      } catch { /* transient — the next keystroke retries */ } finally {
        if (seq === seqRef.current) setSearching(false);
      }
    }, 300);
  }

  function pick(r) {
    setOpen(false);
    setViewerDocId(r.doc_id);
  }

  return (
    <div className="pw-search pw-search-live" ref={boxRef}>
      <Icon name="search" />
      <input
        ref={inputRef}
        placeholder="Search artifacts, sources, content…"
        value={q}
        onChange={onChange}
        onFocus={() => { if (results.length) setOpen(true); }}
      />
      <span className="pw-kbd">⌘K</span>

      {open && (
        <div className="pw-search-drop">
          {searching && results.length === 0 && <div className="pw-search-note">Searching…</div>}
          {!searching && results.length === 0 && <div className="pw-search-note">No matches in this workshop.</div>}
          {results.map((r) => (
            <button key={r.doc_id} className="pw-search-item" onClick={() => pick(r)}>
              <span className={`pw-search-ic pw-search-ic-${r.origin}`}>
                <Icon name={r.origin === 'generated' ? 'sparkles' : 'doc-text'} />
              </span>
              <span className="pw-search-txt">
                <span className="pw-search-name">{r.name}</span>
                <span className="pw-search-sub">
                  {r.origin === 'generated' ? 'Generated artifact' : 'Source document'}
                  {r.match === 'content' && r.snippet ? ` — “${r.snippet}”` : ''}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}

      {viewerDocId && (
        <DocumentViewer workshopId={workshopId} docId={viewerDocId} onClose={() => setViewerDocId(null)} />
      )}
    </div>
  );
}
