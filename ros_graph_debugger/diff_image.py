"""SVG/PNG renderer for ``rgd diff --image``.

The renderer deliberately stays dependency-free for SVG. PNG conversion is an
optional cairosvg step so the core package remains lightweight.
"""

from __future__ import annotations

import html
from pathlib import Path


W = 1500
PANEL_W = 700
GAP = 40
MARGIN = 34
NODE_W = 150
TOPIC_W = 230
H = 760

COLORS = {
    'bg': '#0d1117',
    'panel': '#161b22',
    'panel2': '#1c2330',
    'border': '#2d333b',
    'text': '#c9d1d9',
    'muted': '#8b949e',
    'accent': '#58a6ff',
    'ok': '#3fb950',
    'warning': '#d29922',
    'critical': '#f85149',
    'unknown': '#6e7681',
}


def write_diff_image(path: str, diff: dict, base_snaps: list[dict],
                     cur_snaps: list[dict]) -> dict:
    """Write a side-by-side graph diff image.

    Returns ``{'path': actual_path, 'format': 'svg'|'png', 'note': str}``.
    When ``path`` ends in ``.png`` and cairosvg is unavailable, a sibling SVG is
    written instead and the note explains the fallback.
    """
    svg = render_diff_svg(diff, base_snaps, cur_snaps)
    out = Path(path)
    if out.suffix.lower() == '.png':
        try:
            import cairosvg  # type: ignore
        except Exception:
            svg_path = out.with_suffix('.svg')
            svg_path.write_text(svg, encoding='utf-8')
            return {
                'path': str(svg_path),
                'format': 'svg',
                'note': 'cairosvg not installed; wrote SVG instead',
            }
        cairosvg.svg2png(bytestring=svg.encode('utf-8'), write_to=str(out))
        return {'path': str(out), 'format': 'png', 'note': ''}

    out.write_text(svg, encoding='utf-8')
    return {'path': str(out), 'format': 'svg', 'note': ''}


