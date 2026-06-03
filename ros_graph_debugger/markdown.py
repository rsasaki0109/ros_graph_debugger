"""Render a snapshot as compact Markdown for LLM consumption.

The goal is an AI-friendly briefing a developer can paste into a chat (or an
agent can fetch via /api/v1/snapshot.md): issues first with evidence, then a
terse graph/metrics table. Kept small on purpose so it fits a prompt."""

from __future__ import annotations


def _fmt_rate(v):
    return f'{v:.1f} Hz' if isinstance(v, (int, float)) else '—'


def _fmt_bw(v):
    if not isinstance(v, (int, float)):
        return '—'
    if v >= 1e6:
        return f'{v/1e6:.1f} MB/s'
    if v >= 1e3:
        return f'{v/1e3:.1f} KB/s'
    return f'{v:.0f} B/s'


def _resolve_focus(d: dict, focus: str):
    """Map a focus string to a ``('node', id)`` or ``('topic', name)`` target.

    Tries exact node id, exact topic name, exact node name, then a suffix
    match on a node, then on a topic. Returns ``(None, None)`` if nothing
    matches. Nodes win ties so a bare name resolves to the node, not a topic."""
    nodes, topics = d['nodes'], d['topics']
    for n in nodes:
        if n['id'] == focus:
            return 'node', n['id']
    for t in topics:
        if t['name'] == focus:
            return 'topic', t['name']
    for n in nodes:
        if n.get('name') == focus:
            return 'node', n['id']
    for n in nodes:
        if n['id'].endswith(focus) or (n.get('name') or '').endswith(focus):
            return 'node', n['id']
    for t in topics:
        if t['name'].endswith(focus):
            return 'topic', t['name']
    return None, None


def focus_subgraph(d: dict, focus: str):
    """Slice a snapshot down to one node/topic and its 1-hop neighbourhood.

    For a node: keeps it, every node sharing a topic with it, those topics and
    edges. For a topic: keeps the topic and its publisher/subscriber nodes.
    Either way it keeps issues touching that neighbourhood. Returns
    ``(label, sliced_dict)`` or ``(None, None)`` when the focus is unknown.
    Big Autoware/Nav2 graphs are too large to hand an AI whole; this is the
    "just the part I'm asking about" briefing."""
    kind, key = _resolve_focus(d, focus)
    if kind is None:
        return None, None

    if kind == 'node':
        label = key
        topic_names = {t['name'] for t in d['topics']
                       if key in t.get('publishers', [])
                       or key in t.get('subscribers', [])}
        neighbours = {key}
    else:  # topic
        label = key
        topic_names = {key}
        neighbours = set()
    for t in d['topics']:
        if t['name'] in topic_names:
            neighbours.update(t.get('publishers', []))
            neighbours.update(t.get('subscribers', []))

    def issue_in_scope(i):
        return (set(i.get('related_nodes', [])) & neighbours
                or set(i.get('related_topics', [])) & topic_names)

    issues = [i for i in d['issues'] if issue_in_scope(i)]
    frames = set()
    for i in issues:
        frames.update(i.get('related_frames', []))

    sliced = {
        'timestamp': d.get('timestamp'),
        'profile': d.get('profile'),
        'nodes': [n for n in d['nodes'] if n['id'] in neighbours],
        'topics': [t for t in d['topics'] if t['name'] in topic_names],
        'edges': [e for e in d.get('edges', []) if e.get('topic') in topic_names],
        'tf_edges': [e for e in d.get('tf_edges', [])
                     if e.get('parent') in frames or e.get('child') in frames],
        'diagnostics': [],
        'issues': issues,
    }
    return label, sliced


def snapshot_to_markdown(snap, focus: str | None = None) -> str:
    # Accept a GraphSnapshot, a replay _Frame, or a plain dict.
    d = snap.to_dict() if hasattr(snap, 'to_dict') else snap

    focused_on = None
    if focus:
        label, sliced = focus_subgraph(d, focus)
        if sliced is None:
            return (f'# ROS Graph Debugger — Runtime Snapshot\n'
                    f'No node or topic matching `{focus}` is in the graph.')
        d, focused_on = sliced, label

    lines: list[str] = []
    lines.append('# ROS Graph Debugger — Runtime Snapshot')
    if focused_on:
        lines.append(f'Focused on **{focused_on}** and its direct neighbours.')
        others = [n['id'] for n in d['nodes'] if n['id'] != focused_on]
        if others:
            lines.append('Neighbours: ' + ', '.join(sorted(others)))
    if d.get('profile'):
        lines.append(f'Profile: **{d["profile"]}**')
    lines.append(f'Nodes: {len(d["nodes"])}  ·  Topics: {len(d["topics"])}  ·  '
                 f'Issues: {len(d["issues"])}')
    lines.append('')

    # --- Issues first: this is what an AI should act on. ---
    issues = d['issues']
    lines.append('## Issues (what to look at next)')
    if not issues:
        lines.append('No issues detected.')
    else:
        for i in issues:
            lines.append(f'### [{i["severity"].upper()}] {i["title"]}')
            if i.get('explanation'):
                lines.append(i['explanation'])
            if i.get('evidence'):
                lines.append('- Evidence: ' + '; '.join(i['evidence']))
            if i.get('suggested_actions'):
                lines.append('- Suggested: ' + '; '.join(i['suggested_actions']))
            related = i.get('related_nodes', []) + i.get('related_topics', []) \
                + i.get('related_frames', [])
            if related:
                lines.append('- Related: ' + ', '.join(related))
            lines.append('')

    # --- Topic table. ---
    lines.append('## Topics')
    lines.append('| topic | type | pub | sub | rate | age p95 | bandwidth | qos | status |')
    lines.append('|---|---|---|---|---|---|---|---|---|')
    for t in sorted(d['topics'], key=lambda x: x['name']):
        age = t.get('header_age_p95_ms')
        lines.append('| {name} | {type} | {p} | {s} | {rate} | {age} | {bw} | {qos} | '
                     '{status} |'.format(
                         name=t['name'],
                         type=t['type'].split('/')[-1] if t['type'] else '—',
                         p=t['publisher_count'], s=t['subscriber_count'],
                         rate=_fmt_rate(t.get('rate_hz')),
                         age=f'{age:.0f} ms' if isinstance(age, (int, float)) else '—',
                         bw=_fmt_bw(t.get('bandwidth_bps')),
                         qos=t.get('qos_status', 'unknown'),
                         status=t.get('status', 'unknown')))
    lines.append('')

    # --- Nodes with process metrics, when known. ---
    metric_nodes = [n for n in d['nodes'] if n.get('cpu_percent') is not None]
    if metric_nodes:
        lines.append('## Nodes (process metrics)')
        lines.append('| node | cpu | rss | confidence |')
        lines.append('|---|---|---|---|')
        for n in sorted(metric_nodes, key=lambda x: -(x.get('cpu_percent') or 0)):
            rss = n.get('rss_bytes')
            rss_s = f'{rss/1e6:.0f} MB' if rss else '—'
            lines.append(f'| {n["id"]} | {n["cpu_percent"]:.0f}% | {rss_s} | '
                         f'{n["process_mapping_confidence"]} |')
        lines.append('')

    # --- TF staleness, if any. ---
    stale_tf = [e for e in d['tf_edges'] if e.get('status') != 'ok']
    if stale_tf:
        lines.append('## TF (stale transforms)')
        for e in stale_tf:
            lines.append(f'- {e["parent"]} -> {e["child"]}: '
                         f'{e.get("age_ms", "?")} ms')
        lines.append('')

    return '\n'.join(lines)
