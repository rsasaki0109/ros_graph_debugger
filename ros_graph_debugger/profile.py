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
    for topic, exp in (data.get('expectations') or {}).items():
        if not isinstance(exp, dict):
            continue
        if 'min_rate_hz' in exp:
            expected_min_rate[topic] = float(exp['min_rate_hz'])
        if 'max_age_ms' in exp:
            expected_max_age_ms[topic] = float(exp['max_age_ms'])

    data['_expected_min_rate'] = expected_min_rate
    data['_expected_max_age_ms'] = expected_max_age_ms
    return data, name
