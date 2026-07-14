/*
 * AI Discovery Canvas — Phase 1 canvas engine.
 *
 * This is a direct port of the approved interactive prototype
 * (AI_Discovery_Canvas_Prototype v0.5): same markup, same vanilla-DOM
 * engine, same behaviour — deliberately NOT re-written as React state,
 * because the requirement is that the UI look and feel EXACTLY like the
 * prototype. React owns only the mount point (see CanvasApp.jsx); this
 * module owns everything inside it.
 *
 * Differences from the prototype file (all deliberate):
 *   1. Wrapped in initCanvasApp(root) so it mounts into a Next.js page
 *      and returns a cleanup function (listeners/intervals removed on
 *      unmount — required for React StrictMode double-mount in dev).
 *   2. `document.body.classList('present')` → root.classList — the CSS
 *      is scoped under .aidc-canvas-root (see canvas.css header note).
 *   3. NEW: board persistence. On boot it GETs /api/canvas/board and
 *      hydrates saved nodes/edges/artifacts (falling back to the demo
 *      seed when the board is empty or the backend is unreachable), and
 *      every mutation debounce-saves the board back via PUT. That is the
 *      Phase 1 "canvas skeleton is real, not just a mock" requirement —
 *      Phase 2 replaces the simulated agent responses with real Bedrock
 *      calls, so produce()'s canned D map below is EXPECTED to be fake
 *      for now.
 *   4. Mojibake in the source prototype (UTF-8 read as Latin-1: 'â', 'Ã')
 *      restored to the intended characters (→, ·, ≈, ×, ⚠, —, 👋, ⇄, ↳).
 *   5. Teams connects AUTOMATICALLY when you signed in with Microsoft —
 *      it silently reuses that SAME MSAL session (acquireTokenSilent) for
 *      Graph scopes instead of running a separate device-code sign-in.
 *      The manual device-code flow only appears as a fallback for mock-
 *      auth sessions, which have no Microsoft account to reuse.
 */

import { getMsalInstance } from '../lib/msalConfig';

const MARKUP = `
<div id="app">
  <div class="menubar">
    <div class="branddot"></div>
    <button class="mi" id="miFile" data-tip="File">File</button><button class="mi" id="miEdit" data-tip="Edit">Edit</button>
    <button class="mi" id="miView" data-tip="View">View</button><button class="mi" id="miInsert" data-tip="Insert">Insert</button>
    <button class="mi" id="miTools" data-tip="Tools">Tools</button><button class="mi" id="miHelp" data-tip="Help">Help</button>
    <div class="spacer"></div>
    <div class="right">
      <div class="avatars" id="avatarsBox" data-tip="Signed in as you — real-time co-presence (multiple simultaneous facilitators) isn't built yet"></div>
      <button class="btn" data-tip="Real-time multi-user invite/roles isn't built yet — this button is not wired up" disabled style="opacity:.5;cursor:default"><span class="ic" data-ic="users"></span>Share</button>
      <button class="btn solid" id="exportTop" data-tip="Assemble &amp; export the handoff package"><span class="ic" data-ic="upload"></span>Export</button>
    </div>
  </div>
  <div class="menudrop" id="menuDrop"></div>
  <div class="toolbar">
    <a class="chip" id="backProjects" href="/projects" data-tip="Back to your projects" style="text-decoration:none;display:inline-flex;align-items:center;gap:4px;margin-right:8px">‹ Projects</a>
    <div class="boardname" id="boardNameBox" data-tip="Double-click to rename this engagement board"><span class="dot"></span><span id="boardNameText">Untitled Engagement</span></div>
    <div class="session" data-info="session" data-info-title="Session — real" data-info-text="Date is today's real date. The clock counts real elapsed time since this board was opened in this browser tab (resets on refresh — there's no persisted 'session start' concept yet). There is no real multi-facilitator presence tracking yet, so no attendee count is shown."><span class="ic" data-ic="calendar"></span><b id="sessDate">—</b> · <span id="sessClock">00:00</span> · <span id="sessWho">—</span> <span class="ic" data-ic="info" style="font-size:12px;color:#aab2bd"></span></div>
    <div class="searchbox" data-tip="Search the canvas — highlights &amp; flies to matches"><span class="ic" data-ic="search"></span><input id="searchIn" placeholder="Search canvas…"/><span class="cnt" id="searchCnt"></span><button class="clr" id="searchClr"><span class="ic" data-ic="x"></span></button></div>
    <div class="toggle" data-tip="Edgeless vs. structured Page"><button class="on" id="mEdgeless">Edgeless</button><button id="mPage">Page</button></div>
    <div class="tspacer"></div>
    <div class="facil">
      <button class="chip" id="cTimer" data-tip="Timer"><span class="ic" data-ic="clock"></span><span id="timerVal">00:00</span></button>
      <button class="chip" data-tip="Not wired up yet — visual only" style="opacity:.5"><span class="ic" data-ic="vote"></span>Vote</button>
      <button class="chip rec" id="cRec" data-tip="Record"><span class="recdot"></span>REC</button>
      <button class="chip" id="teamsBtn" data-tip="Pull a Teams meeting transcript (Microsoft Graph)"><span class="ic" data-ic="users"></span>Teams</button>
      <button class="chip" id="cPresent" data-tip="Present — a clean, guided walkthrough"><span class="ic" data-ic="play"></span>Present</button>
    </div>
  </div>
  <div class="lensrow" id="lensrow">
    <div class="lensseg" id="lensseg"></div>
    <span class="hint" id="lensHint">one canvas · these are places on it</span>
    <div class="right">
      <button class="chip" id="focusChip" data-tip="Focus — dim the other zones to work on this one (zoom out to mind-map across all)"><span class="ic" data-ic="target"></span>Focus</button>
      <button class="btn" id="handoffBtn" data-tip="Handoff — assemble &amp; export (an action, not a place)"><span class="ic" data-ic="upload"></span>Handoff</button>
    </div>
  </div>
  <div class="main" id="main">
    <div class="drawer left" id="artDrawer">
      <div class="dhead"><span class="ic" data-ic="folder"></span><div><div class="ttl">Live Artifacts</div><div class="sub">your canvas, indexed</div></div>
        <button class="infobtn" data-info="artifacts" data-info-title="Live Artifacts" data-info-text="A plain-English index of everything on the canvas, grouped the way you'd look for it. Each item is live. When an output is approved it files itself here and drops a card in the right zone."><span class="ic" data-ic="info"></span></button></div>
      <div id="folders" style="overflow:auto"></div>
    </div>
    <div class="stage" id="stage">
      <div class="dotgrid"></div>
      <div class="layer" id="layer"><svg id="edges" width="6000" height="3000"></svg></div>

      <button class="handle" id="askHandle" data-tip="Open the Assistant"><span class="ic" data-ic="sparkles"></span><span class="vt">Ask</span></button>

      <div class="drawer right open" id="askDrawer">
        <div class="dhead"><span class="ic" data-ic="sparkles"></span><div><div class="ttl">Assistant</div><div class="sub">sensitive to where you are</div></div>
          <button class="infobtn" data-info="assistant" data-info-title="Assistant" data-info-text="A guide, not a menu. It defaults its context to the zone you're in, narrows to whatever you select, leads with the few right actions, previews output, and on approve files it by type into the canvas + Live Artifacts."><span class="ic" data-ic="info"></span></button>
          <button class="x" id="askClose" data-tip="Collapse"><span class="ic" data-ic="x"></span></button></div>
        <div class="thread" id="thread"></div>
        <div class="suggest" id="suggestBox"><div class="lbl" id="suggestHead" style="cursor:pointer"><span class="ic" data-ic="sparkles"></span>SUGGESTED NEXT <span id="suggStage" style="color:var(--accent)"></span><span class="ic chev" data-ic="chevron" style="margin-left:auto"></span></div><div class="sugg" id="sugg"></div></div>
        <div class="ctxbar" id="ctxbar"><span class="lbl">CONTEXT</span><span class="ctxchip" id="ctxScope"><span class="ic" data-ic="map"></span><span id="ctxScopeLbl">Run zone</span></span></div>
        <div class="composer">
          <div class="row"><textarea id="input" placeholder="Ask anything, or type / for agents…"></textarea><button class="send" id="send" data-tip="Send"><span class="ic" data-ic="send"></span></button></div>
          <div class="tools"><button class="tbtn" id="slashBtn" data-tip="Browse all agents"><span class="ic" data-ic="sparkles"></span>/ Agents</button><button class="tbtn" id="attachBtn" data-tip="Attach a file"><span class="ic" data-ic="paperclip"></span>Attach</button><button class="tbtn" id="webBtn" data-tip="Research the web"><span class="ic" data-ic="globe"></span>Research web</button></div>
          <div class="hint">Lead with <b>Suggested next</b> · or <kbd>/</kbd> for the full catalogue</div>
        </div>
      </div>

      <div id="zoombar" data-tip="Zoom &amp; fit (wheel to zoom)"><button id="zOut"><span class="ic" data-ic="minus"></span></button><span class="zval" id="zVal">100%</span><button id="zIn"><span class="ic" data-ic="plus"></span></button><button id="zFit" data-tip="Fit everything"><span class="ic" data-ic="maximize"></span></button></div>
      <div id="minimap" data-tip="Minimap — click a zone to fly there"><div id="mmInner"></div><span class="mmlabel">map</span></div>
      <div id="transcript"><div class="thead" id="trHead"><span class="live"></span><span class="ttl">Live transcript</span><span class="now" id="trNow"></span><span class="sm"><button class="mini" id="summBtn" data-tip="Summarize into the Assistant"><span class="ic" data-ic="summarize"></span>Summarize</button></span><span class="ic" data-ic="chevron"></span></div><div class="tbody" id="tbody"></div></div>
    </div>
  </div>
</div>

<div id="nodeBar"></div>

<div id="presentBar"><button class="pb" id="pPrev"><span class="ic" data-ic="caretL"></span></button><div><div class="pname" id="pName">Run</div><div class="step-of" id="pStep"></div></div><button class="pb" id="pNext"><span class="ic" data-ic="caretR"></span></button><button class="ex" id="pExit"><span class="ic" data-ic="x"></span>Exit present</button></div>

<div id="modalWrap"><div class="modal"><div class="mh"><span class="ic" data-ic="upload"></span><span class="t">Handoff package</span><button class="x" id="modalX"><span class="ic" data-ic="x"></span></button></div><div class="mb"><p class="vsub">Assembled from your Live Artifacts — documentation only in Phase 1.</p><div id="handoffList"></div><div class="fmt"><span style="color:var(--muted);font-size:13px">Format:</span><button class="chip on" id="fmtDocx">DOCX</button><button class="chip" id="fmtPdf">PDF</button><button class="chip" id="fmtMd">Markdown</button></div><button class="btn solid" id="exportPkg" style="margin-top:8px"><span class="ic" data-ic="upload"></span>Export package</button></div></div></div><div id="graphModalWrap"><div class="modal wide"><div class="mh"><span class="ic" id="graphModalIcon" data-ic="calendar"></span><span class="t" id="graphModalTitle">Browse</span><button class="x" id="graphModalX"><span class="ic" data-ic="x"></span></button></div><div class="mb" id="graphModalBody"></div></div></div>

<input type="file" id="fileInput" style="display:none" multiple/>
<div id="slash"></div><div id="tooltip"></div><div id="popover"></div><div id="toast"></div>
`;

