"""Trace a representative source -> sink data-flow path through the graph.

The graph is bipartite (nodes <-> topics). Given a focus node or topic, we walk
upstream and downstream, and at each branch follow the *lowest-rate* link — the
constraining edge — so the path highlights where a pipeline is throttled. This
is the "critical path" an AI (or a human) wants when a node is flagged as a
bottleneck: not just "detector is slow" but "camera -> detector -> [4.1 Hz] ->
tracker -> ...", with the throttling hop marked.

Kept free of the rclpy/model layer: it operates on the plain snapshot dict, so
it is reused by the Markdown briefing, the REST API, and tests alike.
"""

from __future__ import annotations

_MAX_HOPS = 12


def resolve_focus(d: dict, focus: str):
    """Map a focus string to a ``('node', id)`` or ``('topic', name)`` target.

    Tries exact node id, exact topic name, exact node name, then a suffix match
    on a node, then on a topic. Returns ``(None, None)`` if nothing matches.
    Nodes win ties so a bare name resolves to the node, not a topic."""
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


def _rate_key(topic: dict) -> float:
    """Sort key for "most constraining": known low rates first, unknown last."""
    r = topic.get('rate_hz')
    return r if isinstance(r, (int, float)) else float('inf')


def _walk(nodes_by_id, topics_by_name, start: str, downstream: bool):
    """Walk one direction from ``start``, following the lowest-rate link each
    step. Returns hops as ``{from, topic, rate_hz, status, to}`` in walk order
    (nearest hop first)."""
    hops = []
    visited = {start}
    cur = start
    for _ in range(_MAX_HOPS):
        node = nodes_by_id.get(cur)
        if not node:
            break
        link_topics = node.get('publishers' if downstream else 'subscribers', [])
        endpoint_key = 'subscribers' if downstream else 'publishers'
        cands = []
        for tn in link_topics:
            t = topics_by_name.get(tn)
            if not t:
                continue
            others = [e for e in t.get(endpoint_key, []) if e not in visited]
            if others:
                cands.append((t, sorted(others)[0]))
        if not cands:
            break
        cands.sort(key=lambda c: (_rate_key(c[0]), c[0]['name']))
        t, nxt = cands[0]
        frm, to = (cur, nxt) if downstream else (nxt, cur)
        hops.append({'from': frm, 'topic': t['name'], 'rate_hz': t.get('rate_hz'),
                     'status': t.get('status'), 'to': to})
        visited.add(nxt)
        cur = nxt
    return hops


def trace_pipeline_path(d: dict, focus: str):
    """Trace the constraining path through ``focus`` (a node or topic).

    Returns ``{target, pivot, nodes, hops, bottleneck_topic}`` or ``None`` if
    the focus is unknown or has no connected path. ``nodes`` is the ordered list
    of node ids from source to sink; ``hops`` are the topics between them;
    ``bottleneck_topic`` is the lowest-rate hop (the throttling link)."""
    kind, key = resolve_focus(d, focus)
    if kind is None:
        return None

    nodes_by_id = {n['id']: n for n in d['nodes']}
    topics_by_name = {t['name']: t for t in d['topics']}

    if kind == 'node':
        pivot, label = key, key
    else:  # topic — pivot on a publisher (or a subscriber if it has no pub)
        t = topics_by_name[key]
        pubs, subs = sorted(t.get('publishers', [])), sorted(t.get('subscribers', []))
        pivot = (pubs or subs or [None])[0]
        label = key
        if pivot is None:
            return None

    up = _walk(nodes_by_id, topics_by_name, pivot, downstream=False)
    down = _walk(nodes_by_id, topics_by_name, pivot, downstream=True)
    hops = list(reversed(up)) + down
    if not hops:
        return None

    nodes_seq = [hops[0]['from']] + [h['to'] for h in hops]
    rated = [h for h in hops if isinstance(h['rate_hz'], (int, float))]
    bottleneck = min(rated, key=lambda h: h['rate_hz'])['topic'] if rated else None
    return {'target': label, 'pivot': pivot, 'nodes': nodes_seq, 'hops': hops,
            'bottleneck_topic': bottleneck}
