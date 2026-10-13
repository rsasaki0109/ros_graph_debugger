"""Tier C tracing: per-callback execution-time stats.

The richest latency signal in ROS 2 is how long each callback actually runs —
that is what `ros2_tracing` / LTTng capture. A real adapter reads a CTF trace
(or a live LTTng session) and aggregates `callback_start`/`callback_end` pairs
into per-callback duration stats. Recording needs an LTTng-enabled build, so
it does not happen in every environment; converting a recorded trace only
needs babeltrace2 (`rgd trace convert`).

The pure half of that adapter lives here: resolving callback owners from the
trace's registration events, pairing `callback_start`/`callback_end` into
durations, and aggregating them into `CallbackStat`s. Reading the CTF itself
(babeltrace2) is in `trace_convert.py`. There is also a **synthetic source**,
mirroring how `replay.py` lets the whole product work without DDS:
`synthesize_callbacks` produces plausible `CallbackStat`s from the graph, with
an optional hot node whose callback p95 spikes — the same shape a real trace
yields.
"""

from __future__ import annotations

import json

from .model import CallbackStat, callback_source


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
    p95 / max. Optional ``source`` is retained; missing metadata stays unknown.
    Different sources are never pooled. ``callback`` defaults to ``sub <topic>``
    when absent. A duration row alone is not evidence of a paired trace."""
    groups: dict[tuple, dict] = {}
    for r in rows:
        node = r.get('node')
        dur = r.get('duration_ms')
        if not node or not isinstance(dur, (int, float)):
            continue
        topic = r.get('topic', '') or ''
        callback = r.get('callback') or (f'sub {topic}' if topic else 'callback')
        source = callback_source(r.get('source'))
        key = (node, callback, topic, source)
        g = groups.setdefault(key, {'durations': []})
        g['durations'].append(float(dur))

    out: list[CallbackStat] = []
    for (node, callback, topic, source), g in groups.items():
        d = g['durations']
        out.append(CallbackStat(
            node=node, callback=callback, topic=topic, count=len(d), source=source,
            mean_ms=round(sum(d) / len(d), 1), p95_ms=_percentile(d, 95),
            max_ms=round(max(d), 1)))
    out.sort(key=lambda c: (c.node, c.callback, c.topic, c.source or ''))
    return out


def pair_callback_events(events) -> list[dict]:
    """Pair ``ros2:callback_start`` / ``callback_end`` events into durations.

    This is the core of a CTF/LTTng adapter, kept pure so it is unit-tested
    without babeltrace2. ``events`` is an iterable of
    ``{kind: 'start'|'end', handle, t_ns}`` in time order; returns one
    ``{handle, duration_ms, t_ns}`` per matched pair. A per-handle stack handles
    reentrancy within a thread. Optional ``tid`` separates concurrent executions
    of the same handle; without it, pairing retains the legacy per-handle stack.
    Unmatched ends are ignored, unmatched starts are dropped."""
    pending: dict[object, list] = {}
    rows: list[dict] = []
    for e in events:
        handle = e.get('handle')
        t = e.get('t_ns')
        if handle is None or not isinstance(t, (int, float)):
            continue
        key = (handle, e.get('tid'))
        if e.get('kind') == 'start':
            pending.setdefault(key, []).append(t)
        elif e.get('kind') == 'end':
            stack = pending.get(key)
            if stack:
                t0 = stack.pop()
                rows.append({'handle': handle, 't_ns': t0,
                             'duration_ms': round((t - t0) / 1e6, 3)})
    return rows


def rows_with_owners(duration_rows, owners) -> list[dict]:
    """Attach ``{node, callback, topic}`` to paired durations by callback handle.

    ``owners`` maps a callback handle to its owner dict (resolved from the
    registration events a trace also carries). Rows whose handle has no known
    owner are dropped — we don't invent attribution."""
    out: list[dict] = []
    for r in duration_rows:
        o = owners.get(r['handle'])
        if not o or not o.get('node'):
            continue
        out.append({'node': o['node'], 'callback': o.get('callback', ''),
                    'topic': o.get('topic', ''), 'duration_ms': r['duration_ms']})
    return out


def _fq(name: str, namespace: str) -> str:
    ns = (namespace or '/').rstrip('/')
    return f'{ns}/{name}' if name else ''


