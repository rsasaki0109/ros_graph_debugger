"""MCP server exposing a running ros_graph_debugger agent to AI assistants.

This is a thin stdio MCP server that proxies the agent's REST API, so an AI
(Claude, etc.) can pull the live ROS graph, issues, and metrics as tools.

Install the SDK first:   pip install "mcp[cli]"
Run:                     python -m ros_graph_debugger.mcp_server
Register with Claude Code:
    claude mcp add ros-graph -- python -m ros_graph_debugger.mcp_server

Set RGD_BASE to point at a non-default agent URL (default http://127.0.0.1:3939).
"""

from __future__ import annotations

import json
import os
import urllib.request

BASE = os.environ.get('RGD_BASE', 'http://127.0.0.1:3939')


def _get(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return r.read().decode()


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        raise SystemExit(
            'The MCP SDK is not installed. Run:  pip install "mcp[cli]"')

    mcp = FastMCP('ros-graph-debugger')

    @mcp.tool()
    def get_runtime_briefing() -> str:
        """Get an AI-ready Markdown briefing of the live ROS 2 system:
        issues to look at next, plus topic and node metrics."""
        return _get('/api/v1/snapshot.md')

    @mcp.tool()
    def get_issues() -> str:
        """Get the current detected issues (bottlenecks, stale topics, QoS
        mismatches, ...) as JSON, with evidence and suggested actions."""
        return _get('/api/v1/issues')

    @mcp.tool()
    def get_graph() -> str:
        """Get the live ROS graph (nodes, topics, edges) as JSON."""
        return _get('/api/v1/graph')

    @mcp.tool()
    def get_topics() -> str:
        """Get all topics with rate/bandwidth/QoS metrics as JSON."""
        return _get('/api/v1/topics')

    @mcp.tool()
    def health() -> str:
        """Check whether the ros_graph_debugger agent is reachable."""
        try:
            return _get('/api/v1/health')
        except Exception as exc:
            return json.dumps({'status': 'unreachable', 'base': BASE,
                               'error': str(exc)})

    mcp.run()


if __name__ == '__main__':
    main()
