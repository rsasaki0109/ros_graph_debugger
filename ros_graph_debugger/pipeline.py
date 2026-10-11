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

    # Annotate each hop with the consuming node's callback p95 for that topic —
    # the measured execution-time counterpart to the rate-based bottleneck.
    cb_p95 = {(c['node'], c.get('topic')): c.get('p95_ms')
              for c in d.get('callbacks', [])
              if isinstance(c.get('p95_ms'), (int, float))}
    for h in hops:
        h['cb_p95_ms'] = cb_p95.get((h['to'], h['topic']))

    nodes_seq = [hops[0]['from']] + [h['to'] for h in hops]
    rated = [h for h in hops if isinstance(h['rate_hz'], (int, float))]
    bottleneck = min(rated, key=lambda h: h['rate_hz'])['topic'] if rated else None
    cb_hops = [h for h in hops if isinstance(h['cb_p95_ms'], (int, float))]
    cb_bottleneck = max(cb_hops, key=lambda h: h['cb_p95_ms'])['to'] if cb_hops else None
    return {'target': label, 'pivot': pivot, 'nodes': nodes_seq, 'hops': hops,
            'bottleneck_topic': bottleneck, 'cb_bottleneck_node': cb_bottleneck}


# --------------------------------------------------------------------------- #
# The same path over a recording: rate and callback time per hop, over time.
# --------------------------------------------------------------------------- #
_BLAME_KINDS = ('bottleneck', 'slow_callback')
_BLOCKS = '▁▂▃▄▅▆▇█'


def _blamed(snap: dict) -> list[str]:
    return [n for i in snap.get('issues', []) if i.get('kind') in _BLAME_KINDS
            for n in i.get('related_nodes', [])]


def default_focus(snapshots: list[dict]) -> str | None:
    """The node most often named by a bottleneck / slow-callback issue."""
    counts: dict[str, int] = {}
    for s in snapshots:
        for n in set(_blamed(s)):
            counts[n] = counts.get(n, 0) + 1
    return max(sorted(counts), key=lambda n: counts[n]) if counts else None


def pipeline_timeline(snapshots: list[dict], focus: str | None = None) -> dict | None:
    """Follow one pipeline path across a recording.

    The path is traced once — on the first sample where ``focus`` is blamed
    (or the last sample) — and held fixed, so the series compare like with
    like. Each hop gets the topic rate and the consuming node's callback p95
    per sample (``None`` where not measured). Returns ``{target, nodes, t,
    hops: [{from, topic, to, rate_hz: [...], cb_p95_ms: [...]}], blamed: [...]}``
    or ``None`` when there is no focus or no path."""
    if not snapshots:
        return None
    focus = focus or default_focus(snapshots)
    if not focus:
        return None
    ref = next((s for s in snapshots if focus in _blamed(s)), snapshots[-1])
    path = trace_pipeline_path(ref, focus)
    if path is None:
        return None
    t0 = snapshots[0].get('timestamp') or 0.0
    times, blamed = [], []
    hops = [{'from': h['from'], 'topic': h['topic'], 'to': h['to'],
             'rate_hz': [], 'cb_p95_ms': []} for h in path['hops']]
    for s in snapshots:
        times.append(round((s.get('timestamp') or t0) - t0, 2))
        blamed.append(path['pivot'] in _blamed(s))
        rates = {t['name']: t.get('rate_hz') for t in s.get('topics', [])}
        cbs = {(c['node'], c.get('topic')): c.get('p95_ms')
               for c in s.get('callbacks', [])}
        for h in hops:
            h['rate_hz'].append(rates.get(h['topic']))
            h['cb_p95_ms'].append(cbs.get((h['to'], h['topic'])))
    return {'target': path['target'], 'pivot': path['pivot'],
            'nodes': path['nodes'], 't': times, 'hops': hops, 'blamed': blamed}


def spark(values: list, width: int = 40) -> str:
    """A time-aligned block sparkline: ``values`` are bucketed into at most
    ``width`` cells (bucket mean); a bucket with no measurement is a space."""
    if not values:
        return ''
    n = len(values)
    cells = min(width, n)
    buckets = []
    for i in range(cells):
        chunk = [v for v in values[i * n // cells:(i + 1) * n // cells]
                 if isinstance(v, (int, float))]
        buckets.append(sum(chunk) / len(chunk) if chunk else None)
    known = [b for b in buckets if b is not None]
    if not known:
        return ' ' * cells
    lo, hi = min(known), max(known)
    out = []
    for b in buckets:
        if b is None:
            out.append(' ')
        elif hi <= lo:
            out.append(_BLOCKS[len(_BLOCKS) // 2])
        else:
            out.append(_BLOCKS[round((b - lo) / (hi - lo) * (len(_BLOCKS) - 1))])
    return ''.join(out)