def render_diff_svg(diff: dict, base_snaps: list[dict],
                    cur_snaps: list[dict]) -> str:
    marks = _marked_refs(diff)
    base = _representative_snapshot(base_snaps, marks, prefer_issue=False)
    cur = _representative_snapshot(cur_snaps, marks, prefer_issue=True)
    base_graph = _graph_from_snapshot(base, marks)
    cur_graph = _graph_from_snapshot(cur, marks)
    verdict = diff.get('verdict', 'stable')
    verdict_color = COLORS['critical'] if verdict == 'regressed' else (
        COLORS['ok'] if verdict == 'stable' else COLORS['accent'])

    title = 'ROS Graph Regression Diff'
    subtitle = _subtitle(diff)
    panels = [
        _panel_svg(base_graph, 0, 'Baseline', diff, marks, is_current=False),
        _panel_svg(cur_graph, PANEL_W + GAP, 'Candidate', diff, marks,
                   is_current=True),
    ]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{_e(title)}">
  <style>
    .label {{ font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: {COLORS['text']}; }}
    .small {{ font: 11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: {COLORS['muted']}; }}
    .title {{ font: 700 22px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: {COLORS['text']}; }}
    .panel-title {{ font: 700 15px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: {COLORS['text']}; }}
    .badge {{ font: 700 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  </style>
  <rect width="{W}" height="{H}" fill="{COLORS['bg']}"/>
  <text x="{MARGIN}" y="34" class="title">{_e(title)}</text>
  <rect x="{W - 190}" y="16" width="150" height="30" rx="15" fill="{verdict_color}" opacity="0.18" stroke="{verdict_color}"/>
  <text x="{W - 115}" y="36" text-anchor="middle" class="badge" fill="{verdict_color}">{_e(verdict.upper())}</text>
  <text x="{MARGIN}" y="58" class="small">{_e(subtitle)}</text>
  {''.join(panels)}
</svg>'''


def _subtitle(diff: dict) -> str:
    if diff.get('verdict') == 'stable':
        return 'No regressions detected'
    parts = []
    if diff.get('topic_regressions'):
        parts.append(f'{len(diff["topic_regressions"])} topic rate drop(s)')
    if diff.get('callback_regressions'):
        parts.append(f'{len(diff["callback_regressions"])} slower callback(s)')
    if diff.get('new_issues'):
        parts.append(f'{len(diff["new_issues"])} new issue(s)')
    return ' · '.join(parts) or 'Graph comparison'


def _marked_refs(diff: dict) -> dict:
    topics = {t['name'] for t in diff.get('topic_regressions', [])}
    nodes = {c['node'] for c in diff.get('callback_regressions', [])}
    for issue in diff.get('new_issues', []):
        topics.update(issue.get('related_topics', []))
        nodes.update(issue.get('related_nodes', []))
    return {'topics': topics, 'nodes': nodes}


def _representative_snapshot(snaps: list[dict], marks: dict,
                             prefer_issue: bool) -> dict:
    if not snaps:
        return {'nodes': [], 'topics': [], 'issues': []}
    if prefer_issue:
        for snap in snaps:
            if any(_issue_touches(issue, marks)
                   for issue in snap.get('issues', [])):
                return snap
    for snap in reversed(snaps):
        if _snap_touches(snap, marks):
            return snap
    return snaps[-1]


def _issue_touches(issue: dict, marks: dict) -> bool:
    return bool(set(issue.get('related_topics', [])) & marks['topics'] or
                set(issue.get('related_nodes', [])) & marks['nodes'])


def _snap_touches(snap: dict, marks: dict) -> bool:
    topics = {t.get('name') for t in snap.get('topics', [])}
    nodes = {n.get('id') for n in snap.get('nodes', [])}
    return bool(topics & marks['topics'] or nodes & marks['nodes'])


def _graph_from_snapshot(snap: dict, marks: dict) -> dict:
    nodes = {n['id']: n for n in snap.get('nodes', [])}
    topics = {t['name']: t for t in snap.get('topics', [])}
    focus_nodes = set(marks['nodes'])
    focus_topics = set(marks['topics'])

    for topic in list(focus_topics):
        t = topics.get(topic)
        if not t:
            continue
        focus_nodes.update(t.get('publishers', []))
        focus_nodes.update(t.get('subscribers', []))
    for node in list(focus_nodes):
        n = nodes.get(node)
        if not n:
            continue
        focus_topics.update(n.get('publishers', []))
        focus_topics.update(n.get('subscribers', []))

    if not focus_nodes and not focus_topics:
        focus_nodes = set(list(nodes)[:8])
        for node in focus_nodes:
            n = nodes[node]
            focus_topics.update(n.get('publishers', [])[:1])
            focus_topics.update(n.get('subscribers', [])[:1])

    if len(nodes) <= 50 and len(topics) <= 50:
        # Keep enough context for small graphs, but still sort highlighted items
        # first so regressions are near the visual center.
        focus_nodes.update(nodes)
        focus_topics.update(topics)

    focus_nodes = {n for n in focus_nodes if n in nodes}
    focus_topics = {t for t in focus_topics if t in topics}

    edges = []
    for tname in focus_topics:
        t = topics[tname]
        for pub in t.get('publishers', []):
            if pub in focus_nodes:
                edges.append(('N:' + pub, 'T:' + tname, tname))
        for sub in t.get('subscribers', []):
            if sub in focus_nodes:
                edges.append(('T:' + tname, 'N:' + sub, tname))

    return {
        'nodes': [nodes[n] for n in _sort_refs(focus_nodes, marks['nodes'])],
        'topics': [topics[t] for t in _sort_refs(focus_topics, marks['topics'])],
        'edges': edges,
    }


def _sort_refs(refs, marked):
    return sorted(refs, key=lambda x: (0 if x in marked else 1, x))


def _panel_svg(graph: dict, x0: int, title: str, diff: dict, marks: dict,
               is_current: bool) -> str:
    x = MARGIN + x0
    y = 86
    body_h = H - y - 36
    coords = _layout(graph, x, y + 54, body_h - 72, marks)
    edge_svg = ''.join(_edge_svg(edge, coords, marks) for edge in graph['edges'])
    node_svg = ''.join(_node_svg(n, coords['N:' + n['id']], marks, is_topic=False)
                       for n in graph['nodes'])
    topic_svg = ''.join(_node_svg(t, coords['T:' + t['name']], marks, is_topic=True)
                        for t in graph['topics'])
    summary = _panel_summary(diff, is_current)
    return f'''
  <g>
    <rect x="{x}" y="{y}" width="{PANEL_W}" height="{body_h}" rx="8" fill="{COLORS['panel']}" stroke="{COLORS['border']}"/>
    <text x="{x + 18}" y="{y + 28}" class="panel-title">{_e(title)}</text>
    <text x="{x + 18}" y="{y + 47}" class="small">{_e(summary)}</text>
    {edge_svg}
    {node_svg}
    {topic_svg}
  </g>'''


def _panel_summary(diff: dict, is_current: bool) -> str:
    h = diff.get('health', {})
    if diff.get('verdict') == 'stable':
        return 'No regressions'
    if is_current:
        return f'health {h.get("cur", "ok")} · red items regressed'
    return f'health {h.get("base", "ok")} · before change'


def _layout(graph: dict, x: int, y: int, height: int, marks: dict) -> dict:
    publisher_nodes = set()
    for topic in graph['topics']:
        publisher_nodes.update(topic.get('publishers', []))
    node_left = [n for n in graph['nodes'] if n['id'] in publisher_nodes]
    left_ids = {n['id'] for n in node_left}
    node_right = [n for n in graph['nodes'] if n['id'] not in left_ids]
    layers = [
        [('N:' + n['id'], n) for n in node_left],
        [('T:' + t['name'], t) for t in graph['topics']],
        [('N:' + n['id'], n) for n in node_right],
    ]
    xs = [x + 84, x + PANEL_W / 2, x + PANEL_W - 84]
    coords = {}
    for li, items in enumerate(layers):
        items = items[:12]
        if not items:
            continue
        gap = height / max(1, len(items))
        for i, (rid, item) in enumerate(items):
            cy = y + gap * (i + 0.5)
            coords[rid] = (xs[li], cy)
    return coords


def _edge_svg(edge, coords: dict, marks: dict) -> str:
    src, dst, topic = edge
    if src not in coords or dst not in coords:
        return ''
    x1, y1 = coords[src]
    x2, y2 = coords[dst]
    bad = topic in marks['topics']
    color = COLORS['critical'] if bad else COLORS['border']
    width = 3 if bad else 1.5
    return (f'<path d="M{x1:.1f},{y1:.1f} C{(x1+x2)/2:.1f},{y1:.1f} '
            f'{(x1+x2)/2:.1f},{y2:.1f} {x2:.1f},{y2:.1f}" '
            f'fill="none" stroke="{color}" stroke-width="{width}" '
            f'stroke-dasharray="8 6" opacity="{0.95 if bad else 0.65}"/>')


def _node_svg(item: dict, xy, marks: dict, *, is_topic: bool) -> str:
    cx, cy = xy
    name = item['name'] if is_topic else item.get('name') or item['id']
    ref = item['name'] if is_topic else item['id']
    marked = ref in (marks['topics'] if is_topic else marks['nodes'])
    w = TOPIC_W if is_topic else NODE_W
    h = 58 if is_topic else 54
    x = cx - w / 2
    y = cy - h / 2
    status = item.get('status', 'unknown')
    border = COLORS['critical'] if marked else COLORS.get(status, COLORS['unknown'])
    fill = 'rgba(248,81,73,0.18)' if marked else COLORS['panel2']
    label = _shorten(name, 30 if is_topic else 18)
    meta = _topic_meta(item) if is_topic else _node_meta(item)
    return f'''
    <g>
      <rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{border}" stroke-width="{4 if marked else 2}"/>
      <text x="{cx:.1f}" y="{cy - 6:.1f}" text-anchor="middle" class="label">{_e(label)}</text>
      <text x="{cx:.1f}" y="{cy + 14:.1f}" text-anchor="middle" class="small">{_e(meta)}</text>
    </g>'''


def _topic_meta(t: dict) -> str:
    rate = t.get('rate_hz')
    bw = t.get('bandwidth_bps')
    bits = []
    if isinstance(rate, (int, float)):
        bits.append(f'{rate:.1f} Hz')
    if isinstance(bw, (int, float)):
        bits.append(_fmt_bw(bw))
    return ' · '.join(bits) or t.get('status', 'unknown')


def _node_meta(n: dict) -> str:
    cpu = n.get('cpu_percent')
    if isinstance(cpu, (int, float)):
        return f'CPU {cpu:.0f}%'
    return n.get('status', 'unknown')


def _fmt_bw(v):
    if v >= 1e6:
        return f'{v / 1e6:.1f} MB/s'
    if v >= 1e3:
        return f'{v / 1e3:.1f} KB/s'
    return f'{v:.0f} B/s'


def _shorten(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return '…' + s[-(n - 1):]


def _e(s) -> str:
    return html.escape(str(s), quote=True)
