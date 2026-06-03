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
from ros_graph_debugger.model import NodeInfo, RuntimeGraphStore, TopicInfo
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


def test_get_node_briefing_focuses_the_graph(agent):
    base, _ = agent
    # The focused briefing path the get_node_briefing tool builds is live and
    # scopes the Markdown to the requested node.
    from urllib.parse import quote
    md = mcp_server.fetch('/api/v1/snapshot.md?focus=' + quote('/detector'),
                          base=base)
    assert 'Focused on **/detector**' in md
    assert '/objects' in md


def test_set_expected_rate_round_trips_into_config(agent):
    base, thresholds = agent
    mcp_server.post('/api/v1/config',
                    {'expected_min_rate': {'/objects': 15.0}}, base=base)
    # The live thresholds object the issue engine reads is now updated.
    assert thresholds.min_rate_for('/objects') == 15.0
    # And the agent reports it back through the read tool.
    cfg = json.loads(mcp_server.fetch('/api/v1/config', base=base))
    assert cfg['expected_min_rate']['/objects'] == 15.0
