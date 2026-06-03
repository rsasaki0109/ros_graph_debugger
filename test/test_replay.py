"""ReplayStore, the scripted demo scenario, and the replay HTTP endpoints."""

import json
import threading
import time
import urllib.request

import pytest
import uvicorn

from ros_graph_debugger.replay import ReplayStore, build_demo_recording
from ros_graph_debugger.server import create_app


def test_replaystore_advance_loop_seek():
    snaps = [{'timestamp': float(i), 'nodes': [], 'topics': [], 'edges': [],
              'tf_edges': [], 'diagnostics': [], 'issues': []} for i in range(4)]
    rs = ReplayStore(snaps, loop=True)
    assert rs.total == 4
    assert rs.snapshot().to_dict()['timestamp'] == 0.0

    rs.advance()
    assert rs.snapshot().to_dict()['timestamp'] == 1.0

    rs.seek(3)
    assert rs.state()['index'] == 3
    assert rs.state()['playing'] is False  # seek pauses

    rs.set_playing(True)
    rs.advance()  # wraps because loop=True
    assert rs.state()['index'] == 0


def test_replaystore_no_loop_stops_at_end():
    snaps = [{'timestamp': float(i)} for i in range(3)]
    rs = ReplayStore(snaps, loop=False)
    rs.seek(2)
    rs.set_playing(True)
    rs.advance()
    st = rs.state()
    assert st['index'] == 2
    assert st['playing'] is False


def test_demo_recording_has_bottleneck_window():
    header, snaps = build_demo_recording()
    assert header['profile']['name'] == 'autoware'
    assert len(snaps) >= 20

    def has_bottleneck(snap):
        return any(i['kind'] == 'bottleneck' for i in snap['issues'])

    # Healthy at the start, stalled in the middle.
    assert not has_bottleneck(snaps[0])
    assert any(has_bottleneck(s) for s in snaps)
    # The objects topic rate actually drops in the stalled window.
    rates = [t['rate_hz'] for s in snaps for t in s['topics']
             if t['name'].endswith('/objects')]
    assert min(rates) < 5.0 and max(rates) >= 10.0
    # Every snapshot is a complete, renderable graph.
    for s in snaps:
        assert s['nodes'] and s['topics'] and 'edges' in s


def test_demo_recording_has_tf_tree_and_diagnostics():
    _, snaps = build_demo_recording()
    # TF forms a small tree (map -> base_link -> sensors), with statics present.
    tf = snaps[0]['tf_edges']
    parents = {e['parent'] for e in tf}
    children = {e['child'] for e in tf}
    assert 'map' in parents and 'base_link' in children
    assert {e['child'] for e in tf if e['parent'] == 'base_link'}  # sensors hang off base_link
    assert any(e['static'] for e in tf) and any(not e['static'] for e in tf)
    # The dynamic map->base_link goes critical during the stall, ok otherwise.
    def mbl(s):
        return next(e for e in s['tf_edges']
                    if e['parent'] == 'map' and e['child'] == 'base_link')
    assert mbl(snaps[0])['status'] == 'ok'
    assert any(mbl(s)['status'] == 'critical' for s in snaps)

    # Diagnostics are present and escalate during the stall window.
    assert all(s['diagnostics'] for s in snaps)
    worst = [max(d['level'] for d in s['diagnostics']) for s in snaps]
    assert worst[0] == 0 and max(worst) >= 2  # clean start, ERROR mid-run


@pytest.fixture(scope='module')
def replay_url():
    header, snaps = build_demo_recording()
    rs = ReplayStore(snaps, loop=True)
    app = create_app(rs, web_dir='/nonexistent',
                     profile_data=header['profile'],
                     replay=rs, replay_interval=1000)  # ticker won't fire in-test
    port = 38941
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                           log_level='error'))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f'http://127.0.0.1:{port}'
    for _ in range(50):
        try:
            urllib.request.urlopen(base + '/api/v1/health', timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError('replay server did not start')
    yield base
    server.should_exit = True
    t.join(timeout=5)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def _post(url):
    req = urllib.request.Request(url, method='POST')
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_replay_endpoints(replay_url):
    state = _get(replay_url + '/api/v1/replay')
    assert state['mode'] == 'replay'
    assert state['total'] >= 20

    # Seek into the stalled window and confirm the served snapshot reflects it.
    seek = _post(replay_url + '/api/v1/replay/seek?index=18')
    assert seek['index'] == 18
    snap = _get(replay_url + '/api/v1/snapshot')
    assert any(i['kind'] == 'bottleneck' for i in snap['issues'])

    # Seek back to the start: clean.
    _post(replay_url + '/api/v1/replay/seek?index=0')
    snap0 = _get(replay_url + '/api/v1/snapshot')
    assert not any(i['kind'] == 'bottleneck' for i in snap0['issues'])

    # Profile still served for stage grouping.
    assert _get(replay_url + '/api/v1/profile')['name'] == 'autoware'

    # Markdown briefing works on the replayed frame too.
    with urllib.request.urlopen(replay_url + '/api/v1/snapshot.md', timeout=5) as r:
        assert 'Runtime Snapshot' in r.read().decode()
