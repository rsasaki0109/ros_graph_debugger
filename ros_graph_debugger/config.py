"""Runtime configuration: probe policy and analysis thresholds.

Kept free of rclpy so the web server, profile loader, and tests can all share
it. Expectations support both exact topic names and regex patterns, so a single
rule like ``^/localization/.*`` can set a floor for a whole Autoware stage.
``Thresholds`` is mutated live by ``POST /api/v1/config``; the issue engine
reads it on the spin thread, so writes replace whole values (GIL-atomic) rather
than mutating in place mid-read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ProbeConfig:
    enabled: bool = True
    include_patterns: list[str] = field(default_factory=list)  # explicit allow
    regex: str = ''
    allow_large: bool = False
    max_topics: int = 12
    window: int = 50  # samples kept per topic for rate/size stats


@dataclass
class Thresholds:
    high_bandwidth_bps: float = 50_000_000  # 50 MB/s
    large_msg_bytes: float = 1_000_000      # 1 MB
    stale_topic_ms: float = 2000.0
    tf_stale_ms: float = 1000.0
    high_cpu_percent: float = 90.0
    high_rss_bytes: float = 2_000_000_000
    slow_callback_ms: float = 100.0  # callback p95 above this is flagged (Tier C)

    # Exact-topic expectations (highest priority).
    expected_min_rate: dict[str, float] = field(default_factory=dict)
    expected_max_age_ms: dict[str, float] = field(default_factory=dict)
    # Regex-pattern expectations: list of (pattern, value), first match wins.
    min_rate_patterns: list[tuple[str, float]] = field(default_factory=list)
    max_age_patterns: list[tuple[str, float]] = field(default_factory=list)

    # Compiled cache (rebuilt whenever patterns change via set_patterns).
    _min_rate_re: list = field(default_factory=list, repr=False)
    _max_age_re: list = field(default_factory=list, repr=False)

    def __post_init__(self):
        self._recompile()

    # -- pattern handling --------------------------------------------------- #
    def _recompile(self) -> None:
        def compile_pairs(pairs):
            out = []
            for pat, val in pairs:
                try:
                    out.append((re.compile(pat), float(val)))
                except (re.error, TypeError, ValueError):
                    continue
            return out
        self._min_rate_re = compile_pairs(self.min_rate_patterns)
        self._max_age_re = compile_pairs(self.max_age_patterns)

    def set_patterns(self, min_rate=None, max_age=None) -> None:
        if min_rate is not None:
            self.min_rate_patterns = list(min_rate)
        if max_age is not None:
            self.max_age_patterns = list(max_age)
        self._recompile()

    # -- lookups (exact first, then first matching pattern) ----------------- #
    def min_rate_for(self, topic: str):
        if topic in self.expected_min_rate:
            return self.expected_min_rate[topic]
        for rgx, val in self._min_rate_re:
            if rgx.search(topic):
                return val
        return None

    def max_age_for(self, topic: str):
        if topic in self.expected_max_age_ms:
            return self.expected_max_age_ms[topic]
        for rgx, val in self._max_age_re:
            if rgx.search(topic):
                return val
        return None


# Scalar threshold fields that POST /api/v1/config may set.
_SCALAR_FIELDS = (
    'high_bandwidth_bps', 'large_msg_bytes', 'stale_topic_ms', 'tf_stale_ms',
    'high_cpu_percent', 'high_rss_bytes', 'slow_callback_ms')


def apply_config(thresholds: Thresholds, payload: dict) -> dict:
    """Apply a partial config payload to a live Thresholds. Returns what changed.

    Recognized keys:
      - scalar threshold names (see _SCALAR_FIELDS)
      - expected_min_rate / expected_max_age_ms: {topic: value} (merged)
      - min_rate_patterns / max_age_patterns: [[pattern, value], ...] (replaced)
    """
    changed = {}
    for key in _SCALAR_FIELDS:
        if key in payload:
            try:
                setattr(thresholds, key, float(payload[key]))
                changed[key] = getattr(thresholds, key)
            except (TypeError, ValueError):
                pass

    if isinstance(payload.get('expected_min_rate'), dict):
        merged = dict(thresholds.expected_min_rate)
        for t, v in payload['expected_min_rate'].items():
            try:
                merged[t] = float(v)
            except (TypeError, ValueError):
                continue
        thresholds.expected_min_rate = merged
        changed['expected_min_rate'] = merged

    if isinstance(payload.get('expected_max_age_ms'), dict):
        merged = dict(thresholds.expected_max_age_ms)
        for t, v in payload['expected_max_age_ms'].items():
            try:
                merged[t] = float(v)
            except (TypeError, ValueError):
                continue
        thresholds.expected_max_age_ms = merged
        changed['expected_max_age_ms'] = merged

    if isinstance(payload.get('min_rate_patterns'), list):
        thresholds.set_patterns(min_rate=[tuple(p) for p in payload['min_rate_patterns']
                                          if len(p) == 2])
        changed['min_rate_patterns'] = thresholds.min_rate_patterns
    if isinstance(payload.get('max_age_patterns'), list):
        thresholds.set_patterns(max_age=[tuple(p) for p in payload['max_age_patterns']
                                         if len(p) == 2])
        changed['max_age_patterns'] = thresholds.max_age_patterns

    return changed


def config_to_dict(thresholds: Thresholds) -> dict:
    return {
        'high_bandwidth_bps': thresholds.high_bandwidth_bps,
        'large_msg_bytes': thresholds.large_msg_bytes,
        'stale_topic_ms': thresholds.stale_topic_ms,
        'tf_stale_ms': thresholds.tf_stale_ms,
        'high_cpu_percent': thresholds.high_cpu_percent,
        'high_rss_bytes': thresholds.high_rss_bytes,
        'slow_callback_ms': thresholds.slow_callback_ms,
        'expected_min_rate': thresholds.expected_min_rate,
        'expected_max_age_ms': thresholds.expected_max_age_ms,
        'min_rate_patterns': thresholds.min_rate_patterns,
        'max_age_patterns': thresholds.max_age_patterns,
    }
