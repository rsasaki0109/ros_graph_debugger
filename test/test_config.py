"""Thresholds lookups, apply_config, pattern expectations, profile parsing,
analyzer firing via patterns, and the live /api/v1/config endpoint."""

import json
import threading
import time
import urllib.request

import pytest
import uvicorn

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import (
    Thresholds,
    apply_config,
    config_to_dict,
)
from ros_graph_debugger.model import NodeInfo, RuntimeGraphStore, TopicInfo
from ros_graph_debugger.paths import find_profile
from ros_graph_debugger.profile import load_profile
from ros_graph_debugger.server import create_app


def test_min_rate_for_exact_then_pattern():
    t = Thresholds(expected_min_rate={'/exact': 30.0})
    t.set_patterns(min_rate=[('^/perception/.*', 10.0), ('^/control/.*', 5.0)])
    assert t.min_rate_for('/exact') == 30.0           # exact wins
    assert t.min_rate_for('/perception/objects') == 10.0  # first pattern
    assert t.min_rate_for('/control/cmd') == 5.0
    assert t.min_rate_for('/unknown') is None


def test_apply_config_scalars_and_merges():
    t = Thresholds()
    changed = apply_config(t, {
        'high_cpu_percent': 75,
        'expected_min_rate': {'/objects': 12},
        'min_rate_patterns': [['^/planning/.*', 8]],
        'bogus_key': 1,
    })
    assert t.high_cpu_percent == 75.0
    assert t.min_rate_for('/objects') == 12.0
    assert t.min_rate_for('/planning/trajectory') == 8.0
    assert 'high_cpu_percent' in changed
    assert 'bogus_key' not in changed
    # round-trips through config_to_dict
    d = config_to_dict(t)
    assert d['high_cpu_percent'] == 75.0
    assert d['expected_min_rate']['/objects'] == 12.0


def test_apply_config_ignores_bad_values():
    t = Thresholds()
    before = t.high_cpu_percent
    apply_config(t, {'high_cpu_percent': 'not a number'})
    assert t.high_cpu_percent == before


def test_autoware_profile_has_pattern_expectations():
    data, _ = load_profile(find_profile('autoware'))
    pats = data['_min_rate_patterns']
    assert any(p == '^/control/command/.*' for p, _ in pats)


def test_analyzer_fires_rate_drop_via_pattern():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/control/command/control_cmd', publisher_count=1,
                  subscriber_count=1, publishers=['/ctl'], subscribers=['/veh'])
    store.set_graph({'/ctl': NodeInfo(id='/ctl', name='ctl'),
                     '/veh': NodeInfo(id='/veh', name='veh')},
                    {t.name: t})
    store.update_topic_metrics(t.name, rate_hz=3.0)  # below pattern floor of 10

    thr = Thresholds()
    thr.set_patterns(min_rate=[('^/control/command/.*', 10.0)])
    kinds = {i.kind for i in analyze(store, thr)}
    assert 'rate_drop' in kinds


@pytest.fixture(scope='module')
def cfg_url():
    store = RuntimeGraphStore()
    thresholds = Thresholds()
    app = create_app(store, web_dir='/nonexistent', thresholds=thresholds)
    port = 38943
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                           log_level='error'))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base = f'http://127.0.0.1:{port}'
    for _ in range(50):
        try:
            urllib.request.urlopen(base + '/api/v1/health', timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError('config server did not start')
    yield base, thresholds
    server.should_exit = True
    th.join(timeout=5)


def test_config_endpoint_live_update(cfg_url):
    base, thresholds = cfg_url
    # GET reflects defaults
    with urllib.request.urlopen(base + '/api/v1/config', timeout=5) as r:
        cfg = json.loads(r.read())
    assert cfg['high_cpu_percent'] == 90.0

    # POST changes the live thresholds object
    body = json.dumps({'high_cpu_percent': 60,
                       'expected_min_rate': {'/objects': 15}}).encode()
    req = urllib.request.Request(base + '/api/v1/config', data=body,
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    with urllib.request.urlopen(req, timeout=5) as r:
        out = json.loads(r.read())
    assert 'high_cpu_percent' in out['changed']
    # the SAME object the issue engine reads was mutated
    assert thresholds.high_cpu_percent == 60.0
    assert thresholds.min_rate_for('/objects') == 15.0
