"""MCP proxy tests.

The MCP server is a thin proxy over the agent REST API. We don't need the MCP
SDK installed to test it: the proxy logic (``fetch`` / ``post`` / ``READ_TOOLS``)
is SDK-free, so we run it against a real uvicorn server in a thread and assert
every advertised tool path is live and the write tool round-trips into config.
"""

import json
import threading
import time
import urllib.request

import pytest
import uvicorn

from ros_graph_debugger import mcp_server
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.model import (CallbackStat, Issue, NodeInfo,
                                      RuntimeGraphStore, TfEdge, TopicInfo)
from ros_graph_debugger.server import create_app


def _store():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/objects', type='std_msgs/msg/String',
                  publisher_count=1, subscriber_count=1,
                  publishers=['/detector'], subscribers=['/tracker'])
    t.probed = True
    t.rate_hz = 4.2
    store.set_graph(
        {'/detector': NodeInfo(id='/detector', name='detector',
                               publishers=['/objects']),
         '/tracker': NodeInfo(id='/tracker', name='tracker')},
        {'/objects': t})
    store.set_tf({('map', 'base_link'): TfEdge('map', 'base_link',
                 age_ms=3000, status='critical')})
    store.set_callbacks([CallbackStat('/detector', 'objects',
                         topic='/objects', count=5, p95_ms=120)])
    store.set_issues([Issue('stale-tf', 'warning', 'tf_stale', 'Old transform',
                     evidence=['age: 3000 ms'], related_frames=['map', 'base_link'])])
    return store


@pytest.fixture(scope='module')
def agent():
    thresholds = Thresholds()
    app = create_app(_store(), web_dir='/nonexistent',
                     profile_data={'name': 'autoware', 'groups': {}},
                     thresholds=thresholds)
    port = 38941
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{port}'
    for _ in range(50):
        try:
            urllib.request.urlopen(base + '/api/v1/health', timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError('server did not start')
    yield base, thresholds
    server.should_exit = True
    thread.join(timeout=5)


def test_every_read_tool_path_is_live(agent):
    base, _ = agent
    # Each advertised read tool must resolve against the running agent.
    for name, path, doc in mcp_server.READ_TOOLS:
        body = mcp_server.fetch(path, base=base)
        assert body, f'{name} ({path}) returned empty body'
    # The Markdown briefing is Markdown; the rest are JSON.
    for name, path, doc in mcp_server.READ_TOOLS:
        if path.endswith('.md'):
            continue
        json.loads(mcp_server.fetch(path, base=base))


def test_read_tool_paths_are_documented():
    # Every read tool path must appear in the canonical API doc (guards typos).
    import os
    docs = os.path.join(os.path.dirname(__file__), '..', 'docs', 'api.md')
    with open(docs) as f:
        doc = f.read()
    for name, path, _ in mcp_server.READ_TOOLS:
        assert path in doc, f'{name} proxies undocumented {path}'


def test_runtime_snapshot_keeps_cross_group_evidence_and_unknown_values(agent):
    base, _ = agent
    path = next(path for name, path, _ in mcp_server.READ_TOOLS
                if name == 'get_runtime_snapshot')
    snapshot = json.loads(mcp_server.fetch(path, base=base))
    assert snapshot['timestamp'] > 0
    detector = next(n for n in snapshot['nodes'] if n['id'] == '/detector')
    assert detector['cpu_percent'] is None
    assert detector['process_mapping_confidence'] == 'none'
    assert snapshot['topics'][0]['rate_hz'] == 4.2
    assert snapshot['tf_edges'][0]['age_ms'] == 3000
    assert snapshot['callbacks'][0]['p95_ms'] == 120
    issue = snapshot['issues'][0]
    assert issue['kind'] == 'tf_stale' and issue['evidence'] == ['age: 3000 ms']
    assert issue['related_frames'] == ['map', 'base_link']


def test_get_node_briefing_focuses_the_graph(agent):
    base, _ = agent
    # The focused briefing path the get_node_briefing tool builds is live and
    # scopes the Markdown to the requested node.
    from urllib.parse import quote
    md = mcp_server.fetch('/api/v1/snapshot.md?focus=' + quote('/detector'),
                          base=base)
    assert 'Focused on **/detector**' in md
    assert '/objects' in md


def test_get_pipeline_path_traces_the_constraining_route(agent):
    base, _ = agent
    from urllib.parse import quote
    p = json.loads(mcp_server.fetch('/api/v1/path?target=' + quote('/detector'),
                                    base=base))
    assert p['pivot'] == '/detector'
    assert '/detector' in p['nodes'] and '/tracker' in p['nodes']


def test_set_expected_rate_round_trips_into_config(agent):
    base, thresholds = agent
    mcp_server.post('/api/v1/config',
                    {'expected_min_rate': {'/objects': 15.0}}, base=base)
    # The live thresholds object the issue engine reads is now updated.
    assert thresholds.min_rate_for('/objects') == 15.0
    # And the agent reports it back through the read tool.
    cfg = json.loads(mcp_server.fetch('/api/v1/config', base=base))
    assert cfg['expected_min_rate']['/objects'] == 15.0
