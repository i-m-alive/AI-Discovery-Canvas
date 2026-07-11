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
};

export function Icon({ name, ...rest }) {
  return (
    <span className="ic" {...rest}>
      <svg viewBox="0 0 24 24" dangerouslySetInnerHTML={{ __html: PATHS[name] || '' }} />
    </span>
  );
}