export function initCanvasApp(root, opts = {}) {
  root.innerHTML = MARKUP;
  const USER = opts.user || {};
  // A Workshop IS this canvas board — every board/document/agent call is
  // scoped to it server-side (see app/routes/canvas.py, app/routes/agents.py).
  const WORKSHOP_ID = opts.workshopId;
  const PROJECT_ID = opts.projectId;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ── Cleanup bookkeeping (React unmount / StrictMode remount) ────────
  const docListeners = [];   // [target, type, fn, opts]
  const intervals = [];
  const timeouts = new Set();
  function on(target, type, fn, opts) { target.addEventListener(type, fn, opts); docListeners.push([target, type, fn, opts]); }
  function every(ms, fn) { const h = setInterval(fn, ms); intervals.push(h); return h; }
  function later(ms, fn) { const h = setTimeout(() => { timeouts.delete(h); fn(); }, ms); timeouts.add(h); return h; }

  const $ = (id) => root.querySelector('#' + id);

  // ── Icons (verbatim from the prototype) ─────────────────────────────
  const ICON = {
    pointer: '<path d="M5 3l5 17 2.3-6.5 6.7-2.2z"/>', hand: '<path d="M12 4v16M4 12h16"/><path d="M9 7l3-3 3 3M9 17l3 3 3-3M7 9l-3 3 3 3M17 9l3 3-3 3"/>',
    sticky: '<path d="M5 4h14v9l-6 6H5z"/><path d="M19 13h-6v6"/>', square: '<rect x="4" y="4" width="16" height="16" rx="2.5"/>', circle: '<circle cx="12" cy="12" r="8.5"/>',
    connector: '<circle cx="6" cy="6" r="2.4"/><circle cx="18" cy="18" r="2.4"/><path d="M7.7 7.7l8.6 8.6"/>', text: '<path d="M5 5h14M12 5v14M9 19h6"/>',
    pen: '<path d="M4 20l4-1L18 8l-3-3L5 15z"/><path d="M14 6l3 3"/>', frame: '<path d="M3 8V4h4M21 8V4h-4M3 16v4h4M21 16v4h-4"/>',
    comment: '<path d="M5 5h14v9H10l-4 4z"/>', upload: '<path d="M12 16V5M8 9l4-4 4 4"/><path d="M5 19h14"/>', clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4.5l3 2"/>',
    vote: '<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="M8 12l3 3 5-6"/>', play: '<path d="M7 5l12 7-12 7z"/>',
    sparkles: '<path d="M12 3l1.7 4.6L18 9l-4.3 1.4L12 15l-1.7-4.6L6 9l4.3-1.4z"/><path d="M18 14l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
    send: '<path d="M5 12l15-7-6.5 15-2.5-5.5z"/>', paperclip: '<path d="M9 8v8a3 3 0 0 0 6 0V6.5a4.5 4.5 0 0 0-9 0V16a6 6 0 0 0 12 0V8.5"/>',
    globe: '<circle cx="12" cy="12" r="8.2"/><path d="M3.8 12h16.4M12 3.8c2.6 2.2 2.6 14 0 16.4M12 3.8c-2.6 2.2-2.6 14 0 16.4"/>',
    map: '<path d="M9 4L4 6v14l5-2 6 2 5-2V4l-5 2-6-2z"/><path d="M9 4v14M15 6v14"/>', list: '<path d="M8.5 6H19M8.5 12H19M8.5 18H19"/><circle cx="4.7" cy="6" r="1.1"/><circle cx="4.7" cy="12" r="1.1"/><circle cx="4.7" cy="18" r="1.1"/>',
    'check-circle': '<circle cx="12" cy="12" r="8.2"/><path d="M8.4 12.2l2.6 2.6 4.6-5.2"/>', check: '<path d="M5 12.5l4 4 10-10.5"/>', alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4.5M12 17.4v.1"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>', info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.6v.1"/>',
    plus: '<path d="M12 5v14M5 12h14"/>', minus: '<path d="M5 12h14"/>', maximize: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>', search: '<circle cx="11" cy="11" r="6.2"/><path d="M15.6 15.6L20 20"/>',
    users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 5.2a3.2 3.2 0 0 1 0 5.8M21 20a6 6 0 0 0-4-5.7"/>',
    dollar: '<path d="M12 3v18"/><path d="M16 7.5C16 5.6 14.2 4.5 12 4.5S8 5.6 8 7.5 9.8 10.5 12 10.5 16 11.6 16 13.5 14.2 16.5 12 16.5 8 15.4 8 13.5"/>',
    scale: '<path d="M12 4v16M5 8h14"/><path d="M5 8l-2.5 5h5zM19 8l-2.5 5h5z"/><path d="M8 20h8"/>', flow: '<rect x="3" y="9" width="6" height="6" rx="1.2"/><rect x="15" y="9" width="6" height="6" rx="1.2"/><path d="M9 12h6"/>',
    'doc-text': '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h5"/>', target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="0.7"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6"/>', summarize: '<path d="M5 6h14M5 10h14M5 14h9M5 18h6"/>',
    database: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
    calendar: '<rect x="4" y="5" width="16" height="16" rx="2"/><path d="M4 9h16M8 3v4M16 3v4"/>', chevron: '<path d="M6 9l6 6 6-6"/>', caretL: '<path d="M15 6l-6 6 6 6"/>', caretR: '<path d="M9 6l6 6-6 6"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>', edit: '<path d="M4 20l4-1L18 8l-3-3L5 15z"/>', copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M4 16V4h12"/>', trash: '<path d="M5 7h14M9 7V5h6v2M7 7l1 13h8l1-13"/>',
  };
  function fillIcons(r) { (r || root).querySelectorAll('[data-ic]').forEach((e) => { if (e.dataset.done) return; const n = e.getAttribute('data-ic'); if (!ICON[n]) return; e.innerHTML = '<svg viewBox="0 0 24 24">' + ICON[n] + '</svg>'; e.dataset.done = 1; }); }
  const ico = (n) => '<span class="ic"><svg viewBox="0 0 24 24">' + (ICON[n] || '') + '</svg></span>';

  // ── Zones / agents / artifact catalog ────────────────────────────────
  // Region `id`s are load-bearing (persisted on every node as `n.region`,
  // see serializeBoard() below) and MUST stay stable across this
  // relabel — only `label`/`sub` (display-only, never persisted; REGIONS
  // itself is a static client-side catalog, rebuilt fresh on every load)
  // change to the 4-phase engagement-lifecycle language.
  const REGIONS = [
    { id: 'prepare', label: 'Pre-Workshop', sub: 'Ingest · Internalize · Research', x: 40, y: 90, w: 820, h: 600, desc: 'do the homework before the room', suggest: ['ingest', 'deepresearch', 'questions'] },
    { id: 'run', label: 'During Workshop', sub: 'Capture · Synthesize · Generate', x: 960, y: 90, w: 940, h: 700, desc: 'capture the workshop live', suggest: ['drawflow', 'findgaps', 'decisions'] },
    { id: 'synthesize', label: 'Post-Workshop', sub: 'Backlog · Opportunities · MoM', x: 2000, y: 90, w: 960, h: 700, desc: 'turn talk into deliverables', suggest: ['opportunities', 'mom', 'stories'] },
    { id: 'project', label: 'Proposal & Planning', sub: 'SOW · ROI · Risk · Team', x: 3060, y: 90, w: 900, h: 620, desc: 'scope the engagement', suggest: ['sow', 'roi'] },
  ];
  const baseRect = {}; REGIONS.forEach((r) => { baseRect[r.id] = { x: r.x, y: r.y, w: r.w, h: r.h }; });
  const REGION = (id) => REGIONS.find((r) => r.id === id);
  // Pre-Workshop now has its own dashboard page (see PreWorkshopDashboard.jsx)
  // outside this canvas entirely — opts.initialLens lets the phase-tab bar
  // (frontend/app/canvas/[workshopId]/page.js) open the canvas already
  // scoped to whichever of the OTHER 3 phases was clicked.
  let curLens = (opts.initialLens && REGION(opts.initialLens)) ? opts.initialLens : 'run', focusOn = false;

  const AGENTS = [
    { id: 'ingest', region: 'prepare', icon: 'database', nm: 'Ingest client docs', ds: 'Pull context from files' },
    { id: 'research', region: 'prepare', icon: 'globe', nm: 'Research company', ds: 'Summarise public info' },
    { id: 'deepresearch', region: 'prepare', icon: 'target', nm: 'Deep research', ds: 'Analyse all Prepare docs + web' },
    { id: 'brief', region: 'prepare', icon: 'doc-text', nm: 'Context brief', ds: 'Pre-workshop synthesis' },
    { id: 'questions', region: 'prepare', icon: 'list', nm: 'Questions to ask', ds: 'Sharp discovery questions' },
    { id: 'agenda', region: 'prepare', icon: 'list', nm: 'Draft agenda', ds: 'Plan the workshop' },
    { id: 'transcribe', region: 'run', icon: 'mic', nm: 'Transcribe', ds: 'Capture dialogue' },
    { id: 'summarize', region: 'run', icon: 'summarize', nm: 'Summarize', ds: 'Recap so far' },
    { id: 'drawflow', region: 'run', icon: 'flow', nm: 'Draw process flow', ds: 'Diagram the process' },
    { id: 'findgaps', region: 'run', icon: 'alert', nm: 'Find gaps', ds: 'Surface gaps & risks' },
    { id: 'decisions', region: 'run', icon: 'check-circle', nm: 'Capture decisions', ds: 'Log decisions & actions' },
    { id: 'stories', region: 'synthesize', icon: 'list', nm: 'User stories', ds: 'Dev-ready stories' },
    { id: 'bdd', region: 'synthesize', icon: 'check-circle', nm: 'Acceptance criteria', ds: 'Given-When-Then' },
    { id: 'docs', region: 'synthesize', icon: 'doc-text', nm: 'Documentation', ds: 'Docs & manuals' },
    { id: 'opportunities', region: 'synthesize', icon: 'target', nm: 'Find opportunities', ds: 'Improvement & automation' },
    { id: 'mom', region: 'synthesize', icon: 'summarize', nm: 'Minutes of Meeting', ds: 'Assemble from session' },
    { id: 'sow', region: 'project', icon: 'doc-text', nm: 'Draft SOW', ds: 'Statement of Work' },
    { id: 'roi', region: 'project', icon: 'dollar', nm: 'Calculate ROI', ds: 'Estimate return' },
    { id: 'risk', region: 'project', icon: 'scale', nm: 'Benefit ⇄ risk', ds: 'Weigh benefit vs risk' },
    { id: 'team', region: 'project', icon: 'users', nm: 'Suggest team', ds: 'Team composition' },
  ];
  const A = (id) => AGENTS.find((a) => a.id === id);
  // Real folder taxonomy (Master Documentation §13 / FR-04), genuinely
  // EMPTY until a real agent draft is Approved into one of them — no
  // fictional pharmacovigilance items pre-loaded here.
  const ARTI = [
    { folder: 'Background', icon: 'database', items: [] },
    { folder: 'How it works', icon: 'flow', items: [] },
    { folder: 'Requirements', icon: 'list', items: [] },
    { folder: 'Issues & decisions', icon: 'alert', items: [] },
    { folder: 'Meeting notes', icon: 'summarize', items: [] },
    { folder: 'Proposal', icon: 'dollar', items: [] },
  ];
  const collapsed = { Background: 1, 'How it works': 1 };
  const FOLDER = { ingest: 'Background', research: 'Background', deepresearch: 'Background', brief: 'Background', questions: 'Background', agenda: 'Background', drawflow: 'How it works', docs: 'How it works', summarize: 'Meeting notes', mom: 'Meeting notes', stories: 'Requirements', bdd: 'Requirements', findgaps: 'Issues & decisions', decisions: 'Issues & decisions', opportunities: 'Issues & decisions', sow: 'Proposal', roi: 'Proposal', risk: 'Proposal', team: 'Proposal' };

  const S = { tool: 'select', zoom: 1, panX: 0, panY: 0, sel: null, nodes: [], edges: [], web: false, files: [], nid: 0, eid: 0, connectFrom: null };
  const layer = $('layer'), stage = $('stage'), edgesSvg = $('edges');

  // ── Persistence (NEW vs prototype — see module header) ──────────────
  let saveT = null;
  function serializeBoard() {
    const nodes = S.nodes.map((n) => {
      const { el, ...rest } = n; return rest;
    });
    return {
      nodes, edges: S.edges, nid: S.nid, eid: S.eid,
      artifacts: ARTI.map((f) => ({ folder: f.folder, items: [...f.items] })),
      transcript: [...trLines],
      board_name: boardName,
    };
  }
  function scheduleSave() {
    clearTimeout(saveT);
    saveT = setTimeout(() => {
      fetch('/api/canvas/board?workshop_id=' + WORKSHOP_ID, {
        method: 'PUT', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(serializeBoard()),
      }).catch(() => {});
    }, 900);
    timeouts.add(saveT);
  }

  /* regions */
  function renderRegions() { REGIONS.forEach((r) => { const d = document.createElement('div'); d.className = 'region' + (r.id === curLens ? ' active' : ''); d.id = 'reg-' + r.id; d.style.left = r.x + 'px'; d.style.top = r.y + 'px'; d.style.width = r.w + 'px'; d.style.height = r.h + 'px'; d.innerHTML = '<div class="rlabel">' + r.label + '</div>'; layer.appendChild(d); }); }
  function syncRegionEls() { REGIONS.forEach((r) => { const d = root.querySelector('#reg-' + r.id); if (d) { d.style.left = r.x + 'px'; d.style.top = r.y + 'px'; d.style.width = r.w + 'px'; d.style.height = r.h + 'px'; d.classList.toggle('active', r.id === curLens); } }); }
  function recomputeRegions() {
    REGIONS.forEach((r) => {
      const b = baseRect[r.id]; let x0 = b.x, y0 = b.y, x1 = b.x + b.w, y1 = b.y + b.h;
      S.nodes.forEach((n) => { if (n.region !== r.id) return; x0 = Math.min(x0, n.x - 30); y0 = Math.min(y0, n.y - 30); x1 = Math.max(x1, n.x + (n.w || 180) + 30); y1 = Math.max(y1, n.y + nodeH(n) + 30); });
      r.x = x0; r.y = y0; r.w = x1 - x0; r.h = y1 - y0;
    }); syncRegionEls(); updateMinimap();
  }
  function assignRegion(x, y) { for (const r of REGIONS) { if (x >= baseRect[r.id].x && x <= baseRect[r.id].x + baseRect[r.id].w && y >= baseRect[r.id].y && y <= baseRect[r.id].y + baseRect[r.id].h) return r.id; } return null; }
  function spotIn(r) { return { x: r.x + 40 + Math.random() * Math.max(60, r.w - 260), y: r.y + 70 + Math.random() * Math.max(60, r.h - 160) }; }

  function applyT() { layer.style.transform = `translate(${S.panX}px,${S.panY}px) scale(${S.zoom})`; $('zVal').textContent = Math.round(S.zoom * 100) + '%'; updateMinimap(); positionNodeBar(); }
  function stageDim() { const r = stage.getBoundingClientRect(); return { w: r.width, h: r.height }; }
  let animT = null;
  function panToRegion(id, anim) { const r = REGION(id), d = stageDim(), pad = 90; const z = Math.min(1.4, Math.max(0.35, Math.min(d.w / (r.w + pad * 2), d.h / (r.h + pad * 2)))); S.zoom = z; S.panX = d.w / 2 - (r.x + r.w / 2) * z; S.panY = d.h / 2 - (r.y + r.h / 2) * z; if (anim !== false) { layer.classList.add('anim'); clearTimeout(animT); animT = later(480, () => layer.classList.remove('anim')); } applyT(); }
  function fitAll() { const d = stageDim(); const minX = Math.min(...REGIONS.map((r) => r.x)) - 40, minY = Math.min(...REGIONS.map((r) => r.y)) - 40, maxX = Math.max(...REGIONS.map((r) => r.x + r.w)) + 40, maxY = Math.max(...REGIONS.map((r) => r.y + r.h)) + 40; const z = Math.min(d.w / (maxX - minX), d.h / (maxY - minY)); S.zoom = z; S.panX = d.w / 2 - ((minX + maxX) / 2) * z; S.panY = d.h / 2 - ((minY + maxY) / 2) * z; layer.classList.add('anim'); later(480, () => layer.classList.remove('anim')); applyT(); }

  /* ---------- nodes ---------- */
  function makeNode(spec) { const n = Object.assign({ id: 'n' + (++S.nid), type: 'sticky', x: 0, y: 0, w: 170, h: null, text: '', fill: null }, spec); if (n.region === undefined) n.region = assignRegion(n.x, n.y); S.nodes.push(n); renderNode(n); scheduleSave(); return n; }
  function hydrateNode(saved) { const n = { ...saved }; S.nodes.push(n); renderNode(n); return n; }
  function renderNode(n) {
    const d = document.createElement('div'); d.className = 'node ' + n.type + (n.type === 'shape' ? (' ' + (n.shape || 'rect')) : '') + (n.kind ? (' kind-' + n.kind) : '');
    d.style.left = n.x + 'px'; d.style.top = n.y + 'px'; d.style.width = (n.w || 170) + 'px'; if (n.h) d.style.height = n.h + 'px'; d.dataset.id = n.id; n.el = d;
    let inner = '<div class="body"' + (n.fill ? (' style="--fill:' + n.fill + '"') : '') + '>';
    if (n.type === 'card') { inner += '<div class="ntitle">' + (n.icon ? ico(n.icon) : '') + (n.label || '') + '</div>' + (n.meta ? '<div class="nmeta">' + n.meta + '</div>' : '') + (n.doc ? '<div class="open"' + (n.docId ? ' data-preview="1" style="cursor:pointer;text-decoration:underline"' : '') + '>' + ico('doc-text') + (n.docId ? 'view document' : 'open document') + '</div>' : '') + (n.gen ? '<button class="genbtn" data-gen="' + n.gen + '">' + ico('sparkles') + 'Generate with Assistant</button>' : '') + (n.diagram ? '<button class="genbtn" data-dionode="1">' + ico('flow') + 'Open in draw.io</button>' : '') + (n.prov ? '<div class="prov">↳ ' + n.prov + '</div>' : ''); }
    else if (n.type === 'frame') { inner += ''; }
    else { inner += '<div class="ntext">' + (n.text || '') + '</div>'; }
    inner += '</div>';
    if (n.type === 'frame') inner += '<div class="flabel">' + (n.text || 'Frame') + '</div>';
    inner += '<div class="rz"></div>';
    d.innerHTML = inner; d.dataset.txt = ((n.text || '') + ' ' + (n.label || '') + ' ' + (n.meta || '')).toLowerCase();
    d.addEventListener('pointerdown', (e) => onNodeDown(n, e));
    d.addEventListener('dblclick', () => { if (n.type === 'card') return; startEdit(n); });
    const g = d.querySelector('[data-gen]'); if (g) g.onclick = (e) => { e.stopPropagation(); openAssistant(); runAgent(g.dataset.gen); };
    const dn = d.querySelector('[data-dionode]'); if (dn) dn.onclick = (e) => { e.stopPropagation(); openDrawioEditor(n.diagram.xml, (x) => { n.diagram.xml = x; scheduleSave(); }); };
    const pv = d.querySelector('[data-preview]'); if (pv) pv.onclick = (e) => { e.stopPropagation(); openDocPreview(n.label, n.docId); };
    const rz = d.querySelector('.rz'); if (rz) rz.addEventListener('pointerdown', (e) => startResize(n, e));
    layer.appendChild(d); return d;
  }
  function nodeH(n) { return n.h || (n.el ? n.el.offsetHeight : 80); }

  let dragNode = null, resizing = null;
  function onNodeDown(n, e) {
    if (e.target.closest('.rz') || e.target.closest('[data-gen]')) return;
    if (S.tool === 'conn') { e.stopPropagation(); handleConnect(n); return; }
    if (e.target.getAttribute && e.target.getAttribute('contenteditable') === 'true') return;
    e.stopPropagation(); selectNode(n.id);
    dragNode = { n, sx: e.clientX, sy: e.clientY, ox: n.x, oy: n.y, moved: false };
  }
  on(document, 'pointermove', (e) => {
    if (dragNode) { const dx = (e.clientX - dragNode.sx) / S.zoom, dy = (e.clientY - dragNode.sy) / S.zoom; if (Math.abs(e.clientX - dragNode.sx) + Math.abs(e.clientY - dragNode.sy) > 3) dragNode.moved = true; dragNode.n.x = dragNode.ox + dx; dragNode.n.y = dragNode.oy + dy; dragNode.n.el.style.left = dragNode.n.x + 'px'; dragNode.n.el.style.top = dragNode.n.y + 'px'; drawEdges(); positionNodeBar(); }
    if (resizing) { const dw = (e.clientX - resizing.sx) / S.zoom, dh = (e.clientY - resizing.sy) / S.zoom; resizing.n.w = Math.max(60, resizing.ow + dw); resizing.n.h = Math.max(40, resizing.oh + dh); resizing.n.el.style.width = resizing.n.w + 'px'; resizing.n.el.style.height = resizing.n.h + 'px'; drawEdges(); positionNodeBar(); }
  });
  on(document, 'pointerup', () => { if (dragNode) { if (dragNode.moved) { recomputeRegions(); scheduleSave(); } dragNode = null; } if (resizing) { recomputeRegions(); scheduleSave(); resizing = null; } });
  function startResize(n, e) { e.stopPropagation(); selectNode(n.id); resizing = { n, sx: e.clientX, sy: e.clientY, ow: n.w || 170, oh: nodeH(n) }; }

  function startEdit(n) {
    const t = n.el.querySelector('.ntext') || n.el.querySelector('.flabel'); if (!t) return;
    t.setAttribute('contenteditable', 'true'); t.focus();
    const sel = window.getSelection(); sel.selectAllChildren(t);
    t.onblur = () => { t.removeAttribute('contenteditable'); n.text = t.textContent; n.el.dataset.txt = (n.text + ' ' + (n.label || '')).toLowerCase(); t.onblur = null; scheduleSave(); };
    t.onkeydown = (ev) => { if (ev.key === 'Enter' && !ev.shiftKey && n.type !== 'text') { ev.preventDefault(); t.blur(); } };
  }
  function selectNodeBase(id) { S.sel = id; root.querySelectorAll('.node').forEach((el) => el.classList.toggle('sel', el.dataset.id === id)); const n = S.nodes.find((x) => x.id === id); if (n) setScope((n.text || n.label || 'Item').replace(/<[^>]+>/g, '').slice(0, 40), n.icon || 'map'); positionNodeBar(); }
  function selectNode(id) { selectNodeBase(id); buildNodeBar(); positionNodeBar(); }
  function clearSel() { S.sel = null; root.querySelectorAll('.node').forEach((el) => el.classList.remove('sel')); scopeToZone(); positionNodeBar(); }
  function delNode(id) {
    const n = S.nodes.find((x) => x.id === id); if (!n) return;
    if (n.docId) { fetch('/api/agents/document/' + n.docId + '?workshop_id=' + WORKSHOP_ID, { method: 'DELETE', credentials: 'same-origin' }).catch(() => {}); }
    if (n.el) n.el.remove(); S.nodes = S.nodes.filter((x) => x.id !== id); S.edges = S.edges.filter((e) => e.from !== id && e.to !== id); if (S.sel === id) clearSel(); drawEdges(); recomputeRegions(); scheduleSave();
  }

  // Generic full-text preview overlay (documents) — same overlay pattern
  // as the draw.io editor, reused rather than duplicated.
  async function openDocPreview(title, docId) {
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;z-index:120;background:rgba(24,39,64,.5);display:flex;align-items:center;justify-content:center';
    ov.innerHTML = '<div style="width:min(720px,92vw);max-height:82vh;background:#fff;border-radius:12px;overflow:hidden;display:flex;flex-direction:column">' +
      '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--line);font-size:13px"><b>' + esc(title || 'Document') + '</b><span style="color:var(--muted);font-size:11px" data-status>loading…</span><button data-docx="1" style="margin-left:auto;font-size:15px;cursor:pointer;border:none;background:none">✕</button></div>' +
      '<div style="padding:14px 18px;overflow:auto;white-space:pre-wrap;font-size:12.5px;line-height:1.6;color:#3a4350" data-body></div></div>';
    root.appendChild(ov);
    ov.querySelector('[data-docx]').onclick = () => ov.remove();
    ov.addEventListener('click', (e) => { if (e.target === ov) ov.remove(); });
    try {
      const r = await fetch('/api/agents/document/' + docId + '?workshop_id=' + WORKSHOP_ID, { credentials: 'same-origin' });
      const j = await r.json();
      if (!ov.isConnected) return;
      if (j && j.ok) { ov.querySelector('[data-status]').textContent = j.text.length + ' chars extracted'; ov.querySelector('[data-body]').textContent = j.text; }
      else { ov.querySelector('[data-status]').textContent = ''; ov.querySelector('[data-body]').textContent = '⚠ ' + ((j && j.error) || 'could not load this document — it may have been removed'); }
    } catch { if (ov.isConnected) { ov.querySelector('[data-status]').textContent = ''; ov.querySelector('[data-body]').textContent = '⚠ network error loading document'; } }
  }
  function dupNode(id) { const n = S.nodes.find((x) => x.id === id); if (!n) return; const c = Object.assign({}, n); delete c.id; delete c.el; c.x = n.x + 26; c.y = n.y + 26; const nn = makeNode(c); selectNode(nn.id); recomputeRegions(); }

  /* edges / connectors */
  function drawEdges() { let h = ''; S.edges.forEach((e) => { const a = S.nodes.find((x) => x.id === e.from), b = S.nodes.find((x) => x.id === e.to); if (!a || !b) return; const ax = a.x + (a.w || 170) / 2, ay = a.y + nodeH(a) / 2, bx = b.x + (b.w || 170) / 2, by = b.y + nodeH(b) / 2; h += '<line x1="' + ax + '" y1="' + ay + '" x2="' + bx + '" y2="' + by + '" stroke="#8aa0bd" stroke-width="2"/><circle cx="' + bx + '" cy="' + by + '" r="3.5" fill="#8aa0bd"/>'; }); edgesSvg.innerHTML = h; }
  function handleConnect(n) { if (!S.connectFrom) { S.connectFrom = n.id; n.el.classList.add('connsrc'); toast('Pick the node to connect to…'); } else if (S.connectFrom === n.id) { n.el.classList.remove('connsrc'); S.connectFrom = null; } else { S.edges.push({ id: 'e' + (++S.eid), from: S.connectFrom, to: n.id }); const src = S.nodes.find((x) => x.id === S.connectFrom); if (src && src.el) src.el.classList.remove('connsrc'); S.connectFrom = null; drawEdges(); toast('Linked'); setTool('select'); scheduleSave(); } }

  /* node context toolbar */
  const SW = ['#fff2c2', '#d8e8fb', '#d8f0e2', '#f7dce4', '#e6e0f7', '#eceff3', '#ffffff'];
  function positionNodeBar() { const bar = $('nodeBar'); const n = S.nodes.find((x) => x.id === S.sel); if (!n || !n.el || root.classList.contains('present')) { bar.style.display = 'none'; return; } const r = n.el.getBoundingClientRect(); bar.style.display = 'flex'; bar.style.left = Math.max(8, Math.min(window.innerWidth - bar.offsetWidth - 8, r.left)) + 'px'; bar.style.top = Math.max(70, r.top - 42) + 'px'; }
  function buildNodeBar() {
    const bar = $('nodeBar'); let h = ''; const n = S.nodes.find((x) => x.id === S.sel); const colorable = n && ['sticky', 'shape', 'text', 'frame'].includes(n.type);
    if (colorable) h += SW.map((c) => '<div class="sw" data-c="' + c + '" style="background:' + c + '"></div>').join('') + '<div class="sep"></div>';
    h += '<button class="nb" data-act="edit" data-tip="Edit text (or double-click)">' + ico('edit') + '</button><button class="nb" data-act="dup" data-tip="Duplicate">' + ico('copy') + '</button><button class="nb del" data-act="del" data-tip="Delete">' + ico('trash') + '</button>';
    bar.innerHTML = h; fillIcons(bar);
    bar.querySelectorAll('.sw').forEach((s) => { s.onclick = () => { const nn = S.nodes.find((x) => x.id === S.sel); if (!nn) return; nn.fill = s.dataset.c; const b = nn.el.querySelector('.body'); if (b) b.style.setProperty('--fill', nn.fill); scheduleSave(); }; });
    bar.querySelectorAll('[data-act]').forEach((b) => { b.onclick = () => { const act = b.dataset.act, nn = S.nodes.find((x) => x.id === S.sel); if (!nn) return; if (act === 'edit') startEdit(nn); if (act === 'dup') dupNode(nn.id); if (act === 'del') delNode(nn.id); }; });
  }

  function hydrateBoard(board) {
    S.nid = board.nid || 0; S.eid = board.eid || 0;
    (board.nodes || []).forEach((n) => hydrateNode(n));
    S.edges = (board.edges || []).map((e) => ({ ...e }));
    (board.artifacts || []).forEach((sf) => { const f = ARTI.find((x) => x.folder === sf.folder); if (f) f.items = [...(sf.items || [])]; });
    (board.transcript || []).forEach((l) => { const ix = String(l).indexOf(': '); if (ix > 0) addTrLine(l.slice(0, ix), l.slice(ix + 2), false); else addTrLine('', l, false); });
    if (board.board_name) setBoardName(board.board_name);
    renderArtifacts(); drawEdges(); recomputeRegions();
  }

  /* tools — switched via the Insert menu (MENUS.insert below), not a
     persistent sidebar; the sidebar (and its Select/Pan buttons) was
     removed to make room for the always-visible Live Artifacts panel.
     Select is the default S.tool and needs no dedicated button; Pan had
     no functional behavior of its own to begin with — empty-canvas
     drag-to-pan below (stage pointerdown/pointermove) already works
     regardless of S.tool, it only ever changed the cursor to 'grab'. */
  function setTool(t) { S.tool = t; stage.style.cursor = ['sticky', 'rect', 'ellipse', 'text', 'frame'].includes(t) ? 'crosshair' : (t === 'conn' ? 'cell' : 'default'); if (t !== 'conn' && S.connectFrom) { const s = S.nodes.find((x) => x.id === S.connectFrom); if (s && s.el) s.el.classList.remove('connsrc'); S.connectFrom = null; } }
  const fileInput = $('fileInput');

  /* canvas pan + create */
  let drag = false, last = null;
  on(stage, 'pointerdown', (e) => {
    if (e.target.closest('.node,.drawer,.handle,#zoombar,#minimap,#transcript,.region .rlabel,#presentBar,#nodeBar,#prepareActions,#runActions,#graphModalWrap')) return;
    if (['sticky', 'rect', 'ellipse', 'text', 'frame'].includes(S.tool)) {
      const r = stage.getBoundingClientRect(); const wx = (e.clientX - r.left - S.panX) / S.zoom, wy = (e.clientY - r.top - S.panY) / S.zoom; let n;
      if (S.tool === 'sticky') n = makeNode({ type: 'sticky', text: '', x: wx, y: wy, w: 120, h: 90 });
      else if (S.tool === 'rect') n = makeNode({ type: 'shape', shape: 'rect', text: '', x: wx, y: wy, w: 150, h: 90 });
      else if (S.tool === 'ellipse') n = makeNode({ type: 'shape', shape: 'ellipse', text: '', x: wx, y: wy, w: 130, h: 130 });
      else if (S.tool === 'text') n = makeNode({ type: 'text', text: 'Text', x: wx, y: wy, w: 140, h: 40 });
      else if (S.tool === 'frame') n = makeNode({ type: 'frame', text: 'Frame', x: wx, y: wy, w: 300, h: 220 });
      setTool('select'); selectNode(n.id); recomputeRegions(); if (n.type !== 'frame') startEdit(n); return;
    }
    if (S.tool === 'conn') { if (S.connectFrom) { const s = S.nodes.find((x) => x.id === S.connectFrom); if (s && s.el) s.el.classList.remove('connsrc'); S.connectFrom = null; } return; }
    clearSel(); drag = true; last = { x: e.clientX, y: e.clientY }; stage.setPointerCapture(e.pointerId);
  });
  on(stage, 'pointermove', (e) => { if (!drag) return; S.panX += e.clientX - last.x; S.panY += e.clientY - last.y; last = { x: e.clientX, y: e.clientY }; applyT(); });
  on(stage, 'pointerup', () => { drag = false; });
  on(stage, 'wheel', (e) => {
    // Scrolling inside the drawers (chat thread, Suggested Next, Live
    // Artifacts folders), the transcript log, or a modal must scroll that
    // element natively — not zoom the canvas underneath it. This was the
    // reported bug: scrolling the chat moved the canvas instead.
    if (e.target.closest('.drawer,#transcript,.modal')) return;
    e.preventDefault(); const r = stage.getBoundingClientRect(); const f = e.deltaY < 0 ? 1.1 : 0.9; const nz = Math.min(2.2, Math.max(0.3, S.zoom * f)); const mx = e.clientX - r.left, my = e.clientY - r.top; S.panX = mx - (mx - S.panX) * (nz / S.zoom); S.panY = my - (my - S.panY) * (nz / S.zoom); S.zoom = nz; applyT();
  }, { passive: false });
  $('zIn').onclick = () => { S.zoom = Math.min(2.2, S.zoom * 1.15); applyT(); };
  $('zOut').onclick = () => { S.zoom = Math.max(0.3, S.zoom / 1.15); applyT(); };
  $('zFit').onclick = fitAll;
  on(document, 'keydown', (e) => { const ed = document.activeElement; const typing = ed && (ed.tagName === 'TEXTAREA' || ed.tagName === 'INPUT' || ed.getAttribute('contenteditable') === 'true'); if (typing) return; if ((e.key === 'Delete' || e.key === 'Backspace') && S.sel) { e.preventDefault(); delNode(S.sel); } if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'd' && S.sel) { e.preventDefault(); dupNode(S.sel); } });

  /* lens + focus — a numbered engagement-phase stepper (Pre-Workshop →
     During Workshop → Post-Workshop → Proposal & Planning), one step per
     REGION in array order. Steps before curLens are check-marked done,
     the current one is highlighted, later ones are dim/upcoming — same
     visual language as the reference product this was modeled on. */
  function renderLens() {
    const el = $('lensseg'); el.innerHTML = '';
    const curIdx = REGIONS.findIndex((r) => r.id === curLens);
    REGIONS.forEach((r, i) => {
      const done = i < curIdx, on = r.id === curLens;
      const b = document.createElement('button');
      b.className = 'lens' + (on ? ' on' : '') + (done ? ' done' : '');
      b.setAttribute('data-tip', r.label + ' — ' + r.desc);
      b.innerHTML = '<span class="lnum">' + (done ? ico('check') : (i + 1)) + '</span>' +
        '<span class="ltxt"><span class="llabel">' + esc(r.label) + '</span><span class="lsub">' + esc(r.sub || '') + '</span></span>';
      b.onclick = () => setLens(r.id);
      el.appendChild(b); fillIcons(b);
    });
    const hint = $('lensHint'); if (hint) hint.textContent = 'Phase ' + (curIdx + 1) + ' of ' + REGIONS.length;
  }
  function setLens(id) { curLens = id; syncRegionEls(); renderLens(); renderSuggest(); if (!S.sel) scopeToZone(); applyFocus(); panToRegion(id); syncTranscriptVisibility(); }
  function applyFocus() { stage.classList.toggle('focus', focusOn); S.nodes.forEach((n) => { if (!n.el) return; n.el.classList.toggle('faded', focusOn && n.region && n.region !== curLens); }); }
  $('focusChip').onclick = function () { focusOn = !focusOn; this.classList.toggle('on', focusOn); applyFocus(); toast(focusOn ? 'Focus on — other zones dimmed' : 'Focus off'); };

  function renderSuggest() { const r = REGION(curLens), sroot = $('sugg'); sroot.innerHTML = ''; $('suggStage').textContent = '· ' + r.label; r.suggest.forEach((id) => { const a = A(id); if (!a) return; const b = document.createElement('button'); b.innerHTML = ico(a.icon) + '<div><div style="font-weight:600">' + a.nm + '</div><div class="d">' + a.ds + '</div></div>'; b.onclick = () => { openAssistant(); runAgent(id); }; sroot.appendChild(b); fillIcons(b); }); }
  $('suggestHead').onclick = () => { $('suggestBox').classList.toggle('col'); };

  /* context */
  function setScope(l, ic2) { $('ctxScopeLbl').textContent = l; root.querySelector('#ctxScope .ic').innerHTML = '<svg viewBox="0 0 24 24">' + (ICON[ic2] || ICON.map) + '</svg>'; }
  function scopeToZone() { setScope(REGION(curLens).label + ' zone', 'map'); }
  function ctxLabel() { return $('ctxScopeLbl').textContent; }
  function renderCtxExtras() { root.querySelectorAll('.ctxchip.extra').forEach((e) => e.remove()); const bar = $('ctxbar'); S.files.forEach((f, i) => { const c = document.createElement('span'); c.className = 'ctxchip file extra'; c.title = f.chars ? Math.round(f.chars / 1000) + 'k chars extracted' : ''; c.innerHTML = ico('paperclip') + esc(f.name) + ' <button>' + ico('x') + '</button>'; bar.appendChild(c); fillIcons(c); c.querySelector('button').onclick = () => { S.files.splice(i, 1); renderCtxExtras(); }; }); if (S.web) { const c = document.createElement('span'); c.className = 'ctxchip web extra'; c.innerHTML = ico('globe') + 'Web research <button>' + ico('x') + '</button>'; bar.appendChild(c); fillIcons(c); c.querySelector('button').onclick = () => { S.web = false; $('webBtn').classList.remove('on'); renderCtxExtras(); }; } }

  /* drawers — Live Artifacts is now an always-visible panel (no open/
     close state of its own, see MARKUP), only the Assistant still
     collapses/expands. */
  const askDrawer = $('askDrawer');
  function openAssistant() { askDrawer.classList.add('open'); $('askHandle').style.display = 'none'; }
  function closeAssistant() { askDrawer.classList.remove('open'); $('askHandle').style.display = 'inline-flex'; }
  $('askClose').onclick = closeAssistant; $('askHandle').onclick = openAssistant;

  // Artifact items are strings (the seeded demo set) OR provenance
  // objects {label, agent, zone, src, by, at} — every Approve since
  // Phase 2 files the object form (FR-04: provenance tag). The tooltip
  // on each row shows the full provenance.
  const artLabel = (it) => (typeof it === 'string' ? it : (it && it.label) || '');
  const artTitle = (it) => (it && typeof it === 'object'
    ? ['agent: ' + (it.agent || '—'), 'zone: ' + (it.zone || '—'), 'source: ' + (it.src || '—'), 'approved by: ' + (it.by || '—'), 'at: ' + (it.at || '—')].join('\n')
    : '');
  function renderArtifacts() { const froot = $('folders'); froot.innerHTML = ''; ARTI.forEach((f) => { const col = collapsed[f.folder] ? ' col' : ''; const d = document.createElement('div'); d.className = 'folder' + col; const items = f.items.length ? f.items.map((i) => '<div class="fitem" title="' + esc(artTitle(i)) + '"><span class="live"></span>' + esc(artLabel(i)) + '</div>').join('') : '<div class="empty">— nothing yet —</div>'; d.innerHTML = '<div class="folderhead">' + ico(f.icon) + f.folder + '<span class="cnt">' + f.items.length + '</span><span class="ic chev" data-ic="chevron"></span></div><div class="fitems">' + items + '</div>'; froot.appendChild(d); fillIcons(d); d.querySelector('.folderhead').onclick = () => { collapsed[f.folder] = !collapsed[f.folder]; renderArtifacts(); }; d.querySelectorAll('.fitem').forEach((it) => { it.onclick = () => toast('Flying to “' + it.textContent.trim() + '”'); }); }); }
  function fileArtifact(folder, item) { const f = ARTI.find((x) => x.folder === folder); if (!f) return; f.items.push(item); collapsed[folder] = false; renderArtifacts(); scheduleSave(); }

  /* thread + agents */
  const thread = $('thread');
  function scrollThread() { thread.scrollTop = thread.scrollHeight; }
  function userMsg(h) { const m = document.createElement('div'); m.className = 'msg user'; m.innerHTML = h; thread.appendChild(m); scrollThread(); }
  function botMsg(inner) { const m = document.createElement('div'); m.className = 'msg bot'; m.innerHTML = '<div class="who">' + ico('sparkles') + 'Assistant</div>' + inner; thread.appendChild(m); fillIcons(m); scrollThread(); return m; }
  function runAgent(id) { const a = A(id); if (!a) return; openAssistant(); if (a.region && a.region !== curLens && !['summarize', 'transcribe'].includes(id)) setLens(a.region); userMsg('Run <span class="cmd">/' + id + '</span> on <b>' + esc(ctxLabel()) + '</b>'); closeSlash(); input.value = ''; sizeInput(); if (id === 'sow' || id === 'roi') { askThen(id); return; } if (id === 'deepresearch') { askResearchInstruction(); return; } produce(id); }
  function askThen(id) { const q = id === 'sow' ? 'How long is the engagement?' : 'Over what horizon?'; const o = id === 'sow' ? ['6 weeks', '3 months', '6 months'] : ['12 months', '18 months', '24 months']; const m = botMsg(q + '<div class="quick">' + o.map((x) => '<button>' + x + '</button>').join('') + '</div>'); m.querySelectorAll('.quick button').forEach((b) => { b.onclick = () => { userMsg(esc(b.textContent)); m.querySelector('.quick').remove(); produce(id, b.textContent); }; }); }

  // Deep research's custom-instruction step: free text (what to research),
  // or skip for a grounded default built server-side from whatever's in
  // Prepare (see agent_catalog._default_research_query) — never a
  // meaningless generic placeholder.
  function askResearchInstruction() {
    const m = botMsg(
      'What should I research? Optional — leave blank and I’ll use a sensible default based on your Prepare documents.' +
      '<div class="research-row" style="margin-top:8px;display:flex;gap:6px">' +
      '<textarea data-research-input placeholder="e.g. how competitors handle adverse-event intake" style="flex:1;min-height:36px;max-height:100px;resize:vertical;border:1px solid var(--line2);border-radius:8px;padding:7px 9px;font:inherit;font-size:12px;outline:none"></textarea>' +
      '<button class="mini go" data-research-go>' + ico('check') + 'Research</button></div>'
    );
    const ta = m.querySelector('[data-research-input]');
    const go = m.querySelector('[data-research-go]');
    const submit = () => {
      const val = ta.value.trim();
      userMsg(val ? esc(val) : '<i>(no instruction — using a default scope)</i>');
      m.querySelector('.research-row')?.remove();
      produce('deepresearch', val || undefined);
    };
    go.onclick = submit;
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } });
    ta.focus();
  }

  // Persistent, always-visible Prepare-zone actions — the dedicated
  // upload + research entry points, placed directly on the canvas inside
  // the Prepare region (not just reachable via the toolbar/slash palette).
  function renderPrepareActions() {
    const r = baseRect.prepare;
    const el = document.createElement('div');
    el.id = 'prepareActions';
    el.style.cssText = 'position:absolute;left:' + (r.x + 20) + 'px;top:' + (r.y + 14) + 'px;display:flex;gap:8px;z-index:3;';
    el.innerHTML =
      '<button class="mini" data-act="upload-doc" style="background:#fff;box-shadow:var(--shadow-sm)" title="Upload PDFs, Word docs, spreadsheets, PowerPoint, CSV, HTML, text, or .drawio files">' + ico('upload') + 'Upload documents</button>' +
      '<button class="mini go" data-act="research" style="box-shadow:var(--shadow-sm)" title="Deep research across every Prepare document + the web">' + ico('target') + 'Research</button>';
    layer.appendChild(el); fillIcons(el);
    el.querySelector('[data-act="upload-doc"]').onclick = () => fileInput.click();
    el.querySelector('[data-act="research"]').onclick = () => { setLens('prepare'); openAssistant(); askResearchInstruction(); };
  }

  // Run zone's own persistent action bar. Scheduling a meeting
  // (Calendars.ReadWrite/People.Read) and mail import (Mail.Read) were
  // dropped: this app registration (NaviCORE) has no admin-consented
  // permission for either, the signed-in user holds no Azure AD
  // directory role to self-grant them, and Entra ID's incremental
  // consent negotiation was already poisoned by an earlier bundled
  // request into showing "Approval required" for everything regardless
  // of how narrowly later requests were scoped — confirmed live, not
  // fixable from this app's code. Only Teams browsing/import remains,
  // since it uses scopes already tenant-consented.
  function renderRunActions() {
    const r = baseRect.run;
    const el = document.createElement('div');
    el.id = 'runActions';
    el.style.cssText = 'position:absolute;left:' + (r.x + 20) + 'px;top:' + (r.y + 14) + 'px;display:flex;gap:8px;z-index:3;';
    el.innerHTML =
      '<button class="mini" data-act="teams" style="background:#fff;box-shadow:var(--shadow-sm)" title="Browse & import a Teams meeting transcript">' + ico('users') + 'Teams</button>';
    layer.appendChild(el); fillIcons(el);
    el.querySelector('[data-act="teams"]').onclick = () => { setLens('run'); openTeamsBrowser(); };
  }

  // Board summary + attachments + transcript the backend agents ground on.
  function buildContext() {
    const byZone = {};
    S.nodes.forEach((n) => { const z = n.region || '?'; const t = (n.type === 'card' ? (n.label || '') + (n.meta ? ' (' + n.meta + ')' : '') : (n.text || '')).trim(); if (t) (byZone[z] = byZone[z] || []).push(t); });
    let board = '';
    REGIONS.forEach((r) => { const items = byZone[r.id] || []; if (items.length) board += r.label + ':\n' + items.map((t) => '- ' + t.slice(0, 120)).join('\n') + '\n'; });
    return {
      zone: REGION(curLens).label,
      scope: ctxLabel(),
      board: board.slice(0, 6000),
      transcript: trLines.slice(-24),
      files: S.files.map((f) => ({ name: f.name, text: f.text })),
    };
  }
  // Phase 2: produce() is the REAL pipeline — POST /api/agents/run →
  // Flask → agent_catalog prompt → AWS Bedrock → sanitised draft JSON →
  // the same Approve/Edit/Reject card the prototype showed. The Phase 1
  // canned D-map simulation is gone.
  async function produce(id, extra) {
    const a = A(id), src = ctxLabel();
    if (id === 'transcribe') {
      // Live capture is UI state, not generation — real capture is Phase 3.
      botMsg('<div class="card2"><div class="chead">' + ico(a.icon) + 'Transcript</div><div class="cbody">Capturing continuously.</div></div>');
      return;
    }
    const m = botMsg('Draft from <b>' + esc(src) + '</b>:<div class="card2"><div class="chead">' + ico(a.icon) + esc(a.nm) + '</div><div class="cbody" style="color:var(--muted)"><span style="animation:aidc-pulse 1.3s infinite">Generating…</span></div></div>');
    let resp = null;
    try {
      const r = await fetch('/api/agents/run', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_id: id, context: buildContext(), extra: extra || null, workshop_id: WORKSHOP_ID }),
      });
      resp = await r.json();
    } catch (e) { resp = { ok: false, error: String((e && e.message) || e) }; }
    if (disposed) return;
    if (!resp || !resp.ok) {
      m.querySelector('.card2').outerHTML = '<div class="card2"><div class="chead">' + ico('alert') + 'Agent failed</div><div class="cbody">' + esc((resp && resp.error) || 'request failed') + '<br><span style="color:var(--muted);font-size:11px">Check AWS Bedrock credentials / BEDROCK_MODEL_ID in backend/.env, then try again.</span></div></div>';
      fillIcons(m); scrollThread(); return;
    }
    renderDraftCard(m, a, resp.draft, src, id);
  }

  // Inline preview of the generated end-to-end process diagrams (1-4) —
  // same visual language as the seed flow strip, one labeled section per
  // distinct process. The real artifact is the multi-page .drawio XML
  // underneath (services/drawio.py::build_drawio_multi_xml).
  function diagramPreviewHtml(diagrams) {
    return (diagrams || []).map((dg) => {
      const chips = (dg.nodes || []).map((n) => '<span title="' + esc(n.type || '') + '" style="border:1px solid var(--line2);border-radius:8px;padding:6px 10px;background:#fff;box-shadow:var(--shadow-sm);font-size:11px;color:#46505d">' + esc(n.label) + '</span>').join('<span style="color:#8aa0bd">→</span>');
      return '<div style="margin-top:9px"><div style="font-size:11.5px;font-weight:600;color:#46505d;margin-bottom:4px">' + esc(dg.title || '') + '</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">' + chips + '</div></div>';
    }).join('');
  }

  function downloadFile(name, text, mime) {
    const b = new Blob([text], { type: mime || 'application/octet-stream' });
    const u = URL.createObjectURL(b); const aEl = document.createElement('a');
    aEl.href = u; aEl.download = name; aEl.click();
    later(4000, () => URL.revokeObjectURL(u));
  }

  // Embedded draw.io editor (embed.diagrams.net, JSON protocol): load the
  // XML, let the facilitator refine it, save writes back via onSave.
  // Needs internet; a load failure just leaves the overlay closable.
  function openDrawioEditor(xml, onSave) {
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;z-index:120;background:rgba(24,39,64,.5);display:flex;align-items:center;justify-content:center';
    ov.innerHTML = '<div style="width:92vw;height:88vh;background:#fff;border-radius:12px;overflow:hidden;display:flex;flex-direction:column">' +
      '<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);font-size:12.5px"><b>draw.io editor</b><span style="color:var(--muted)">embed.diagrams.net — Save writes back into the card</span><button data-diox="1" style="margin-left:auto;font-size:15px;cursor:pointer;border:none;background:none">✕</button></div>' +
      '<iframe style="flex:1;border:0" src="https://embed.diagrams.net/?embed=1&ui=atlas&spin=1&proto=json"></iframe></div>';
    root.appendChild(ov);
    const frame = ov.querySelector('iframe');
    function onMsg(ev) {
      if (!ov.isConnected || ev.source !== frame.contentWindow) return;
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.event === 'init') frame.contentWindow.postMessage(JSON.stringify({ action: 'load', xml }), '*');
      else if (msg.event === 'save') { onSave(msg.xml); toast('Diagram saved'); frame.contentWindow.postMessage(JSON.stringify({ action: 'exit' }), '*'); }
      else if (msg.event === 'exit') ov.remove();
    }
    on(window, 'message', onMsg);   // registered via on() → removed at unmount too
    ov.querySelector('[data-diox]').onclick = () => ov.remove();
  }

  function renderDraftCard(m, a, d, src, id) {
    const kindByReg = { prepare: 'prep', run: 'workshop', synthesize: 'ba', project: 'next' };
    const folder = d.folder;
    const dest = folder ? ('Files into <b>' + esc(folder) + '</b> + a card in the <b>' + REGION(a.region).label + '</b> zone') : 'Stays in chat';
    let diagramXml = d.diagram ? d.diagram.xml : null;
    const diagramBtns = d.diagram
      ? '<button class="mini" data-dl="1">' + ico('flow') + 'Download .drawio</button><button class="mini" data-dio="1">' + ico('edit') + 'Edit in draw.io</button>'
      : '';
    // d.body_html is sanitised SERVER-side (agent_catalog.sanitize_html) —
    // the only innerHTML injection of model output in the app.
    m.querySelector('.card2').outerHTML =
      '<div class="card2"><div class="chead">' + ico(a.icon) + esc(d.title) + '</div><div class="cbody">' + d.body_html +
      (d.diagram ? diagramPreviewHtml(d.diagram.diagrams) : '') + '</div>' +
      '<div class="dest">' + ico('info') + '<span>' + dest + '</span></div>' +
      '<div class="cact"><button class="mini go" data-ap="1">' + ico('check') + 'Approve</button><button class="mini" data-ed="1">' + ico('edit') + 'Edit</button>' + diagramBtns + '<button class="mini" data-rj="1">' + ico('x') + 'Reject</button></div></div>';
    fillIcons(m); scrollThread();
    const dl = m.querySelector('[data-dl]');
    if (dl) dl.onclick = () => downloadFile((d.node.label || 'process-flow').replace(/[^\w-]+/g, '-') + '.drawio', diagramXml, 'application/xml');
    const dio = m.querySelector('[data-dio]');
    if (dio) dio.onclick = () => openDrawioEditor(diagramXml, (x) => { diagramXml = x; });
    const cbody = m.querySelector('.cbody');
    const ed = m.querySelector('[data-ed]');
    ed.onclick = () => {
      const editing = cbody.getAttribute('contenteditable') === 'true';
      if (editing) { cbody.removeAttribute('contenteditable'); cbody.style.outline = ''; ed.innerHTML = ico('edit') + 'Edit'; }
      else { cbody.setAttribute('contenteditable', 'true'); cbody.style.outline = '2px dashed var(--accent)'; cbody.focus(); ed.innerHTML = ico('check') + 'Done editing'; }
      fillIcons(ed);
    };
    m.querySelector('[data-rj]').onclick = () => {
      cbody.removeAttribute('contenteditable'); cbody.style.outline = '';
      m.querySelector('.cact').innerHTML = '<span style="font-size:11.5px;color:var(--muted);display:inline-flex;align-items:center;gap:6px">' + ico('x') + 'Rejected — nothing filed or placed</span>';
      fillIcons(m); toast('Draft rejected');
    };
    m.querySelector('[data-ap]').onclick = () => {
      cbody.removeAttribute('contenteditable'); cbody.style.outline = '';
      const r = REGION(a.region); const p = spotIn(r);
      const nodeSpec = { type: 'card', kind: kindByReg[a.region], x: p.x, y: p.y, w: 210, region: a.region, prov: 'from: ' + src, icon: d.node.icon, label: d.node.label, meta: d.node.meta, doc: d.node.doc, docId: d.node.docId };
      if (diagramXml) nodeSpec.diagram = { xml: diagramXml, diagrams: d.diagram.diagrams };
      makeNode(nodeSpec);
      recomputeRegions();
      if (folder) {
        fileArtifact(folder, {
          label: d.node.label || d.title, agent: '/' + id, zone: d.zone, src,
          by: USER.name || USER.email || 'you',
          at: new Date().toISOString().slice(0, 16).replace('T', ' '),
          // Approved content (post-edit) travels into the handoff export.
          body: cbody.innerHTML,
          ...(diagramXml ? { xml: diagramXml } : {}),
        });
      }
      m.querySelector('.cact').innerHTML = '<span style="font-size:11.5px;color:var(--green);display:inline-flex;align-items:center;gap:6px">' + ico('check') + (folder ? 'Filed in ' + folder + ' · placed in ' + REGION(a.region).label : 'Pinned') + '</span>'; fillIcons(m);
      if (a.region !== curLens) setLens(a.region); else { applyFocus(); panToRegion(curLens); }
      toast(d.node.label + ' → ' + (folder || 'chat'));
    };
  }

  /* slash + composer */
  const slash = $('slash'), input = $('input');
  function openSlash(f) { f = (f || '').toLowerCase(); const list = AGENTS.filter((a) => (a.nm + ' ' + a.ds + ' ' + a.id).toLowerCase().includes(f)); let html = '', lg = null; const lab = { prepare: 'Prepare', run: 'Run', synthesize: 'Synthesize', project: 'Project' }; if (!list.length) html = '<div class="scapt">No match — press Enter to ask.</div>'; list.forEach((a) => { if (a.region !== lg) { html += '<div class="sgroup">' + (lab[a.region] || a.region) + '</div>'; lg = a.region; } html += '<div class="sitem" data-id="' + a.id + '">' + ico(a.icon) + '<div><div class="nm">' + a.nm + '</div><div class="ds">' + a.ds + '</div></div></div>'; }); html += '<div class="scapt">' + ico('info') + ' Full catalogue · acts on ' + ctxLabel() + '</div>'; slash.innerHTML = html; fillIcons(slash); slash.querySelectorAll('.sitem').forEach((it) => { it.onclick = () => runAgent(it.dataset.id); }); const r = input.getBoundingClientRect(); slash.style.left = Math.max(8, r.left) + 'px'; slash.style.bottom = (window.innerHeight - r.top + 8) + 'px'; slash.style.display = 'block'; }
  function closeSlash() { slash.style.display = 'none'; }
  $('slashBtn').onclick = () => { if (slash.style.display === 'block') closeSlash(); else openSlash(''); input.focus(); };
  on(input, 'input', () => { sizeInput(); const v = input.value; if (v.startsWith('/')) openSlash(v.slice(1)); else closeSlash(); });
  on(input, 'keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); } if (e.key === 'Escape') closeSlash(); });
  function sizeInput() { input.style.height = '40px'; input.style.height = Math.min(120, input.scrollHeight) + 'px'; }
  async function sendMsg() {
    const v = input.value.trim(); if (!v) return; closeSlash();
    if (v.startsWith('/')) { const id = v.slice(1).trim().split(' ')[0]; if (A(id)) { runAgent(id); return; } }
    userMsg(esc(v)); input.value = ''; sizeInput();
    const m = botMsg('<span style="color:var(--muted);animation:aidc-pulse 1.3s infinite">Thinking…</span>');
    let resp = null;
    try {
      const r = await fetch('/api/agents/chat', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: v, context: buildContext(), workshop_id: WORKSHOP_ID }),
      });
      resp = await r.json();
    } catch (e) { resp = { ok: false, error: String((e && e.message) || e) }; }
    if (disposed) return;
    // Copilot dispatch: the router decided this message is really an
    // agent request — run it through the normal draft-card flow so the
    // result can be Approved (= exported onto its dashboard/zone).
    if (resp && resp.ok && resp.kind === 'dispatch') {
      const ag = A(resp.agent_id);
      m.innerHTML = '<div class="who">' + ico('sparkles') + 'Assistant</div>On it — running <b>/' + esc(resp.agent_id) + '</b>' + (ag ? ' (' + esc(ag.nm) + ')' : '') + (resp.extra ? ' with “' + esc(resp.extra) + '”' : '') + '…';
      fillIcons(m); scrollThread();
      if (ag && ag.region && ag.region !== curLens) setLens(ag.region);
      produce(resp.agent_id, resp.extra || undefined);
      return;
    }
    m.innerHTML = '<div class="who">' + ico('sparkles') + 'Assistant</div>' +
      (resp && resp.ok
        ? esc(resp.reply).replace(/\n/g, '<br>')
        : '<span style="color:var(--red)">⚠ ' + esc((resp && resp.error) || 'request failed') + '</span><br><span style="color:var(--muted);font-size:11px">Check AWS Bedrock credentials / BEDROCK_MODEL_ID in backend/.env.</span>');
    fillIcons(m); scrollThread();
  }
  $('send').onclick = sendMsg;
  $('attachBtn').onclick = () => fileInput.click();
  on(fileInput, 'change', async (e) => {
    const files = [...e.target.files]; e.target.value = '';
    for (const f of files) {
      // .drawio/.xml → import as a diagram (FR-07), not as agent context.
      if (/\.(drawio|xml)$/i.test(f.name)) {
        try {
          const xml = await f.text();
          const r = REGION('synthesize'); const p = spotIn(r);
          makeNode({ type: 'card', kind: 'ba', icon: 'flow', label: f.name.replace(/\.(drawio|xml)$/i, ''), meta: 'imported diagram', region: 'synthesize', x: p.x, y: p.y, w: 220, diagram: { xml }, prov: 'imported file' });
          fileArtifact('How it works', { label: f.name, agent: 'import', zone: 'Synthesize', src: 'file upload', by: USER.name || 'you', at: new Date().toISOString().slice(0, 16).replace('T', ' '), xml });
          recomputeRegions(); toast('Diagram imported → Synthesize · How it works');
        } catch { toast('⚠ could not read ' + f.name); }
        continue;
      }
      toast('Extracting ' + f.name + '…');
      try {
        const fd = new FormData(); fd.append('file', f); fd.append('workshop_id', WORKSHOP_ID);
        const r = await fetch('/api/agents/upload', { method: 'POST', credentials: 'same-origin', body: fd });
        const j = await r.json();
        if (disposed) return;
        if (j && j.ok) {
          // Ephemeral per-turn context (unchanged) — lets whatever agent
          // runs NEXT see this file immediately.
          S.files.push({ name: j.name, text: j.text, chars: j.chars });
          // REAL fix for the reported bug: also a permanent, visible
          // Prepare-zone node + a filed Background artifact — survives
          // reload, and (per deepresearch's server-side corpus pull)
          // grounds research regardless of what's attached this turn.
          const pr = REGION('prepare'); const p = spotIn(pr);
          makeNode({
            type: 'card', kind: 'prep', icon: 'doc-text', label: j.name,
            meta: Math.max(1, Math.round(j.chars / 1000)) + 'k chars extracted',
            region: 'prepare', x: p.x, y: p.y, w: 220, doc: 1, docId: j.doc_id,
            prov: 'uploaded by ' + (USER.name || USER.email || 'you'),
          });
          recomputeRegions();
          fileArtifact('Background', {
            label: j.name, agent: 'upload', zone: 'Prepare', src: 'file upload',
            by: USER.name || USER.email || 'you',
            at: new Date().toISOString().slice(0, 16).replace('T', ' '),
          });
          if (curLens !== 'prepare') toast(j.name + ' uploaded → ' + REGION('prepare').label + ' zone · Background');
          else toast('Uploaded ' + j.name + ' (' + Math.max(1, Math.round(j.chars / 1000)) + 'k chars)');
        } else toast('⚠ ' + ((j && j.error) || ('could not attach ' + f.name)));
      } catch { if (!disposed) toast('⚠ upload failed: ' + f.name); }
    }
    if (!disposed) renderCtxExtras();
  });
  $('webBtn').onclick = function () { S.web = !S.web; this.classList.toggle('on', S.web); renderCtxExtras(); };

  /* search */
  const searchIn = $('searchIn');
  function doSearch() { const q = searchIn.value.trim().toLowerCase(); root.querySelectorAll('.node.hit').forEach((e) => e.classList.remove('hit')); const cnt = $('searchCnt'), clr = $('searchClr'); if (q.length < 2) { cnt.textContent = ''; clr.style.display = 'none'; return; } clr.style.display = 'inline-flex'; const hits = []; layer.querySelectorAll('.node[data-txt]').forEach((el) => { if ((el.dataset.txt || '').includes(q)) { el.classList.add('hit'); hits.push(el); } }); cnt.textContent = hits.length ? hits.length + ' found' : 'none'; if (hits.length) { const el = hits[0]; const d = stageDim(); const wx = parseFloat(el.style.left) + el.offsetWidth / 2, wy = parseFloat(el.style.top) + el.offsetHeight / 2; S.zoom = Math.max(S.zoom, 0.8); S.panX = d.w / 2 - wx * S.zoom; S.panY = d.h / 2 - wy * S.zoom; layer.classList.add('anim'); later(460, () => layer.classList.remove('anim')); applyT(); } }
  on(searchIn, 'input', doSearch); on(searchIn, 'keydown', (e) => { if (e.key === 'Enter') doSearch(); });
  $('searchClr').onclick = () => { searchIn.value = ''; doSearch(); };

  /* minimap */
  const MM = { x0: 0, y0: 50 };
  function updateMinimap() { const inner = $('mmInner'); if (!inner) return; const mw = inner.clientWidth || 170, mh = inner.clientHeight || 104; const maxX = Math.max(...REGIONS.map((r) => r.x + r.w)) + 60, maxY = Math.max(...REGIONS.map((r) => r.y + r.h)) + 60; const s = Math.min(mw / (maxX - MM.x0), mh / (maxY - MM.y0)); let html = ''; REGIONS.forEach((r) => { html += '<div class="mmreg' + (r.id === curLens ? ' active' : '') + '" data-r="' + r.id + '" style="left:' + ((r.x - MM.x0) * s) + 'px;top:' + ((r.y - MM.y0) * s) + 'px;width:' + (r.w * s) + 'px;height:' + (r.h * s) + 'px">' + r.label + '</div>'; }); const d = stageDim(); const vx = -S.panX / S.zoom, vy = -S.panY / S.zoom, vw = d.w / S.zoom, vh = d.h / S.zoom; html += '<div id="mmView" style="left:' + ((vx - MM.x0) * s) + 'px;top:' + ((vy - MM.y0) * s) + 'px;width:' + (vw * s) + 'px;height:' + (vh * s) + 'px"></div>'; inner.innerHTML = html; inner.querySelectorAll('.mmreg').forEach((e) => { e.onclick = (ev) => { ev.stopPropagation(); setLens(e.dataset.r); }; }); inner._s = s; }
  on($('minimap'), 'pointerdown', (e) => { if (e.target.closest('.mmreg')) return; const inner = $('mmInner'); const b = inner.getBoundingClientRect(); const s = inner._s || 0.04; const wx = MM.x0 + (e.clientX - b.left) / s, wy = MM.y0 + (e.clientY - b.top) / s; const d = stageDim(); S.panX = d.w / 2 - wx * S.zoom; S.panY = d.h / 2 - wy * S.zoom; layer.classList.add('anim'); later(300, () => layer.classList.remove('anim')); applyT(); });

  /* facilitation + present */
  let tSec = 0, tRun = false, tT = null;
  function fmt(s) { return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); }
  $('cTimer').onclick = function () { tRun = !tRun; this.classList.toggle('on', tRun); if (tRun) { tT = every(1000, () => { tSec++; $('timerVal').textContent = fmt(tSec); }); } else { clearInterval(tT); } };
  // cRec's handler lives in the transcript section (real capture, Phase 3).
  root.querySelectorAll('.facil .chip:not(#cTimer):not(#cRec):not(#cPresent):not(#teamsBtn)').forEach((c) => { c.onclick = () => c.classList.toggle('on'); });
  // Honest: a structured "Page" layout mode isn't built — the canvas is
  // Edgeless-only today. Selecting "Page" says so instead of pretending.
  $('mEdgeless').onclick = function () { this.classList.add('on'); $('mPage').classList.remove('on'); };
  $('mPage').onclick = function () { toast('Page (structured) layout isn\'t built yet — staying in Edgeless'); };
  let pIdx = 0;
  function showPresent() { const r = REGIONS[pIdx]; $('pName').textContent = r.label; $('pStep').textContent = (pIdx + 1) + ' of ' + REGIONS.length; curLens = r.id; syncRegionEls(); panToRegion(r.id); }
  $('cPresent').onclick = () => { root.classList.add('present'); positionNodeBar(); pIdx = Math.max(0, REGIONS.findIndex((r) => r.id === curLens)); later(60, showPresent); };
  $('pExit').onclick = () => { root.classList.remove('present'); later(60, () => panToRegion(curLens)); };
  $('pPrev').onclick = () => { pIdx = (pIdx - 1 + REGIONS.length) % REGIONS.length; showPresent(); };
  $('pNext').onclick = () => { pIdx = (pIdx + 1) % REGIONS.length; showPresent(); };

  // Real date (today), real elapsed-since-this-tab-was-opened clock (resets
  // on refresh — honest about there being no persisted "session start"
  // concept yet, rather than faking a plausible-looking number).
  $('sessDate').textContent = new Date().toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  $('sessWho').textContent = USER.name || USER.email || 'you';
  let ssec = 0; every(1000, () => { ssec++; $('sessClock').textContent = fmt(ssec); });

  // Real signed-in user's avatar (initials), not fabricated collaborators —
  // there's no real multi-facilitator presence system yet (see Share above).
  (function renderRealAvatar() {
    const label = (USER.name || USER.email || '?').trim();
    const initials = label.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('') || '?';
    const box = $('avatarsBox');
    box.innerHTML = '<div class="av" title="' + esc(label) + ' (you)">' + esc(initials) + '</div>';
  })();

  // Board name: editable, persisted — no fictional client name pretending
  // to be a real engagement.
  const boardNameText = $('boardNameText');
  let boardName = 'Untitled Engagement';
  function setBoardName(n) { boardName = (n || '').trim() || 'Untitled Engagement'; boardNameText.textContent = boardName; }
  on($('boardNameBox'), 'dblclick', () => {
    const el = boardNameText; el.setAttribute('contenteditable', 'true'); el.style.outline = '1px dashed var(--accent)';
    const sel = window.getSelection(); const range = document.createRange(); range.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(range); el.focus();
    const commit = () => { el.removeAttribute('contenteditable'); el.style.outline = ''; setBoardName(el.textContent); scheduleSave(); el.removeEventListener('blur', commit); };
    el.addEventListener('blur', commit);
    el.onkeydown = (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); el.blur(); } };
  });

  /* transcript — Phase 3: REAL capture. The Phase 1/2 fake ticker is
     gone. Two live sources feed the dock: (1) the REC chip toggles
     browser speech recognition (Web Speech API — Chrome/Edge; a clear
     toast explains when unsupported), (2) the Teams button pulls a
     finished meeting's transcript via Microsoft Graph (device-code
     sign-in on NaviCore's Entra app registration). Lines persist with
     the board and feed every agent/chat call via buildContext(). */
  const tbody = $('tbody');
  const trLines = [];
  function addTrLine(who, text, persist = true) {
    text = String(text || '').trim(); if (!text) return;
    const line = (who ? who + ': ' : '') + text;
    trLines.push(line); if (trLines.length > 400) trLines.shift();
    const d = document.createElement('div'); d.className = 'tline';
    d.innerHTML = (who ? '<b>' + esc(who) + ':</b> ' : '') + esc(text);
    tbody.appendChild(d); tbody.scrollTop = tbody.scrollHeight;
    $('trNow').textContent = line;
    while (tbody.children.length > 6) tbody.removeChild(tbody.firstChild);
    if (persist) scheduleSave();
    syncTranscriptVisibility();
  }
  $('trNow').textContent = 'idle — press REC to capture, or pull a Teams transcript';

  // -- in-app microphone capture (Web Speech API) --
  let recog = null, recOn = false;
  function startRec() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { toast('⚠ No speech recognition in this browser — use Chrome/Edge, or pull a Teams transcript'); return false; }
    recog = new SR(); recog.continuous = true; recog.interimResults = true; recog.lang = 'en-US';
    recog.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) addTrLine(USER.name || 'You', r[0].transcript);
        else $('trNow').textContent = '… ' + r[0].transcript.trim();
      }
    };
    recog.onerror = (e) => { if (e.error === 'not-allowed' || e.error === 'service-not-allowed') { toast('⚠ Microphone permission denied'); recOn = false; stopRec(); syncRecChip(); } };
    // Chrome stops recognition every ~60s of silence — auto-restart while armed.
    recog.onend = () => { if (recOn && recog) { try { recog.start(); } catch { /* already starting */ } } };
    try { recog.start(); } catch { return false; }
    return true;
  }
  function stopRec() { const r = recog; recog = null; if (r) { try { r.onend = null; r.stop(); } catch { } } }
  function syncRecChip() { const c = $('cRec'); c.classList.toggle('on', recOn); c.querySelector('.recdot').classList.toggle('live', recOn); root.querySelector('#transcript .thead .live')?.classList.toggle('on', recOn); syncTranscriptVisibility(); }
  $('cRec').onclick = () => {
    if (!recOn) { if (!startRec()) return; recOn = true; toast('Recording — live transcription on'); }
    else { recOn = false; stopRec(); toast('Recording stopped'); $('trNow').textContent = 'idle — press REC to capture, or pull a Teams transcript'; }
    syncRecChip();
  };

  // The dock only appears once there's something real to show — actively
  // recording, or transcript lines already exist (from mic capture or a
  // Teams pull). It does NOT show just because you're in the Run zone;
  // sitting there as unused chrome when nothing is happening was exactly
  // the complaint.
  function syncTranscriptVisibility() {
    $('transcript').classList.toggle('visible', recOn || trLines.length > 0);
  }

  // -- Microsoft Teams transcript pull --
  // Automatic path: if you signed in with Microsoft (not mock auth), reuse
  // that SAME MSAL session for Graph — no separate sign-in step. Falls
  // back to the manual device-code flow only when there's no Microsoft
  // account to reuse (mock auth) or the silent/popup token grab fails.
  //
  // ONLY the scopes already tenant-consented for the NaviCORE app
  // registration belong here (verified live via `az ad app permission
  // list-grants`: OnlineMeetings.Read, OnlineMeetingTranscript.Read.All
  // and Calendars.Read are the only Graph scopes ever admin-approved).
  // Meeting scheduling (Calendars.ReadWrite/People.Read) and Outlook mail
  // import (Mail.Read) were tried and dropped: even though Microsoft
  // classifies those as self-consentable "User"-type permissions, this
  // app's very first (bundled) incremental-consent attempt included the
  // Admin-only OnlineMeetingRecording.Read.All, and Entra ID's consent
  // negotiation for this user+app pair now permanently shows "Approval
  // required" for everything regardless of how narrowly later requests
  // were scoped — confirmed live, not fixable from this app's code.
  const TEAMS_SCOPES = ['OnlineMeetings.Read', 'OnlineMeetingTranscript.Read.All', 'Calendars.Read'];

  async function connectTeamsAutomatically() {
    const msal = getMsalInstance();
    if (!msal) return { ok: false, reason: 'no-msal' };
    await msal.ready;
    const accounts = msal.instance.getAllAccounts();
    if (!accounts.length) return { ok: false, reason: 'no-account' };
    const account = accounts[0];
    let result;
    try {
      result = await msal.instance.acquireTokenSilent({ scopes: TEAMS_SCOPES, account });
    } catch {
      try { result = await msal.instance.acquireTokenPopup({ scopes: TEAMS_SCOPES, account }); }
      catch (e2) { return { ok: false, reason: 'consent-failed', error: e2?.message || String(e2) }; }
    }
    const expiresIn = result.expiresOn ? Math.max(60, Math.floor((result.expiresOn.getTime() - Date.now()) / 1000)) : 3300;
    let j = null;
    try {
      j = await (await fetch('/api/integrations/teams/connect-token', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ access_token: result.accessToken, expires_in: expiresIn }),
      })).json();
    } catch { }
    if (!j || !j.ok) return { ok: false, reason: 'backend-rejected', error: j && j.error };
    return { ok: true, account: j.account };
  }
  async function connectTeamsManually() {
    let j = null;
    try { j = await (await fetch('/api/integrations/teams/connect', { method: 'POST', credentials: 'same-origin' })).json(); } catch { }
    if (!j || !j.ok) { botMsg('<span style="color:var(--red)">⚠ ' + esc((j && j.error) || 'could not start Microsoft sign-in') + '</span>'); return false; }
    const m = botMsg('<div class="card2"><div class="chead">' + ico('users') + 'Connect Microsoft Teams</div><div class="cbody">Open <b>' + esc(j.verification_uri) + '</b> and enter code <b>' + esc(j.user_code) + '</b>, then sign in with your Microsoft 365 account.<br><span style="color:var(--muted)" data-poll="1">Waiting for sign-in…</span></div></div>');
    let connected = false;
    for (let tick = 0; tick < 90 && !connected && !disposed; tick++) {
      await new Promise((res) => setTimeout(res, 5000));
      let p = null;
      try { p = await (await fetch('/api/integrations/teams/poll', { method: 'POST', credentials: 'same-origin' })).json(); } catch { }
      const el = m.querySelector('[data-poll]');
      if (p && p.status === 'connected') { connected = true; if (el) el.innerHTML = '<span style="color:var(--green)">Connected as ' + esc(p.account || 'your account') + ' ✓</span>'; toast('Teams connected'); }
      else if (p && p.status === 'error') { if (el) el.innerHTML = '<span style="color:var(--red)">Sign-in failed: ' + esc(p.error || '') + '</span>'; return false; }
    }
    return connected;
  }
  async function importTeamsTranscript(url, organizer) {
    const m2 = botMsg('<span style="color:var(--muted);animation:aidc-pulse 1.3s infinite">Fetching Teams transcript…</span>');
    let t = null;
    // organizer (email, from the calendar listing) enables the backend's
    // app-only fallback lookup for meetings someone ELSE organized and you
    // were only invited to — delegated Graph access can never resolve
    // those by join URL alone. Omitted (undefined) for a manually pasted
    // URL, where there's no calendar entry to read it from.
    try { t = await (await fetch('/api/integrations/teams/transcript', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ join_url: url, organizer: organizer || undefined }) })).json(); } catch { }
    if (disposed) return;
    if (!t || !t.ok) { m2.innerHTML = '<div class="who">' + ico('sparkles') + 'Assistant</div><span style="color:var(--red)">⚠ ' + esc((t && t.error) || 'request failed') + '</span>'; fillIcons(m2); scrollThread(); return; }
    t.lines.forEach((l) => { const ix = l.indexOf(': '); if (ix > 0) addTrLine(l.slice(0, ix), l.slice(ix + 2), false); else addTrLine('', l, false); });
    scheduleSave();
    m2.innerHTML = '<div class="who">' + ico('sparkles') + 'Assistant</div>Imported <b>' + t.lines.length + '</b> transcript lines from “' + esc(t.meeting_subject) + '” — now run <b>/summarize</b>, <b>/decisions</b> or <b>/mom</b> on them.';
    fillIcons(m2); scrollThread(); toast('Teams transcript imported');
  }
  function askTeamsJoinUrlManually() {
    const url = window.prompt('Paste the Teams meeting join URL:'); if (!url) return;
    importTeamsTranscript(url);
  }
  // Browse the signed-in user's own calendar (Graph /me/calendarView, last
  // 30 days + next day, Teams-online-meetings only) instead of requiring
  // them to already have the join link — falls back to the manual paste
  // for meetings outside that window or that Graph didn't return.
  async function browseTeamsMeetings() {
    let j = null;
    try { j = await (await fetch('/api/integrations/teams/meetings', { credentials: 'same-origin' })).json(); } catch { }
    const meetings = (j && j.ok && Array.isArray(j.meetings)) ? j.meetings : [];
    if (!meetings.length) {
      botMsg((j && j.ok ? 'No upcoming Teams meetings found in your calendar. ' : '⚠ ' + esc((j && j.error) || 'could not load your calendar') + ' ') + 'Paste a join URL instead.');
      askTeamsJoinUrlManually();
      return;
    }
    const rows = meetings.map((mtg, i) => {
      const when = mtg.start ? new Date(mtg.start).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';
      return '<div class="cl" data-mtg="' + i + '" style="cursor:pointer"><span class="cb">' + ico('calendar') + '</span>' + esc(mtg.subject) + '<span style="color:var(--muted);margin-left:8px">' + esc(when) + '</span></div>';
    }).join('');
    const m = botMsg(
      '<div class="card2"><div class="chead">' + ico('calendar') + 'Browse your Teams meetings</div>' +
      '<div class="cbody">' + rows +
      '<div class="cl" data-mtg="paste" style="cursor:pointer;border-top:1px solid var(--line);margin-top:6px;padding-top:8px">' +
      '<span class="cb">' + ico('paperclip') + '</span><i>Paste a join URL instead…</i></div>' +
      '</div></div>'
    );
    m.querySelectorAll('[data-mtg]').forEach((row) => {
      row.onclick = () => {
        const key = row.getAttribute('data-mtg');
        if (key === 'paste') { askTeamsJoinUrlManually(); return; }
        importTeamsTranscript(meetings[Number(key)].join_url, meetings[Number(key)].organizer);
      };
    });
  }
  // Shared connect gate for every Teams/Graph entry point (global chip AND
  // the three Run-zone buttons) — status check, then automatic MSAL-reuse
  // with device-code fallback, exactly as the global chip always did.
  async function ensureTeamsConnected() {
    let st = null;
    try { st = await (await fetch('/api/integrations/teams/status', { credentials: 'same-origin' })).json(); } catch { }
    if (!st) { botMsg('<span style="color:var(--red)">⚠ backend unreachable</span>'); return false; }
    if (!st.configured) { botMsg('⚠ Teams is not configured — set <b>TEAMS_TENANT_ID</b> / <b>TEAMS_CLIENT_ID</b> in backend/.env (they default to the NaviCore Entra app registration).'); return false; }
    if (st.connected) return true;
    const auto = await connectTeamsAutomatically();
    if (auto.ok) { toast('Teams connected automatically as ' + (auto.account || 'you')); return true; }
    if (auto.reason === 'no-account') {
      botMsg('You’re signed in with mock auth, so Teams can’t connect automatically — sign in with Microsoft on the login page for one-click Teams access, or use manual sign-in below.');
    } else if (auto.reason === 'consent-failed') {
      botMsg('⚠ Microsoft declined the Teams permission request (' + esc(auto.error || '') + ') — falling back to manual sign-in.');
    } else {
      botMsg('⚠ ' + esc(auto.error || 'automatic Teams connection failed') + ' — falling back to manual sign-in.');
    }
    return await connectTeamsManually();
  }
  $('teamsBtn').onclick = async () => {
    openAssistant();
    if (!(await ensureTeamsConnected())) return;
    await browseTeamsMeetings();
  };

  // Run zone's "Teams" button: calendar-window navigation, subject search,
  // and "has transcript"/"has recording" filters (checked lazily, only for
  // whatever page of meetings is currently on screen — see
  // check_meeting_availability in the backend). Picking a row imports its
  // transcript exactly like the global chip already does.
  async function openTeamsBrowser() {
    openAssistant();
    if (!(await ensureTeamsConnected())) return;
    const now = new Date();
    const state = {
      start: new Date(now.getTime() - 14 * 86400000), end: new Date(now.getTime() + 86400000),
      q: '', filterTranscript: false,
      all: [], visible: [], availability: {},
    };
    openGraphModal('Browse Teams meetings', 'calendar');
    const body = $('graphModalBody');

    function recomputeVisible() {
      if (!state.filterTranscript) { state.visible = state.all; return; }
      state.visible = state.all.filter((m) => {
        const av = state.availability[m.join_url] || {};
        if (av.error) return true;
        return !!av.has_transcript;
      });
    }

    async function load() {
      body.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">Loading…</div>';
      const params = new URLSearchParams({ start: state.start.toISOString(), end: state.end.toISOString() });
      if (state.q) params.set('q', state.q);
      let j = null;
      try { j = await (await fetch('/api/integrations/teams/meetings?' + params.toString(), { credentials: 'same-origin' })).json(); } catch { }
      state.all = (j && j.ok && Array.isArray(j.meetings)) ? j.meetings : [];
      recomputeVisible();
      render(j && !j.ok ? (j.error || 'could not load meetings') : null);
    }

    function fmtRange() {
      const o = { month: 'short', day: 'numeric' };
      return state.start.toLocaleDateString(undefined, o) + ' – ' + state.end.toLocaleDateString(undefined, o);
    }

    function render(err) {
      const rows = state.visible.map((mtg, i) => {
        const when = mtg.start ? new Date(mtg.start).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';
        return '<div class="grow" data-mtg="' + i + '">' + ico('calendar') + '<div style="flex:1"><div>' + esc(mtg.subject) + '</div><div style="color:var(--muted);font-size:11px">' + esc(when) + '</div></div></div>';
      }).join('') || '<div style="color:var(--muted);padding:12px 0">No meetings in this window' + (state.q ? ' matching “' + esc(state.q) + '”' : '') + '.</div>';
      body.innerHTML =
        '<div class="gnav"><button class="mini" data-nav="prev">' + ico('caretL') + '</button>' +
        '<span style="font-weight:700;font-size:12.5px;white-space:nowrap">' + esc(fmtRange()) + '</span>' +
        '<button class="mini" data-nav="next">' + ico('caretR') + '</button>' +
        '<input type="text" placeholder="Search by subject…" value="' + esc(state.q) + '" data-q="1" /></div>' +
        '<div class="gfilters"><button class="gchip' + (state.filterTranscript ? ' on' : '') + '" data-f="transcript">Has transcript</button></div>' +
        (err ? '<div style="color:var(--red);font-size:12px;margin-bottom:8px">⚠ ' + esc(err) + '</div>' : '') +
        '<div class="glist">' + rows + '</div>' +
        '<div class="cl" data-mtg="paste" style="cursor:pointer;margin-top:6px"><span class="cb">' + ico('paperclip') + '</span><i>Paste a join URL instead…</i></div>';
      fillIcons(body);
      body.querySelector('[data-nav="prev"]').onclick = () => shiftWindow(-14);
      body.querySelector('[data-nav="next"]').onclick = () => shiftWindow(14);
      let qT = null;
      body.querySelector('[data-q]').oninput = (e) => { clearTimeout(qT); qT = setTimeout(() => { state.q = e.target.value; load(); }, 350); };
      body.querySelectorAll('[data-f]').forEach((btn) => { btn.onclick = () => applyFilter(); });
      body.querySelectorAll('[data-mtg]').forEach((row) => {
        row.onclick = () => {
          const key = row.getAttribute('data-mtg');
          closeGraphModal();
          if (key === 'paste') { askTeamsJoinUrlManually(); return; }
          importTeamsTranscript(state.visible[Number(key)].join_url, state.visible[Number(key)].organizer);
        };
      });
    }

    function shiftWindow(days) {
      state.start = new Date(state.start.getTime() + days * 86400000);
      state.end = new Date(state.end.getTime() + days * 86400000);
      load();
    }

    async function applyFilter() {
      state.filterTranscript = !state.filterTranscript;
      if (state.filterTranscript) {
        const need = state.all.filter((m) => !(m.join_url in state.availability));
        if (need.length) {
          body.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">Checking availability…</div>';
          let j = null;
          try {
            j = await (await fetch('/api/integrations/teams/meetings/availability', {
              method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
              // Backend expects {meetings:[{join_url, organizer?}]} (see
              // routes/integrations.py) — a bare join_urls array was
              // silently ignored server-side (body.get('meetings') on a
              // payload that only had 'join_urls' → always []), so this
              // filter never actually returned any results before.
              body: JSON.stringify({ meetings: need.map((m) => ({ join_url: m.join_url, organizer: m.organizer })) }),
            })).json();
          } catch { }
          Object.assign(state.availability, (j && j.ok && j.results) || {});
        }
      }
      recomputeVisible();
      render();
    }

    load();
  }

  on($('trHead'), 'click', (e) => { if (e.target.closest('#summBtn')) return; $('transcript').classList.toggle('open'); });
  $('summBtn').onclick = () => { setLens('run'); openAssistant(); runAgent('summarize'); };

  /* handoff modal */
  function openHandoff() { $('handoffList').innerHTML = ['Process flows', 'User stories', 'Acceptance criteria', 'Documentation & manuals', 'Minutes of Meeting', 'Proposal (SOW · ROI · risk)'].map((x) => '<div class="cl"><span class="cb">' + ico('check') + '</span>' + x + '</div>').join('') + '<div class="cl ph2"><span class="cb"></span>ADO-importable JSON <span style="font-style:italic;margin-left:6px">(Phase 2)</span></div>'; fillIcons($('handoffList')); $('modalWrap').classList.add('on'); }
  $('handoffBtn').onclick = openHandoff; $('exportTop').onclick = openHandoff;
  $('modalX').onclick = () => $('modalWrap').classList.remove('on');
  on($('modalWrap'), 'click', (e) => { if (e.target.id === 'modalWrap') e.currentTarget.classList.remove('on'); });

  /* generic Graph browse modal, used by the Run-zone Teams browser
     (openTeamsBrowser) — swaps #graphModalBody's content on open. */
  function openGraphModal(title, icon) {
    $('graphModalTitle').textContent = title;
    $('graphModalIcon').setAttribute('data-ic', icon);
    fillIcons($('graphModalWrap'));
    $('graphModalWrap').classList.add('on');
  }
  function closeGraphModal() { $('graphModalWrap').classList.remove('on'); }
  $('graphModalX').onclick = closeGraphModal;
  on($('graphModalWrap'), 'click', (e) => { if (e.target.id === 'graphModalWrap') closeGraphModal(); });
  // Real export (Phase 4, FR-11): the backend assembles the package from
  // the live artifact library. DOCX and Markdown are live; PDF is still a
  // deliberate placeholder.
  let selFmt = 'docx';
  function syncFmtChips() { $('fmtDocx').classList.toggle('on', selFmt === 'docx'); $('fmtMd').classList.toggle('on', selFmt === 'md'); $('fmtPdf').classList.remove('on'); }
  $('fmtDocx').onclick = () => { selFmt = 'docx'; syncFmtChips(); };
  $('fmtMd').onclick = () => { selFmt = 'md'; syncFmtChips(); };
  $('fmtPdf').onclick = () => toast('PDF export is coming later — DOCX and Markdown are available');
  $('exportPkg').onclick = async () => {
    const btn = $('exportPkg'); btn.disabled = true;
    try {
      const r = await fetch('/api/export/handoff', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ format: selFmt, board_name: boardName, artifacts: ARTI }),
      });
      const ct = r.headers.get('content-type') || '';
      if (ct.includes('json')) { const j = await r.json(); throw new Error((j && j.error) || ('HTTP ' + r.status)); }
      const blob = await r.blob();
      const u = URL.createObjectURL(blob); const aEl = document.createElement('a');
      aEl.href = u; aEl.download = 'handoff-package.' + (selFmt === 'md' ? 'md' : 'docx'); aEl.click();
      later(4000, () => URL.revokeObjectURL(u));
      $('modalWrap').classList.remove('on'); toast('Handoff package exported (' + selFmt.toUpperCase() + ')');
    } catch (e2) { toast('⚠ ' + ((e2 && e2.message) || 'export failed')); }
    btn.disabled = false;
  };

  /* tooltip / popover / toast */
  const tip = $('tooltip');
  on(root, 'mouseover', (e) => { const t = e.target.closest('[data-tip]'); if (!t) return; tip.textContent = t.getAttribute('data-tip'); tip.style.display = 'block'; const r = t.getBoundingClientRect(); tip.style.left = Math.min(window.innerWidth - 250, Math.max(8, r.left)) + 'px'; let top = r.bottom + 8; if (top > window.innerHeight - 50) top = r.top - tip.offsetHeight - 8; tip.style.top = top + 'px'; });
  on(root, 'mouseout', (e) => { if (e.target.closest('[data-tip]')) tip.style.display = 'none'; });
  const pop = $('popover');
  root.querySelectorAll('[data-info]').forEach((b) => on(b, 'click', (e) => { e.stopPropagation(); pop.innerHTML = '<div class="pt">' + ico('info') + b.getAttribute('data-info-title') + '</div>' + b.getAttribute('data-info-text'); fillIcons(pop); pop.style.display = 'block'; const r = b.getBoundingClientRect(); pop.style.left = Math.min(window.innerWidth - 310, Math.max(8, r.left - 120)) + 'px'; pop.style.top = Math.min(window.innerHeight - 160, r.bottom + 8) + 'px'; }));
  on(root, 'click', (e) => { if (!e.target.closest('[data-info]') && !e.target.closest('#popover')) pop.style.display = 'none'; if (!e.target.closest('#slash') && !e.target.closest('#slashBtn') && !e.target.closest('#input')) closeSlash(); });
  let toT = null;
  function toast(msg) { const t = $('toast'); t.innerHTML = ico('check') + msg; fillIcons(t); t.classList.add('show'); clearTimeout(toT); toT = later(2300, () => t.classList.remove('show')); }

  const WELCOME_HTML = 'Welcome 👋 One infinite canvas — Prepare, Run, Synthesize, Project are <b>places on it</b>, and I’m sensitive to <b>where you are</b>. This board starts empty: upload a document, paste a Teams transcript, or pick a <b>Suggested next</b> action below to generate your first real draft. Double-click the board name (top left) to rename this engagement.';

  /* Top menu bar (File/Edit/View/Insert/Tools/Help) — every item below
     dispatches to a capability that already exists elsewhere in this
     file (upload, export, node edit/duplicate/delete, zoom, tool
     switching, the agent catalogue, Teams, timer/record, ...); nothing
     here is a new feature, just a real menu on top of what already works. */
  const MENUS = {
    file: [
      { label: 'Upload documents…', icon: 'upload', action: 'upload' },
      { label: 'Import .drawio / .xml…', icon: 'flow', action: 'upload' },
      { sep: true },
      { label: 'Export / Handoff…', icon: 'upload', action: 'export' },
      { sep: true },
      { label: 'Rename this workshop', icon: 'edit', action: 'rename-board' },
      { label: 'Back to Projects', icon: 'folder', action: 'back-projects' },
    ],
    edit: [
      { label: 'Duplicate node', icon: 'copy', action: 'dup', shortcut: '⌘D', needsSel: true },
      { label: 'Rename / edit text', icon: 'edit', action: 'edit-node', needsSel: true },
      { label: 'Delete node', icon: 'trash', action: 'del', shortcut: 'Del', needsSel: true },
    ],
    view: [
      { label: 'Zoom in', icon: 'plus', action: 'zoom-in' },
      { label: 'Zoom out', icon: 'minus', action: 'zoom-out' },
      { label: 'Zoom to fit', icon: 'maximize', action: 'zoom-fit' },
      { sep: true },
      { label: 'Focus mode', icon: 'target', action: 'focus' },
      { label: 'Present', icon: 'play', action: 'present' },
      { sep: true },
      { label: 'Search canvas', icon: 'search', action: 'search' },
      { label: 'Assistant', icon: 'sparkles', action: 'assistant' },
      { sep: true },
      { label: 'Go to Pre-Workshop', icon: 'database', action: 'lens:prepare' },
      { label: 'Go to During Workshop', icon: 'play', action: 'lens:run' },
      { label: 'Go to Post-Workshop', icon: 'list', action: 'lens:synthesize' },
      { label: 'Go to Proposal & Planning', icon: 'dollar', action: 'lens:project' },
    ],
    insert: [
      { label: 'Sticky note', icon: 'sticky', action: 'tool:sticky' },
      { label: 'Rectangle', icon: 'square', action: 'tool:rect' },
      { label: 'Ellipse', icon: 'circle', action: 'tool:ellipse' },
      { label: 'Text', icon: 'text', action: 'tool:text' },
      { label: 'Connector', icon: 'connector', action: 'tool:conn' },
      { label: 'Frame', icon: 'frame', action: 'tool:frame' },
      { sep: true },
      { label: 'Upload document…', icon: 'upload', action: 'upload' },
    ],
    tools: [
      { label: 'Ask the Assistant', icon: 'sparkles', action: 'assistant' },
      { label: 'Browse agent catalogue', icon: 'list', action: 'agents' },
      { sep: true },
      { label: 'Connect Teams…', icon: 'users', action: 'teams' },
      { sep: true },
      { label: 'Timer', icon: 'clock', action: 'timer' },
      { label: 'Record', icon: 'mic', action: 'rec' },
    ],
    help: [
      { label: 'Keyboard shortcuts', icon: 'info', action: 'help-shortcuts' },
      { label: 'About this workspace', icon: 'info', action: 'help-about' },
      { label: 'Replay welcome message', icon: 'sparkles', action: 'help-welcome' },
    ],
  };

  function showShortcutsHelp() {
    openGraphModal('Keyboard shortcuts', 'info');
    $('graphModalBody').innerHTML = [
      ['⌘ / Ctrl + D', 'Duplicate the selected node'],
      ['Delete / Backspace', 'Delete the selected node'],
      ['Enter', 'Commit a rename, or send a search / chat message'],
      ['Escape', 'Close the slash-command or menu popup'],
      ['Scroll / pinch', 'Zoom the canvas'],
    ].map(([k, d]) => '<div class="cl"><span class="cb" style="width:auto;padding:2px 7px;font-family:ui-monospace,Menlo,monospace;font-size:10.5px">' + esc(k) + '</span>' + esc(d) + '</div>').join('');
  }
  function showAboutHelp() {
    openGraphModal('About this workspace', 'info');
    $('graphModalBody').innerHTML =
      '<p style="font-size:12.5px;line-height:1.6;color:#3a4350">One infinite canvas — <b>Prepare</b>, <b>Run</b>, ' +
      '<b>Synthesize</b>, <b>Project</b> are places on it, not separate pages. The Assistant is sensitive to where ' +
      'you are and what\'s selected. Everything an agent approves is filed into <b>Live Artifacts</b> and placed ' +
      'as a card in its zone.</p><p style="font-size:12.5px;line-height:1.6;color:var(--muted);margin-top:10px">' +
      'Not yet built: real-time multi-facilitator presence (Share), Vote, and the structured Page layout — Edgeless ' +
      'is the only real layout today.</p>';
  }

  function menuAction(action) {
    closeMenuDrop();
    if (action.startsWith('tool:')) { setTool(action.slice(5)); return; }
    if (action.startsWith('lens:')) { setLens(action.slice(5)); return; }
    switch (action) {
      case 'upload': fileInput.click(); break;
      case 'export': openHandoff(); break;
      case 'rename-board': $('boardNameBox').dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); break;
      case 'back-projects': window.location.href = '/projects/' + PROJECT_ID; break;
      case 'dup': if (S.sel) dupNode(S.sel); break;
      case 'edit-node': { if (S.sel) { const n = S.nodes.find((x) => x.id === S.sel); if (n) startEdit(n); } break; }
      case 'del': if (S.sel) delNode(S.sel); break;
      case 'zoom-in': $('zIn').click(); break;
      case 'zoom-out': $('zOut').click(); break;
      case 'zoom-fit': fitAll(); break;
      case 'focus': $('focusChip').click(); break;
      case 'present': $('cPresent').click(); break;
      case 'search': $('searchIn').focus(); break;
      case 'assistant': openAssistant(); break;
      case 'agents': openAssistant(); openSlash(''); break;
      case 'teams': $('teamsBtn').click(); break;
      case 'timer': $('cTimer').click(); break;
      case 'rec': $('cRec').click(); break;
      case 'help-shortcuts': showShortcutsHelp(); break;
      case 'help-about': showAboutHelp(); break;
      case 'help-welcome': openAssistant(); botMsg(WELCOME_HTML); break;
      default: break;
    }
  }

  const menuDrop = $('menuDrop');
  function closeMenuDrop() {
    menuDrop.style.display = 'none';
    root.querySelectorAll('.menubar .mi.open').forEach((b) => b.classList.remove('open'));
    menuDrop.dataset.open = '';
  }
  function openMenu(which, btn) {
    if (menuDrop.dataset.open === which) { closeMenuDrop(); return; }
    closeMenuDrop();
    menuDrop.innerHTML = MENUS[which].map((it) => {
      if (it.sep) return '<div class="msep"></div>';
      const disabled = it.needsSel && !S.sel;
      return '<div class="mitem' + (disabled ? ' disabled' : '') + '" data-action="' + it.action + '">' +
        ico(it.icon) + '<span class="mlabel">' + esc(it.label) + '</span>' +
        (it.shortcut ? '<span class="mshortcut">' + esc(it.shortcut) + '</span>' : '') + '</div>';
    }).join('');
    fillIcons(menuDrop);
    menuDrop.querySelectorAll('.mitem:not(.disabled)').forEach((el) => { el.onclick = () => menuAction(el.dataset.action); });
    const r = btn.getBoundingClientRect();
    menuDrop.style.left = Math.min(window.innerWidth - 240, r.left) + 'px';
    menuDrop.style.top = (r.bottom + 4) + 'px';
    menuDrop.style.display = 'block';
    btn.classList.add('open');
    menuDrop.dataset.open = which;
  }
  ['File', 'Edit', 'View', 'Insert', 'Tools', 'Help'].forEach((label) => {
    const btn = $('mi' + label);
    btn.onclick = (e) => { e.stopPropagation(); openMenu(label.toLowerCase(), btn); };
  });
  on(root, 'click', (e) => { if (!e.target.closest('.menudrop') && !e.target.closest('.mi')) closeMenuDrop(); });
  on(document, 'keydown', (e) => { if (e.key === 'Escape') closeMenuDrop(); });

  /* resize */
  on(window, 'resize', () => panToRegion(curLens, false));

  /* init */
  if (PROJECT_ID) $('backProjects').href = '/projects/' + PROJECT_ID;
  fillIcons(root); renderRegions(); renderPrepareActions(); renderRunActions(); renderArtifacts(); renderLens(); renderSuggest(); scopeToZone(); drawEdges(); panToRegion(curLens, false); syncTranscriptVisibility();
  $('askHandle').style.display = 'none'; /* Assistant open by default */
  botMsg(WELCOME_HTML);

  // Load the persisted board — this is a REAL engagement, not a demo: an
  // empty board on first visit stays empty (no fictional seed content).
  // Only genuinely missing/unreachable-backend cases fall through silently
  // (the canvas still fully works locally, it just won't persist yet).
  let disposed = false;
  fetch('/api/canvas/board?workshop_id=' + WORKSHOP_ID, { credentials: 'same-origin' })
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (disposed) return;
      const board = data && data.board;
      if (board && Array.isArray(board.nodes) && board.nodes.length) hydrateBoard(board);
      // Always pick up the workshop's chosen name, even when the board is
      // otherwise empty (a brand-new workshop has no nodes to hydrate but
      // was still named at creation time — see app/routes/projects.py).
      else if (board && board.board_name) setBoardName(board.board_name);
      panToRegion(curLens, false);
    })
    .catch(() => { if (!disposed) panToRegion(curLens, false); });

  // ── Cleanup ──────────────────────────────────────────────────────────
  return function cleanup() {
    disposed = true;
    docListeners.forEach(([t, ty, fn, o]) => t.removeEventListener(ty, fn, o));
    intervals.forEach(clearInterval);
    timeouts.forEach(clearTimeout);
    clearTimeout(saveT); clearInterval(tT); clearTimeout(animT); clearTimeout(toT);
    root.classList.remove('present');
    root.innerHTML = '';
  };
}
