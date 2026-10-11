"""Latency Tier B: rclcpp topic statistics (`statistics_msgs/MetricsMessage`).

A subscription created with topic statistics enabled publishes, once per
window, the period between received messages (`message_period`) and their
header-stamp age (`message_age`), in ms. That gives a rate and an age for
topics we never probe ourselves — notably the large sensor topics the agent
refuses to subscribe to — at no extra cost to the agent.

The catch: the message names the *node* (`measurement_source_name`), not the
topic. So attribution is explicit about how it was made:

- ``explicit``   — the stats topic was mapped to a data topic by the user
                   (``agent --statistics-topic /stats/objects=/objects``);
- ``single_sub`` — the node has exactly one data subscription, so it must be it;
- ``ambiguous``  — the node subscribes to several topics; left unattributed;
- ``unknown_node`` — no (unique) node in the graph by that name.

Only attributed stats feed topic metrics. Pure functions over duck-typed
messages and model objects, so this is testable without ROS.
"""

from __future__ import annotations

import time

PERIOD = 'message_period'
AGE = 'message_age'

# statistics_msgs/StatisticDataType
_DATA_TYPES = {1: 'avg', 2: 'min', 3: 'max', 4: 'stddev', 5: 'count'}

# Subscriptions every rclcpp/rclpy node may carry; never the measured one.
_INFRA_TOPICS = {'/parameter_events', '/rosout', '/clock'}

# Drop stats we have not heard about for this long (publisher gone / disabled).
EXPIRY_S = 10.0


def _stamp_to_epoch(stamp) -> float | None:
    sec = getattr(stamp, 'sec', None)
    nanosec = getattr(stamp, 'nanosec', None)
    if sec is None or nanosec is None:
        return None
    return sec + nanosec * 1e-9


def parse_metrics(msg, stats_topic: str = '/statistics',
                  topic_hint: str | None = None,
                  received: float | None = None) -> dict | None:
    """Flatten a MetricsMessage into a plain dict, or None if it is not a
    topic-statistics metric we understand."""
    metric = getattr(msg, 'metrics_source', '')
    if metric not in (PERIOD, AGE):
        return None
    row = {
        'source': getattr(msg, 'measurement_source_name', ''),
        'metric': metric,
        'unit': getattr(msg, 'unit', 'ms'),
        'stats_topic': stats_topic,
        'topic_hint': topic_hint,
        'window_stop': _stamp_to_epoch(getattr(msg, 'window_stop', None)),
        'received': time.time() if received is None else received,
        'avg': None, 'min': None, 'max': None, 'stddev': None, 'count': 0,
    }
    for point in getattr(msg, 'statistics', []) or []:
        key = _DATA_TYPES.get(int(getattr(point, 'data_type', 0)))
        if key is None:
            continue
        value = float(getattr(point, 'data', float('nan')))
        if value != value:  # NaN: no samples in this window
            continue
        row[key] = int(value) if key == 'count' else value
    return row


class StatisticsBuffer:
    """Latest window per (stats topic, source node, metric)."""

    def __init__(self, expiry_s: float = EXPIRY_S) -> None:
        self.expiry_s = expiry_s
        self._rows: dict[tuple[str, str, str], dict] = {}

    def ingest(self, row: dict | None) -> None:
        if row is None:
            return
        self._rows[(row['stats_topic'], row['source'], row['metric'])] = row

    def rows(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        self._rows = {k: r for k, r in self._rows.items()
                      if now - r['received'] <= self.expiry_s}
        return list(self._rows.values())


def parse_statistics_topics(specs: list[str] | None) -> dict[str, str | None]:
    """``agent --statistics-topic STATS[=DATA]`` specs -> {stats: data|None}.
    ``None`` means the default ``/statistics`` with node-based attribution."""
    out: dict[str, str | None] = {}
    for spec in specs if specs is not None else ['/statistics']:
        stats, _, data = spec.partition('=')
        if stats:
            out[stats] = data or None
    return out


def _resolve_node(source: str, nodes: dict) -> str | None:
    if source.startswith('/'):
        return source if source in nodes else None
    hits = [nid for nid, n in nodes.items()
            if getattr(n, 'name', '') == source or nid == '/' + source]
    return hits[0] if len(hits) == 1 else None


def attribute(rows: list[dict], nodes: dict, topics: dict) -> list[dict]:
    """Return rows enriched with ``node``, ``topic`` and ``attribution``."""
    out = []
    for r in rows:
        node = _resolve_node(r['source'], nodes)
        topic, how = None, 'unknown_node'
        if r.get('topic_hint'):
            topic, how = r['topic_hint'], 'explicit'
        elif node is not None:
            subs = sorted(t for t in getattr(nodes[node], 'subscribers', [])
                          if t not in _INFRA_TOPICS)
            if len(subs) == 1:
                topic, how = subs[0], 'single_sub'
            else:
                how = 'ambiguous'
        if topic is not None and topic not in topics:
            topic, how = None, 'unknown_topic'
        out.append({**r, 'node': node, 'topic': topic, 'attribution': how})
    return out


def _p95_estimate(row: dict) -> float | None:
    """Rough p95 from a window's avg/stddev (normal approximation), never above
    the observed max. Topic statistics carry no percentiles."""
    avg = row.get('avg')
    if avg is None:
        return None
    est = avg + 1.645 * (row.get('stddev') or 0.0)
    mx = row.get('max')
    return round(min(est, mx) if mx is not None else est, 1)


def topic_overlay(attributed: list[dict]) -> dict[str, dict]:
    """Per topic, the metrics to overlay: the *slowest* subscriber's rate and
    the *oldest* age (the constraining view when several subscribers report).

    An empty ``message_period`` window means the subscriber saw no message
    pair during it: that is 0 Hz, not "unknown" — a dead sensor must still
    trip its rate expectation."""
    out: dict[str, dict] = {}
    for r in attributed:
        t = r.get('topic')
        if not t:
            continue
        if r['metric'] == PERIOD:
            if not r.get('count'):
                rate = 0.0
            elif r.get('avg') and r['avg'] > 0:
                rate = round(1000.0 / r['avg'], 2)
            else:
                continue
            o = out.setdefault(t, {})
            if o.get('rate_hz') is None or rate < o['rate_hz']:
                o['rate_hz'] = rate
        elif r['metric'] == AGE and r.get('count') and r.get('avg') is not None:
            o = out.setdefault(t, {})
            age, p95 = round(r['avg'], 1), _p95_estimate(r)
            if o.get('header_age_ms') is None or age > o['header_age_ms']:
                o['header_age_ms'] = age
            if p95 is not None and (o.get('header_age_p95_ms') is None
                                    or p95 > o['header_age_p95_ms']):
                o['header_age_p95_ms'] = p95
    return out


def public_row(r: dict) -> dict:
    """The JSON shape exposed in snapshots (ms values rounded)."""
    def rnd(v):
        return None if v is None else round(v, 2)
    return {
        'node': r.get('node'), 'source': r['source'], 'topic': r.get('topic'),
        'attribution': r.get('attribution'), 'metric': r['metric'],
        'unit': r['unit'], 'stats_topic': r['stats_topic'],
        'avg': rnd(r['avg']), 'min': rnd(r['min']), 'max': rnd(r['max']),
        'stddev': rnd(r['stddev']), 'count': r['count'],
        'window_stop': r['window_stop'],
    }
