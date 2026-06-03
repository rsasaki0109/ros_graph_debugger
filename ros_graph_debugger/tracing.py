"""Tier C tracing: per-callback execution-time stats.

The richest latency signal in ROS 2 is how long each callback actually runs —
that is what `ros2_tracing` / LTTng capture. A real adapter reads a CTF trace
(or a live LTTng session) and aggregates `callback_start`/`callback_end` pairs
into per-callback duration stats; that adapter is the remaining v0.3 work and
needs an LTTng-enabled build, so it does not run in every environment.

What lives here now is the **data shape and a synthetic source**, mirroring how
`replay.py` lets the whole product work without DDS: `synthesize_callbacks`
produces plausible `CallbackStat`s from the graph (one per subscription
callback), with an optional hot node whose callback p95 spikes. The demo
recording and the issue engine consume the same shape a real trace adapter will
emit, so wiring LTTng later is a drop-in.
"""

from __future__ import annotations

import json

from .model import CallbackStat


def _percentile(values, pct: float):
    """Nearest-rank percentile, matching the probe metrics' method."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int((pct / 100.0) * len(ordered)))
    return round(float(ordered[idx]), 1)


def aggregate_callback_durations(rows) -> list[CallbackStat]:
    """Aggregate per-invocation callback durations into ``CallbackStat``s.

    This is the real work a tracing adapter does: ``rows`` is an iterable of
    ``{node, callback?, topic?, duration_ms}`` (one per callback invocation, as
    a CTF trace yields), grouped by (node, callback, topic) into count / mean /
    p95 / max. ``callback`` defaults to ``sub <topic>`` when absent."""
    groups: dict[tuple, dict] = {}
    for r in rows:
        node = r.get('node')
        dur = r.get('duration_ms')
        if not node or not isinstance(dur, (int, float)):
            continue
        topic = r.get('topic', '') or ''
        callback = r.get('callback') or (f'sub {topic}' if topic else 'callback')
        key = (node, callback, topic)
        g = groups.setdefault(key, {'durations': []})
        g['durations'].append(float(dur))

    out: list[CallbackStat] = []
    for (node, callback, topic), g in groups.items():
        d = g['durations']
        out.append(CallbackStat(
            node=node, callback=callback, topic=topic, count=len(d),
            mean_ms=round(sum(d) / len(d), 1), p95_ms=_percentile(d, 95),
            max_ms=round(max(d), 1)))
    out.sort(key=lambda c: (c.node, c.callback))
    return out


def load_duration_rows(path: str) -> list[dict]:
    """Read NDJSON callback-duration rows, tolerating truncated/garbage lines
    (as a Ctrl-C'd export leaves). Each row is one callback invocation."""
    rows: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def callbacks_from_trace_file(path: str) -> list[CallbackStat]:
    """Load and aggregate an NDJSON callback-duration trace into CallbackStats.

    Produce the file from a real session with ``ros2 trace`` → babeltrace2,
    emitting one ``{"node","callback","topic","duration_ms"}`` object per
    ``callback_end``/``callback_start`` pair. ros_graph_debugger then treats it
    exactly like the synthetic source."""
    return aggregate_callback_durations(load_duration_rows(path))


def _subs_of(node):
    """Accept a NodeInfo or a plain snapshot dict."""
    if isinstance(node, dict):
        return node['id'], node.get('subscribers', [])
    return node.id, node.subscribers


def synthesize_callbacks(nodes, *, slow_node: str | None = None,
                         slow_p95_ms: float = 210.0,
                         base_p95_ms: float = 18.0) -> list[CallbackStat]:
    """Build one subscription-callback stat per (node, subscribed topic).

    ``slow_node`` (a node id) gets a high p95 to model a callback that has
    become the pipeline's execution bottleneck. Returns ``CallbackStat``s; call
    ``dataclasses.asdict`` to embed them in a snapshot dict."""
    out: list[CallbackStat] = []
    for node in nodes:
        nid, subs = _subs_of(node)
        for topic in subs:
            slow = nid == slow_node
            p95 = slow_p95_ms if slow else base_p95_ms
            out.append(CallbackStat(
                node=nid, callback=f'sub {topic}', topic=topic, count=100,
                mean_ms=round(p95 * 0.55, 1), p95_ms=round(p95, 1),
                max_ms=round(p95 * 1.4, 1)))
    return out
