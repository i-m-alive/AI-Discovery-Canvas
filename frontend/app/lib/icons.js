// Same line-icon set/style as app/canvas/canvasApp.js's inline ICON
// object (24x24 viewBox, stroke=currentColor, stroke-width 1.8) — kept
// as a small standalone React component here since canvasApp.js is
// vanilla JS, not something these React "app shell" pages can import
// from directly. Only the subset actually used outside the canvas.

const PATHS = {
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="0.7"/>',
  'doc-text': '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h5"/>',
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 5.2a3.2 3.2 0 0 1 0 5.8M21 20a6 6 0 0 0-4-5.7"/>',
  trash: '<path d="M5 7h14M9 7V5h6v2M7 7l1 13h8l1-13"/>',
  caretL: '<path d="M15 6l-6 6 6 6"/>',
  upload: '<path d="M12 16V5M8 9l4-4 4 4"/><path d="M5 19h14"/>',
  globe: '<circle cx="12" cy="12" r="8.2"/><path d="M3.8 12h16.4M12 3.8c2.6 2.2 2.6 14 0 16.4M12 3.8c-2.6 2.2-2.6 14 0 16.4"/>',
  flow: '<rect x="3" y="9" width="6" height="6" rx="1.2"/><rect x="15" y="9" width="6" height="6" rx="1.2"/><path d="M9 12h6"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4.5l3 2"/>',
  'check-circle': '<circle cx="12" cy="12" r="8.2"/><path d="M8.4 12.2l2.6 2.6 4.6-5.2"/>',
  check: '<path d="M5 12.5l4 4 10-10.5"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4.5M12 17.4v.1"/>',
  search: '<circle cx="11" cy="11" r="6.2"/><path d="M15.6 15.6L20 20"/>',
  sparkles: '<path d="M12 3l1.7 4.6L18 9l-4.3 1.4L12 15l-1.7-4.6L6 9l4.3-1.4z"/><path d="M18 14l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  list: '<path d="M8.5 6H19M8.5 12H19M8.5 18H19"/><circle cx="4.7" cy="6" r="1.1"/><circle cx="4.7" cy="12" r="1.1"/><circle cx="4.7" cy="18" r="1.1"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 4 1.5 5.5 2 6.5H4c.5-1 2-2.5 2-6.5z"/><path d="M9.5 18.5a2.5 2.5 0 0 0 5 0"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6"/>',
  logout: '<path d="M9.5 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3.5"/><path d="M15.5 8l4 4-4 4M19.5 12H9"/>',
  chevronDown: '<path d="M6 9l6 6 6-6"/>',
  send: '<path d="M5 12l15-7-6.5 15-2.5-5.5z"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>',
  grid: '<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/>',
};

export function Icon({ name, ...rest }) {
  return (
    <span className="ic" {...rest}>
      <svg viewBox="0 0 24 24" dangerouslySetInnerHTML={{ __html: PATHS[name] || '' }} />
    </span>
  );
}
