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


def _layered_positions(nodes: list[dict], edges: list[dict]) -> dict[str, tuple[int, int]]:
    """Assign each node a (column, row) grid slot from its edges, deepest
    ancestor chain first — a plain topological layering, not a real graph
    layout engine, but deterministic and good enough for a code-generated
    diagram (same philosophy as the linear layout above: the model is bad
    at geometry, so it never gets asked to produce any)."""
    ids = [n['id'] for n in nodes]
    id_set = set(ids)
    preds: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        f, t = e.get('from'), e.get('to')
        if f in id_set and t in id_set:
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
    by_rank: dict[int, list[str]] = {}
    for i in ids:
        by_rank.setdefault(rank[i], []).append(i)
    positions: dict[str, tuple[int, int]] = {}
    for r, group in by_rank.items():
        for row, nid in enumerate(group):
            positions[nid] = (r, row)
    return positions


def build_drawio_multi_xml(diagrams: list[dict], title: str = 'Process flows') -> str:
    """diagrams: [{'title', 'summary'?, 'nodes': [{'id','label','type'}],
    'edges': [{'from','to','label'?}]}], node type in
    start/end/process/decision/data. Renders one .drawio page per
    diagram — draw.io/embed.diagrams.net natively support multi-page
    files with tabs, so this is still a single XML string with the same
    contract as ``build_drawio_xml`` (the embedded editor and the
    download button need no changes to handle 1-4 distinct end-to-end
    processes instead of one)."""
    diagrams = [d for d in (diagrams or []) if (d or {}).get('nodes')]
    if not diagrams:
        return build_drawio_xml([], title)

    pages: list[str] = []
    for di, d in enumerate(diagrams):
        nodes = [n for n in (d.get('nodes') or []) if n.get('id') and n.get('label')]
        if not nodes:
            continue
        edges = [e for e in (d.get('edges') or []) if e.get('from') and e.get('to')]
        positions = _layered_positions(nodes, edges)
        pfx = f'd{di}_'
        cells: list[str] = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
        for n in nodes:
            col, row = positions.get(n['id'], (0, 0))
            x = 40 + col * (_W + _GAP_X)
            y = 40 + row * (_H + _GAP_Y)
            style = _NODE_STYLES.get(str(n.get('type') or 'process').lower(), _STYLE_NODE)
            label = html.escape(str(n.get('label', ''))[:80], quote=True)
            cells.append(
                f'<UserObject id="{pfx}{n["id"]}" label="{label}">'
                f'<mxCell style="{style}" vertex="1" parent="1">'
                f'<mxGeometry x="{x}" y="{y}" width="{_W}" height="{_H}" as="geometry"/>'
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
        page_name = html.escape(str(d.get('title') or f'Process {di + 1}')[:80], quote=True)
        pages.append(
            f'<diagram name="{page_name}" id="flow-{di + 1}">'
            '<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" '
            'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
            'pageWidth="1100" pageHeight="850" math="0" shadow="0">'
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
