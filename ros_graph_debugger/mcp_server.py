"""MCP server exposing a running ros_graph_debugger agent to AI assistants.

This is a thin stdio MCP server that proxies the agent's REST API, so an AI
(Claude, etc.) can pull the live ROS graph, issues, and metrics as tools — and
adjust expected rates at runtime.

Install the SDK first:   pip install "mcp[cli]"
Run:                     python -m ros_graph_debugger.mcp_server
Register with Claude Code:
    claude mcp add ros-graph -- python -m ros_graph_debugger.mcp_server

Set RGD_BASE to point at a non-default agent URL (default http://127.0.0.1:3939).

The proxy logic (``fetch`` / ``post`` / the ``READ_TOOLS`` table) is kept free
of the MCP SDK so it can be tested against a real agent without installing mcp.
"""

from __future__ import annotations

import json
import os
import urllib.request
from urllib.parse import quote

BASE = os.environ.get('RGD_BASE', 'http://127.0.0.1:3939')

# Read-only proxy tools: (tool_name, path, description). main() registers each
# one with the MCP SDK; tests iterate this table to verify every path is live.
READ_TOOLS = [
    ('get_runtime_briefing', '/api/v1/snapshot.md',
     'Get an AI-ready Markdown briefing of the live ROS 2 system: issues to '
     'look at next, plus topic and node metrics.'),
    ('get_issues', '/api/v1/issues',
     'Get the current detected issues (bottlenecks, stale topics, QoS '
     'mismatches, ...) as JSON, with evidence and suggested actions.'),
    ('get_graph', '/api/v1/graph',
     'Get the live ROS graph (nodes, topics, edges) as JSON.'),
    ('get_topics', '/api/v1/topics',
     'Get all topics with rate/bandwidth/QoS metrics as JSON.'),
    ('get_nodes', '/api/v1/nodes',
     'Get all nodes with CPU/memory and pub/sub lists as JSON.'),
    ('get_tf', '/api/v1/tf',
     'Get the TF transform edges with freshness (age) as JSON.'),
    ('get_diagnostics', '/api/v1/diagnostics',
     'Get the latest /diagnostics statuses (level, name, message) as JSON.'),
    ('get_config', '/api/v1/config',
     'Get the current analysis thresholds and expected-rate config as JSON.'),
]


def fetch(path: str, base: str | None = None) -> str:
    """GET ``path`` from the agent and return the raw response body."""
    with urllib.request.urlopen((base or BASE) + path, timeout=5) as r:
        return r.read().decode()


def post(path: str, payload: dict, base: str | None = None) -> str:
    """POST a JSON ``payload`` to the agent and return the raw response body."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        (base or BASE) + path, data=data, method='POST',
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode()


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        raise SystemExit(
            'The MCP SDK is not installed. Run:  pip install "mcp[cli]"')

    mcp = FastMCP('ros-graph-debugger')

    # Register the read-only proxies from the table. Bind ``path`` per-tool.
    for name, path, doc in READ_TOOLS:
        def make(p):
            def tool() -> str:
                return fetch(p)
            return tool
        mcp.tool(name=name, description=doc)(make(path))

    @mcp.tool()
    def get_node_briefing(target: str) -> str:
        """Get an AI-ready Markdown briefing focused on one node or topic.

        Slices the live graph down to ``target`` and its direct neighbours
        (for a node: the topics it pub/subs and the nodes on the other end;
        for a topic: its publisher/subscriber nodes), plus any issues touching
        that neighbourhood. Use this instead of get_runtime_briefing when you
        care about one part of a large Autoware/Nav2 graph. ``target`` may be a
        node id, node name, topic name, or a suffix of any of those."""
        return fetch('/api/v1/snapshot.md?focus=' + quote(target, safe=''))

    @mcp.tool()
    def get_pipeline_path(target: str) -> str:
        """Get the constraining source->sink pipeline path through a node/topic.

        Returns JSON ``{target, pivot, nodes, hops, bottleneck_topic}`` where the
        path follows the lowest-rate link at each branch, so ``bottleneck_topic``
        is the throttling hop. Use this to reason about *where* a pipeline is
        slow, not just which node — e.g. after get_issues flags a bottleneck.
        ``target`` may be a node id/name, topic name, or a suffix."""
        return fetch('/api/v1/path?target=' + quote(target, safe=''))

    @mcp.tool()
    def set_expected_rate(topic: str, min_hz: float) -> str:
        """Set the expected minimum publish rate (Hz) for a topic at runtime.

        The issue engine immediately uses it: if the topic falls below this
        floor a rate-drop / bottleneck issue is raised. Use this to encode what
        "healthy" looks like for a specific Autoware/Nav2 topic."""
        return post('/api/v1/config', {'expected_min_rate': {topic: min_hz}})

    @mcp.tool()
    def health() -> str:
        """Check whether the ros_graph_debugger agent is reachable."""
        try:
            return fetch('/api/v1/health')
        except Exception as exc:
            return json.dumps({'status': 'unreachable', 'base': BASE,
                               'error': str(exc)})

    mcp.run()


if __name__ == '__main__':
    main()
