"""Application-reported processing time (the rest of latency Tier B).

Many stacks publish how long each node spent per cycle — Autoware nodes
publish ``~/debug/processing_time_ms`` (a ``Float64Stamped``) from nearly every
component. That is the node's own measurement of its execution time, without
any tracing, so it becomes a ``CallbackStat`` with ``callback =
'processing_time'`` and ``topic`` = the debug topic. The existing machinery
then applies unchanged: the stage-aware ``max_callback_ms`` budget (resolved
from the debug topic, which sits under the node's stage namespace), the
``slow_callback`` issue, and the pipeline path (which falls back to a node's
processing time when it has no per-subscription callback time).

Which topics: regexes from the profile (``processing_time_patterns``) or
``agent --processing-time-topic``. Values are read from a numeric ``data``
field (``std_msgs/Float64``, ``Float64Stamped``, …), in milliseconds.
"""

from __future__ import annotations

import re
from collections import deque

from .model import CallbackStat

LABEL = 'processing_time'
DEFAULT_WINDOW = 100


def compile_patterns(patterns) -> list:
    return [re.compile(p) for p in (patterns or [])]


def matches(topic: str, compiled) -> bool:
    return any(rgx.search(topic) for rgx in compiled)


def value_ms(msg) -> float | None:
    """The numeric ``data`` field of a processing-time message, else None."""
    v = getattr(msg, 'data', None)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def node_for(topic: str, publishers) -> str | None:
    """The node reporting ``topic``: its (single) publisher when the graph
    knows it, else — for the ``~/debug/...`` convention, where ``~`` expands
    to the node's own name — the namespace before ``/debug/``."""
    pubs = sorted(publishers or [])
    if len(pubs) == 1:
        return pubs[0]
    if '/debug/' in topic:
        return topic.split('/debug/', 1)[0] or None
    return None


def _p95(values):
    ordered = sorted(values)
    return round(float(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]), 1)


class ProcessingTimes:
    """Recent values per processing-time topic."""

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self.window = window
        self._values: dict[str, deque] = {}

    def add(self, topic: str, value: float | None) -> None:
        if value is None:
            return
        self._values.setdefault(topic, deque(maxlen=self.window)).append(value)

    def forget(self, topic: str) -> None:
        self._values.pop(topic, None)

    def stats(self, publishers_of=lambda topic: ()) -> list[CallbackStat]:
        out = []
        for topic, vals in sorted(self._values.items()):
            node = node_for(topic, publishers_of(topic))
            if not node or not vals:
                continue
            v = list(vals)
            out.append(CallbackStat(
                node=node, callback=LABEL, topic=topic, count=len(v),
                mean_ms=round(sum(v) / len(v), 1), p95_ms=_p95(v),
                max_ms=round(max(v), 1)))
        return out
