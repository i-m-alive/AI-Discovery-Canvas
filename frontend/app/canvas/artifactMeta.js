// Shared artifact metadata helpers — one source of truth for how a
// source doc or generated artifact is labeled, badged and iconed,
// used by the ArtifactExplorer sidebar, both phase dashboards, and the
// artifacts grid. Extracted from PreWorkshopDashboard so the explorer
// doesn't have to import a page component for a lookup table.

export const STATUS_LABEL = { queued: 'Queued', parsing: 'Parsing', ingested: 'Ingested', failed: 'Failed' };

export function timeAgo(unixSeconds) {
  if (!unixSeconds) return '';
  const diff = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  return `${Math.floor(diff / 86400)} d ago`;
}

function extOf(name) {
  const m = /\.([a-z0-9]+)$/i.exec(name || '');
  return m ? m[1].toLowerCase() : '';
}

// One badge label + icon + accent color per file type.
const FILE_TYPE = {
  pdf: { label: 'PDF', icon: 'doc-text', bg: '#fbecea', fg: '#c0463b' },
  docx: { label: 'DOCX', icon: 'doc-text', bg: '#eaf2fb', fg: '#2f6fb3' },
  doc: { label: 'DOC', icon: 'doc-text', bg: '#eaf2fb', fg: '#2f6fb3' },
  xlsx: { label: 'XLSX', icon: 'list', bg: '#eaf6f0', fg: '#2f8f5b' },
  xls: { label: 'XLS', icon: 'list', bg: '#eaf6f0', fg: '#2f8f5b' },
  csv: { label: 'CSV', icon: 'list', bg: '#eaf6f0', fg: '#2f8f5b' },
  pptx: { label: 'PPTX', icon: 'flow', bg: '#faf1e1', fg: '#c9881f' },
  ppt: { label: 'PPT', icon: 'flow', bg: '#faf1e1', fg: '#c9881f' },
  txt: { label: 'TXT', icon: 'doc-text', bg: '#eef1f5', fg: '#6b7280' },
  md: { label: 'MD', icon: 'doc-text', bg: '#eef1f5', fg: '#6b7280' },
  html: { label: 'HTML', icon: 'doc-text', bg: '#efedfd', fg: '#6d5ce8' },
  zip: { label: 'ZIP', icon: 'folder', bg: '#efedfd', fg: '#6d5ce8' },
};

export function isTranscript(name) {
  return /^teams\s*—|^teams\s*--|\.vtt$|transcript/i.test(name || '');
}

export function fileType(name) {
  // Imported meeting transcripts (named 'Teams — {subject}' by the
  // import-transcript route, mirroring the backend's _is_transcript)
  // get their own source-type identity, not a generic FILE badge.
  if (isTranscript(name)) {
    return { label: 'TRANSCRIPT', icon: 'users', bg: '#efedfd', fg: '#6d5ce8' };
  }
  const ext = extOf(name);
  return FILE_TYPE[ext] || { label: ext ? ext.toUpperCase() : 'FILE', icon: 'doc-text', bg: '#eef1f5', fg: '#6b7280' };
}

// Which icon a generated artifact renders with, by producing agent.
export function agentIcon(agentId) {
  if (agentId === 'workflow' || agentId === 'drawflow') return 'flow';
  if (agentId === 'summarize_docs' || agentId === 'artifact_analyst' || agentId === 'brd') return 'doc-text';
  if (agentId === 'analyze' || agentId === 'capmap') return 'target';
  return 'search';
}

export function downloadDrawio(diagram, name) {
  if (!diagram || !diagram.xml) return;
  const blob = new Blob([diagram.xml], { type: 'application/xml' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${(name || 'workflow').replace(/[^a-z0-9-_]+/gi, '_')}.drawio`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
