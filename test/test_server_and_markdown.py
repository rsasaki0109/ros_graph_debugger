"""Server endpoints + Markdown briefing tests.

The HTTP endpoints are exercised against a real uvicorn server in a thread
(faithful to how users hit it, and free of TestClient/httpx version coupling).
"""

import json
import threading
import time
import urllib.request

import pytest
import uvicorn

from ros_graph_debugger.markdown import snapshot_to_markdown
from ros_graph_debugger.model import Issue, NodeInfo, RuntimeGraphStore, TopicInfo
from ros_graph_debugger.server import create_app


def _store():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/objects', type='std_msgs/msg/String',
                  publisher_count=1, subscriber_count=1,
                  publishers=['/detector'], subscribers=['/tracker'])
    t.probed = True
    t.rate_hz = 4.2
    t.bandwidth_bps = 12_000
    t.status = 'warning'
    n = NodeInfo(id='/detector', name='detector', publishers=['/objects'])
    n.cpu_percent = 95.0
    n.rss_bytes = 1_800_000_000
    n.process_mapping_confidence = 'medium'
    store.set_graph({'/detector': n, '/tracker': NodeInfo(id='/tracker', name='tracker')},
                    {'/objects': t})
    store.set_issues([Issue(
        id='x', severity='critical', kind='bottleneck',
        title='Likely bottleneck: detector',
        explanation='output dropped while inputs healthy',
        evidence=['/objects: 4.2 Hz (expected >= 10.0)', 'detector CPU: 95%'],
        suggested_actions=['Inspect callback duration'],
        related_nodes=['/detector'], related_topics=['/objects'])])
    return store


@pytest.fixture(scope='module')
def base_url():
    app = create_app(_store(), web_dir='/nonexistent',
                     profile_data={'name': 'autoware', 'groups': {}})
    port = 38939
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{port}'
    for _ in range(50):
        try:
            urllib.request.urlopen(url + '/api/v1/health', timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError('server did not start')
    yield url
    server.should_exit = True
    thread.join(timeout=5)


def _get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_rest_endpoints(base_url):
    assert _get_json(base_url + '/api/v1/health')['status'] == 'ok'
    assert _get_json(base_url + '/api/v1/profile')['name'] == 'autoware'

    snap = _get_json(base_url + '/api/v1/snapshot')
    assert len(snap['nodes']) == 2
    assert len(snap['topics']) == 1
    assert len(snap['issues']) == 1
    assert snap['edges'][0]['topic'] == '/objects'

    issues = _get_json(base_url + '/api/v1/issues')
    assert issues[0]['kind'] == 'bottleneck'


def test_markdown_briefing():
    md = snapshot_to_markdown(_store().snapshot())
    assert '# ROS Graph Debugger' in md
    assert 'Likely bottleneck: detector' in md
    assert 'Evidence:' in md
    # topic table renders rate + node metrics section renders cpu
    assert '4.2 Hz' in md
    assert '95%' in md


def test_snapshot_md_endpoint(base_url):
    with urllib.request.urlopen(base_url + '/api/v1/snapshot.md', timeout=5) as r:
        text = r.read().decode()
    assert 'Runtime Snapshot' in text
    assert 'Likely bottleneck: detector' in text
