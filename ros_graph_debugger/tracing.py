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

from .model import CallbackStat


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
