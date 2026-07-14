"""
draw.io (diagrams.net) integration — Phase 4.

The "Draw process flow" agent produces an ordered step list (the model's
job); THIS module turns that list into a native ``.drawio`` file
deterministically (mxGraph XML). Building the XML in code — instead of
asking the model to emit mxGraph — is what makes the diagram reliable:
the model is good at extracting steps from a conversation and terrible
at hand-writing valid mxGraph geometry.

Round-trip contract (FR-07):
  * export — every generated diagram is valid, importable .drawio XML
    (verified by unit test: xml.etree parses it and diagrams.net's
    schema essentials — mxfile/diagram/mxGraphModel/root — are present).
  * import — `steps_from_xml` pulls the node labels/edges back out of a
    .drawio file a user uploads or edits in the embedded editor, so an
    edited diagram can still feed downstream agents as a step list.

No draw.io account/API exists or is needed — the file format IS the
integration (see the Master Documentation §6.1).
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from typing import Optional

# Layout constants — a simple left-to-right flow, wrapping to a new row
# every _PER_ROW steps (matches how the prototype drew its seed flow).
_W, _H = 160, 52
_GAP_X, _GAP_Y = 60, 90
_PER_ROW = 4
_STYLE_NODE = ('rounded=1;whiteSpace=wrap;html=1;fillColor=#ffffff;'
               'strokeColor=#cfd6df;fontColor=#1f2733;fontSize=12;')
_STYLE_EDGE = ('edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;'
               'strokeColor=#8aa0bd;endArrow=block;endFill=1;')
_STYLE_TERMINAL = ('rounded=1;arcSize=50;whiteSpace=wrap;html=1;fillColor=#eef2ff;'
                    'strokeColor=#8aa0bd;fontColor=#1f2733;fontSize=12;')
_STYLE_DECISION = ('rhombus;whiteSpace=wrap;html=1;fillColor=#fff7e6;'
                    'strokeColor=#d9a441;fontColor=#1f2733;fontSize=12;')
_STYLE_DATA = ('shape=parallelogram;whiteSpace=wrap;html=1;fillColor=#eefaf3;'
               'strokeColor=#4caf7d;fontColor=#1f2733;fontSize=12;')
_NODE_STYLES = {'start': _STYLE_TERMINAL, 'end': _STYLE_TERMINAL,
                'decision': _STYLE_DECISION, 'data': _STYLE_DATA,
                'process': _STYLE_NODE}

# ── Swimlane layout (build_drawio_multi_xml) — one horizontal lane per
# responsible actor/role/system (see agent_catalog._E2E_DIAGRAMS_FIELD's
# "lane" field), matching how a BA would actually draw a cross-functional
# process instead of a single flat box-and-arrow chain.
_SW_SLOT_W = 240           # x-space reserved per topological rank (column)
_SW_NODE_W, _SW_NODE_H = 170, 60      # process/start/end/data node size
_SW_DECISION_W, _SW_DECISION_H = 150, 100   # diamonds need more room to
                                             # fit their label without
                                             # overlapping the next lane
_SW_TITLE_W = 130           # width of the rotated lane-title strip
_SW_MARGIN = 40             # left/right margin inside a lane's content area
_SW_ROW_GAP = 16            # vertical gap between stacked nodes sharing a
                             # lane+rank slot (rare, but handled)
_SW_LANE_PAD = 24           # vertical padding above/below a lane's content
_LANE_FILLS = ['#EDE7F6', '#E0F2F1', '#FFF3E0', '#E8EAF6', '#FCE4EC', '#E1F5FE']
_LANE_STROKES = ['#7E57C2', '#26A69A', '#F9A825', '#5C6BC0', '#D81B60', '#0288D1']


def _back_edges(ids: list[str], succ: dict[str, list[str]]) -> set[tuple[str, str]]:
    """DFS back-edge detection (iterative). Model-supplied process edges
    regularly contain cycles — a QA-rework loop back to an earlier step
    is a CORRECT process description — and longest-path ranking explodes
    on them (observed producing a ~25,000-unit-wide page). A human draws
    the loop as a backward arrow; so do we: rank on the graph minus its
    back edges, then still draw every edge."""
    state = {i: 0 for i in ids}   # 0 unvisited, 1 on stack, 2 done
    back: set[tuple[str, str]] = set()
    for root in ids:
        if state[root] != 0:
            continue
        state[root] = 1
        stack: list[tuple[str, iter]] = [(root, iter(succ[root]))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for t in it:
                if state[t] == 1:
                    back.add((node, t))
                elif state[t] == 0:
                    state[t] = 1
                    stack.append((t, iter(succ[t])))
                    advanced = True
                    break
            if not advanced:
                state[node] = 2
                stack.pop()
    return back


def _topo_rank(nodes: list[dict], edges: list[dict]) -> dict[str, int]:
    """Longest-path-from-any-root rank per node id — deterministic column
    assignment for a left-to-right flow. Not a real graph layout engine
    (no crossing minimisation), but the model is bad at geometry, so it's
    never asked to produce any; this stays intentionally simple. Cycles
    are handled by ranking on the graph minus its DFS back edges (see
    _back_edges); ranks are dense-compressed so no column sits empty."""
    ids = [n['id'] for n in nodes]
    id_set = set(ids)
    succ: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        f, t = e.get('from'), e.get('to')
        if f in id_set and t in id_set:
            succ[f].append(t)
    back = _back_edges(ids, succ)
    preds: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        f, t = e.get('from'), e.get('to')
        if f in id_set and t in id_set and (f, t) not in back:
            preds[t].append(f)
    rank = {i: 0 for i in ids}
    for _ in range(len(ids) + 1):
        changed = False
        for i in ids:
            for p in preds[i]:
                if rank[p] + 1 > rank[i]:
                    rank[i] = rank[p] + 1
                    changed = True
        if not changed:
            break
    remap = {r: idx for idx, r in enumerate(sorted(set(rank.values())))}
    return {i: remap[r] for i, r in rank.items()}


def _node_size(ntype: str) -> tuple[int, int]:
    return (_SW_DECISION_W, _SW_DECISION_H) if ntype == 'decision' else (_SW_NODE_W, _SW_NODE_H)


def _swimlane_cells(nodes: list[dict], edges: list[dict], pfx: str) -> tuple[list[str], int, int]:
    """Lay every node out inside a horizontal swimlane per distinct
    'lane' value (missing/blank lanes fall back to one shared 'Process'
    lane, so single-actor diagrams still render — just as a single
    labelled strip instead of a bare box-and-arrow chain). Returns
    (mxCell XML strings, total width, total height)."""
    rank = _topo_rank(nodes, edges)
    lane_order: list[str] = []
    for n in nodes:
        lane = n.get('lane') or 'Process'
        if lane not in lane_order:
            lane_order.append(lane)

    # Slot occupancy per (lane, rank) so two nodes sharing both never
    # overlap — stack the second one into the next sub-row instead.
    slot_count: dict[tuple[str, int], int] = {}
    placed: list[dict] = []
    max_rank = 0
    for n in nodes:
        lane = n.get('lane') or 'Process'
        r = rank.get(n['id'], 0)
        max_rank = max(max_rank, r)
        key = (lane, r)
        subrow = slot_count.get(key, 0)
        slot_count[key] = subrow + 1
        w, h = _node_size(n.get('type', 'process'))
        placed.append({**n, 'lane': lane, 'rank': r, 'subrow': subrow, 'w': w, 'h': h})

    lane_subrows: dict[str, int] = {lane: 0 for lane in lane_order}
    lane_max_h: dict[str, int] = {lane: _SW_NODE_H for lane in lane_order}
    for p in placed:
        lane_subrows[p['lane']] = max(lane_subrows[p['lane']], p['subrow'] + 1)
        lane_max_h[p['lane']] = max(lane_max_h[p['lane']], p['h'])

    lane_height: dict[str, int] = {}
    lane_y: dict[str, int] = {}
    y = 0
    for lane in lane_order:
        subrows = lane_subrows[lane]
        row_h = lane_max_h[lane]
        height = 2 * _SW_LANE_PAD + subrows * row_h + (subrows - 1) * _SW_ROW_GAP
        lane_height[lane] = height
        lane_y[lane] = y
        y += height
    total_height = y
    total_width = _SW_TITLE_W + _SW_MARGIN + (max_rank + 1) * _SW_SLOT_W

    cells: list[str] = []
    for li, lane in enumerate(lane_order):
        fill = _LANE_FILLS[li % len(_LANE_FILLS)]
        stroke = _LANE_STROKES[li % len(_LANE_STROKES)]
        lane_id = f'{pfx}lane{li}'
        name = html.escape(lane[:60], quote=True)
        cells.append(
            f'<mxCell id="{lane_id}" value="{name}" '
            f'style="swimlane;horizontal=0;whiteSpace=wrap;html=1;startSize={_SW_TITLE_W};'
            f'fillColor={fill};strokeColor={stroke};fontColor=#1f2733;fontStyle=1;fontSize=13;" '
            f'vertex="1" parent="1">'
            f'<mxGeometry x="0" y="{lane_y[lane]}" width="{total_width}" height="{lane_height[lane]}" as="geometry"/>'
            f'</mxCell>'
        )

    for p in placed:
        lane = p['lane']
        row_h = lane_max_h[lane]
        slot_w = _SW_SLOT_W
        x = _SW_TITLE_W + _SW_MARGIN + p['rank'] * slot_w + (slot_w - p['w']) // 2
        y_local = _SW_LANE_PAD + p['subrow'] * (row_h + _SW_ROW_GAP) + (row_h - p['h']) // 2
        style = _NODE_STYLES.get(str(p.get('type') or 'process').lower(), _STYLE_NODE)
        label = html.escape(str(p.get('label', ''))[:80], quote=True)
        lane_id = f'{pfx}lane{lane_order.index(lane)}'
        cells.append(
            f'<UserObject id="{pfx}{p["id"]}" label="{label}">'
            f'<mxCell style="{style}" vertex="1" parent="{lane_id}">'
            f'<mxGeometry x="{x}" y="{y_local}" width="{p["w"]}" height="{p["h"]}" as="geometry"/>'
            f'</mxCell></UserObject>'
        )

    for ei, e in enumerate(edges):
        elabel = html.escape(str(e.get('label') or '')[:60], quote=True)
        value_attr = f' value="{elabel}"' if elabel else ''
        cells.append(
            f'<mxCell id="{pfx}e{ei}"{value_attr} style="{_STYLE_EDGE}" edge="1" parent="1" '
            f'source="{pfx}{e["from"]}" target="{pfx}{e["to"]}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>'
        )

    return cells, total_width, total_height


def build_drawio_xml(steps: list[dict], title: str = 'Process flow') -> str:
    """steps: [{'name': str, 'desc': str?}, ...] → .drawio XML string."""
    steps = [s for s in (steps or []) if (s or {}).get('name')]
    if not steps:
        steps = [{'name': 'Start'}, {'name': 'End'}]

    cells: list[str] = [
        '<mxCell id="0"/>',
        '<mxCell id="1" parent="0"/>',
    ]
    for i, s in enumerate(steps):
        col, row = i % _PER_ROW, i // _PER_ROW
        # Serpentine layout: odd rows flow right-to-left so consecutive
        # boxes stay adjacent when the flow wraps.
        if row % 2 == 1:
            col = _PER_ROW - 1 - col
        x = 40 + col * (_W + _GAP_X)
        y = 40 + row * (_H + _GAP_Y)
        label = html.escape(str(s.get('name', ''))[:80], quote=True)
        desc = str(s.get('desc') or '')[:160]
        tooltip = f' tooltip="{html.escape(desc, quote=True)}"' if desc else ''
        cells.append(
            f'<UserObject id="n{i + 2}" label="{label}"{tooltip}>'
            f'<mxCell style="{_STYLE_NODE}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{_W}" height="{_H}" as="geometry"/>'
            f'</mxCell></UserObject>'
        )
    for i in range(len(steps) - 1):
        cells.append(
            f'<mxCell id="e{i}" style="{_STYLE_EDGE}" edge="1" parent="1" '
            f'source="n{i + 2}" target="n{i + 3}"><mxGeometry relative="1" as="geometry"/></mxCell>'
        )

    name = html.escape(title[:80], quote=True)
    return (
        '<mxfile host="ai-discovery-canvas" type="device">'
        f'<diagram name="{name}" id="flow-1">'
        '<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" '
        'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        'pageWidth="1100" pageHeight="850" math="0" shadow="0">'
        '<root>' + ''.join(cells) + '</root>'
        '</mxGraphModel></diagram></mxfile>'
    )


def build_drawio_multi_xml(diagrams: list[dict], title: str = 'Process flows') -> str:
    """diagrams: [{'title', 'summary'?, 'nodes': [{'id','label','type','lane'?}],
    'edges': [{'from','to','label'?}]}], node type in
    start/end/process/decision/data. Renders one .drawio page per
    diagram as a horizontal SWIMLANE per distinct 'lane' (see
    agent_catalog._E2E_DIAGRAMS_FIELD) — a single-actor diagram still
    renders (one lane, one strip), so callers never need to branch on
    whether lanes were actually supplied. draw.io/embed.diagrams.net
    natively support multi-page files with tabs, so this is still a
    single XML string with the same contract as ``build_drawio_xml``
    (the embedded editor and the download button need no changes to
    handle 1-4 distinct end-to-end processes instead of one)."""
    diagrams = [d for d in (diagrams or []) if (d or {}).get('nodes')]
    if not diagrams:
        return build_drawio_xml([], title)

    pages: list[str] = []
    for di, d in enumerate(diagrams):
        nodes = [n for n in (d.get('nodes') or []) if n.get('id') and n.get('label')]
        if not nodes:
            continue
        edges = [e for e in (d.get('edges') or []) if e.get('from') and e.get('to')]
        pfx = f'd{di}_'
        lane_cells, width, height = _swimlane_cells(nodes, edges, pfx)
        cells: list[str] = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>', *lane_cells]
        page_name = html.escape(str(d.get('title') or f'Process {di + 1}')[:80], quote=True)
        pages.append(
            f'<diagram name="{page_name}" id="flow-{di + 1}">'
            '<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" '
            'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
            f'pageWidth="{max(1100, width + 40)}" pageHeight="{max(850, height + 40)}" math="0" shadow="0">'
            '<root>' + ''.join(cells) + '</root>'
            '</mxGraphModel></diagram>'
        )
    if not pages:
        return build_drawio_xml([], title)
    return '<mxfile host="ai-discovery-canvas" type="device">' + ''.join(pages) + '</mxfile>'


def steps_from_xml(xml_text: str) -> Optional[list[dict]]:
    """Best-effort inverse: pull the ordered node labels back out of a
    .drawio file (ours, an embedded-editor edit of ours, or a user's own
    uncompressed export). Returns None when the XML isn't parseable or
    contains no vertices — callers treat that as "keep the raw XML but
    no step list". Compressed (deflated) drawio payloads are out of
    scope — diagrams.net saves uncompressed when asked via the embed
    protocol, which is the path we use."""
    try:
        tree = ET.fromstring(xml_text or '')
    except Exception:
        return None
    steps: list[dict] = []
    # Vertices appear either as UserObject[label] wrapping an mxCell, or
    # as bare mxCell[value] with vertex="1".
    for uo in tree.iter('UserObject'):
        cell = uo.find('mxCell')
        if cell is not None and cell.get('vertex') == '1':
            name = (uo.get('label') or '').strip()
            if name:
                steps.append({'name': name, 'desc': (uo.get('tooltip') or '').strip()})
    for cell in tree.iter('mxCell'):
        if cell.get('vertex') == '1' and (cell.get('value') or '').strip():
            steps.append({'name': cell.get('value').strip(), 'desc': ''})
    return steps or None
