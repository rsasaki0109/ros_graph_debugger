"""Load a profile pack (Autoware / Nav2 / MoveIt).

A profile groups topics into pipeline stages for the UI and declares per-topic
expectations (min rate, max age) that feed the issue engine.
"""

from __future__ import annotations

import yaml


def load_profile(path: str) -> tuple[dict, str]:
    with open(path, 'r') as f:
        data = yaml.safe_load(f) or {}

    name = data.get('name', 'profile')

    expected_min_rate: dict[str, float] = {}
    expected_max_age_ms: dict[str, float] = {}
    expected_callback_ms: dict[str, float] = {}
    for topic, exp in (data.get('expectations') or {}).items():
        if not isinstance(exp, dict):
            continue
        if 'min_rate_hz' in exp:
            expected_min_rate[topic] = float(exp['min_rate_hz'])
        if 'max_age_ms' in exp:
            expected_max_age_ms[topic] = float(exp['max_age_ms'])
        if 'max_callback_ms' in exp:
            expected_callback_ms[topic] = float(exp['max_callback_ms'])

    # Pattern expectations: {pattern, min_rate_hz?, max_age_ms?, max_callback_ms?}.
    min_rate_patterns: list[tuple[str, float]] = []
    max_age_patterns: list[tuple[str, float]] = []
    callback_ms_patterns: list[tuple[str, float]] = []
    for entry in (data.get('expectation_patterns') or []):
        if not isinstance(entry, dict) or 'pattern' not in entry:
            continue
        pat = entry['pattern']
        if 'min_rate_hz' in entry:
            min_rate_patterns.append((pat, float(entry['min_rate_hz'])))
        if 'max_age_ms' in entry:
            max_age_patterns.append((pat, float(entry['max_age_ms'])))
        if 'max_callback_ms' in entry:
            callback_ms_patterns.append((pat, float(entry['max_callback_ms'])))

    data['_expected_min_rate'] = expected_min_rate
    data['_expected_max_age_ms'] = expected_max_age_ms
    data['_expected_callback_ms'] = expected_callback_ms
    data['_min_rate_patterns'] = min_rate_patterns
    data['_max_age_patterns'] = max_age_patterns
    data['_callback_ms_patterns'] = callback_ms_patterns
    return data, name
