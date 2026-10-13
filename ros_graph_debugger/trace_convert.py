"""Read a `ros2_tracing` CTF trace with babeltrace2 (Tier C, turn-key).

`ros2 trace` records an LTTng session as CTF; this reads it with the
babeltrace2 Python bindings (`python3-bt2`, an optional dependency imported
lazily) and flattens each `ros2:*` event into a plain
``{name, pid, tid, t_ns, fields}`` dict. Everything after that — owner resolution,
start/end pairing, aggregation — is the pure, unit-tested code in
`tracing.py`.
"""

from __future__ import annotations

import json
import numbers
from typing import Iterator

from .tracing import aggregate_callback_durations, trace_events_to_rows


class TraceReaderUnavailable(RuntimeError):
    """babeltrace2's Python bindings are not importable."""


class TraceDataLoss(RuntimeError):
    """The CTF reader reported discarded events or packets."""


def _import_bt2():
    try:
        import bt2
    except ImportError as exc:
        raise TraceReaderUnavailable(
            'reading CTF traces needs the babeltrace2 Python bindings: '
            'sudo apt install python3-bt2 (use the system python3, which '
            'ROS 2 already uses)') from exc
    return bt2


def _value(field):
    if isinstance(field, numbers.Integral):
        return int(field)
    if isinstance(field, numbers.Real):
        return float(field)
    return str(field)


def _context_id(event, name):
    ctx = event.common_context_field
    if ctx is not None and name in ctx:
        return int(ctx[name])
    return None


def read_ctf_events(path: str) -> Iterator[dict]:
    """Yield ``{name, pid, tid, t_ns, fields}`` for every ``ros2:*`` event in the
    CTF trace at ``path`` (a `ros2 trace` session directory), in time order.

    Raise TraceDataLoss on any discarded-event or discarded-packet message;
    partial callback populations must not be presented as ordinary statistics.
    """
    bt2 = _import_bt2()
    for msg in bt2.TraceCollectionMessageIterator(path):
        if isinstance(msg, (bt2._DiscardedEventsMessageConst,
                            bt2._DiscardedPacketsMessageConst)):
            unit = 'events' if isinstance(msg, bt2._DiscardedEventsMessageConst) else 'packets'
            count = msg.count if msg.count is not None else 'unknown'
            raise TraceDataLoss(
                f'CTF trace reports discarded {unit} (count: {count}); '
                'callback statistics were not produced. Re-record with larger '
                'LTTng buffers or fewer enabled events.')
        if type(msg) is not bt2._EventMessageConst:
            continue
        event = msg.event
        if not event.name.startswith('ros2:'):
            continue
        payload = event.payload_field
        yield {
            'name': event.name,
            'pid': _context_id(event, 'vpid'),
            'tid': _context_id(event, 'vtid'),
            't_ns': msg.default_clock_snapshot.ns_from_origin,
            'fields': {k: _value(v) for k, v in payload.items()}
            if payload is not None else {},
        }


def convert_trace(path: str) -> tuple[list[dict], dict]:
    """CTF trace directory -> (per-invocation duration rows, counts)."""
    return trace_events_to_rows(read_ctf_events(path))


def write_rows(rows: list[dict], out_path: str) -> None:
    with open(out_path, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def callbacks_from_trace_dir(path: str):
    """CTF trace directory -> aggregated ``CallbackStat``s (for the agent)."""
    rows, _ = convert_trace(path)
    return aggregate_callback_durations(rows)