def resolve_callback_owners(events) -> dict:
    """Resolve callback handles to ``{node, callback, topic}`` from the
    registration events of a ``ros2_tracing`` session.

    ``events`` are ``{name, pid, fields}`` dicts (see ``trace_convert``). The
    chains followed, as emitted by rclcpp:

    - subscription: ``rcl_node_init`` ← ``rcl_subscription_init`` (topic) ←
      ``rclcpp_subscription_init`` ← ``rclcpp_subscription_callback_added``
    - timer: ``rcl_timer_init`` (period) + ``rclcpp_timer_link_node`` +
      ``rclcpp_timer_callback_added``
    - service: ``rcl_service_init`` (name) + ``rclcpp_service_callback_added``

    Handles are pointers, unique only within a process, so every key is
    ``(pid, handle)``. A callback whose chain does not reach a node is left
    out — attribution is never guessed."""
    nodes, rcl_subs, cpp_subs, sub_cbs = {}, {}, {}, {}
    timers, timer_nodes, timer_cbs = {}, {}, {}
    services, service_cbs = {}, {}
    for e in events:
        name = e.get('name', '').removeprefix('ros2:')
        pid, f = e.get('pid'), e.get('fields', {})
        if name == 'rcl_node_init':
            nodes[(pid, f.get('node_handle'))] = _fq(f.get('node_name', ''),
                                                     f.get('namespace', '/'))
        elif name == 'rcl_subscription_init':
            rcl_subs[(pid, f.get('subscription_handle'))] = (
                f.get('node_handle'), f.get('topic_name', ''))
        elif name == 'rclcpp_subscription_init':
            cpp_subs[(pid, f.get('subscription'))] = f.get('subscription_handle')
        elif name == 'rclcpp_subscription_callback_added':
            sub_cbs[(pid, f.get('callback'))] = f.get('subscription')
        elif name == 'rcl_timer_init':
            timers[(pid, f.get('timer_handle'))] = f.get('period')
        elif name == 'rclcpp_timer_link_node':
            timer_nodes[(pid, f.get('timer_handle'))] = f.get('node_handle')
        elif name == 'rclcpp_timer_callback_added':
            timer_cbs[(pid, f.get('callback'))] = f.get('timer_handle')
        elif name == 'rcl_service_init':
            services[(pid, f.get('service_handle'))] = (
                f.get('node_handle'), f.get('service_name', ''))
        elif name == 'rclcpp_service_callback_added':
            service_cbs[(pid, f.get('callback'))] = f.get('service_handle')

    owners: dict = {}
    for (pid, cb), sub in sub_cbs.items():
        rcl = rcl_subs.get((pid, cpp_subs.get((pid, sub))))
        node = rcl and nodes.get((pid, rcl[0]))
        if node:
            owners[(pid, cb)] = {'node': node, 'topic': rcl[1],
                                 'callback': f'sub {rcl[1]}'}
    for (pid, cb), timer in timer_cbs.items():
        node = nodes.get((pid, timer_nodes.get((pid, timer))))
        if node:
            period = timers.get((pid, timer))
            label = f'timer {period / 1e6:g} ms' \
                if isinstance(period, (int, float)) else 'timer'
            owners[(pid, cb)] = {'node': node, 'topic': '', 'callback': label}
    for (pid, cb), srv in service_cbs.items():
        rcl = services.get((pid, srv))
        node = rcl and nodes.get((pid, rcl[0]))
        if node:
            owners[(pid, cb)] = {'node': node, 'topic': '',
                                 'callback': f'srv {rcl[1]}'}
    return owners


def trace_events_to_rows(events) -> tuple[list[dict], dict]:
    """Turn a session's ``{name, pid, t_ns, fields}`` events into per-invocation
    ``{node, callback, topic, duration_ms}`` rows (the NDJSON interchange
    format), plus counts that say how much of the trace was usable."""
    events = sorted(events, key=lambda e: e.get('t_ns') or 0)
    owners = resolve_callback_owners(events)
    marks = []
    for e in events:
        name = e.get('name', '').removeprefix('ros2:')
        if name in ('callback_start', 'callback_end'):
            marks.append({'kind': 'start' if name == 'callback_start' else 'end',
                          'handle': (e.get('pid'), e['fields'].get('callback')),
                          'tid': e.get('tid'),
                          't_ns': e.get('t_ns')})
    paired = pair_callback_events(marks)
    rows = rows_with_owners(paired, owners)
    for row in rows:
        row['source'] = 'trace'
    return rows, {'events': len(events), 'callbacks_resolved': len(owners),
                  'invocations': len(paired), 'attributed': len(rows),
                  'unattributed': len(paired) - len(rows)}


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
    """Load and aggregate a callback trace into CallbackStats.

    ``path`` is either a ``ros2 trace`` session directory (CTF, read with
    babeltrace2 via ``trace_convert``) or an NDJSON file with one
    ``{"node","callback","topic","duration_ms"}`` object per invocation (what
    ``rgd trace convert`` writes, including ``source``). Imported source is a
    declaration, not authenticated provenance. Legacy rows remain unknown."""
    import os
    if os.path.isdir(path):
        from .trace_convert import callbacks_from_trace_dir
        return callbacks_from_trace_dir(path)
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
                source='synthetic',
                mean_ms=round(p95 * 0.55, 1), p95_ms=round(p95, 1),
                max_ms=round(p95 * 1.4, 1)))
    return out
