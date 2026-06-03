"""Merge snapshots from several agents into one fleet-wide view.

A single DDS graph is already covered by one agent. Federation is for hosts that
*can't* see each other — a fleet of robots, or nodes split across DDS domains:
each runs its own ros_graph_debugger, and we stitch their snapshots into one
namespaced graph so an AI (or the web UI) can reason about the whole fleet.

Everything here is a pure function over snapshot dicts — the live fetching is a
thin urllib loop in the CLI. Names are prefixed by host (``/robot1/scan``), so
identical node/topic names on different robots stay distinct.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from .health import summarize_health


def _host_tag(host: str) -> str:
    return (host or 'host').strip('/').replace('/', '_') or 'host'


def _pfx_abs(host: str, name: str) -> str:
    """Prefix an absolute ROS name (``/scan`` -> ``/robot1/scan``)."""
    h = _host_tag(host)
    if not name:
        return '/' + h
    return f'/{h}/{name.lstrip("/")}'


def _pfx_frame(host: str, frame: str) -> str:
    """Prefix a TF frame (frames are relative, e.g. ``base_link``)."""
    return f'{_host_tag(host)}/{frame.lstrip("/")}' if frame else _host_tag(host)


def _merge_one(host: str, snap: dict) -> dict:
    """Return a host-prefixed copy of one snapshot's collections."""
    def node(n):
        m = dict(n)
        m['id'] = _pfx_abs(host, n['id'])
        m['publishers'] = [_pfx_abs(host, t) for t in n.get('publishers', [])]
        m['subscribers'] = [_pfx_abs(host, t) for t in n.get('subscribers', [])]
        m['host'] = host
        return m

    def topic(t):
        m = dict(t)
        m['name'] = _pfx_abs(host, t['name'])
        m['publishers'] = [_pfx_abs(host, n) for n in t.get('publishers', [])]
        m['subscribers'] = [_pfx_abs(host, n) for n in t.get('subscribers', [])]
        m['host'] = host
        return m

    def edge(e):
        m = dict(e)
        m['from_node'] = _pfx_abs(host, e.get('from_node', ''))
        m['to_node'] = _pfx_abs(host, e.get('to_node', ''))
        m['topic'] = _pfx_abs(host, e.get('topic', ''))
        m['host'] = host
        return m

    def tf(e):
        m = dict(e)
        m['parent'] = _pfx_frame(host, e.get('parent', ''))
        m['child'] = _pfx_frame(host, e.get('child', ''))
        m['host'] = host
        return m

    def diag(d):
        m = dict(d)
        m['name'] = f'{_host_tag(host)}/{d.get("name", "")}'
        m['host'] = host
        return m

    def cb(c):
        m = dict(c)
        m['node'] = _pfx_abs(host, c.get('node', ''))
        if c.get('topic'):
            m['topic'] = _pfx_abs(host, c['topic'])
        m['host'] = host
        return m

    def issue(i):
        m = dict(i)
        m['id'] = f'{_host_tag(host)}:{i.get("id", "")}'
        m['title'] = f'[{host}] {i.get("title", "")}'
        m['related_nodes'] = [_pfx_abs(host, n) for n in i.get('related_nodes', [])]
        m['related_topics'] = [_pfx_abs(host, t) for t in i.get('related_topics', [])]
        m['related_frames'] = [_pfx_frame(host, f) for f in i.get('related_frames', [])]
        m['host'] = host
        return m

    return {
        'nodes': [node(n) for n in snap.get('nodes', [])],
        'topics': [topic(t) for t in snap.get('topics', [])],
        'edges': [edge(e) for e in snap.get('edges', [])],
        'tf_edges': [tf(e) for e in snap.get('tf_edges', [])],
        'diagnostics': [diag(d) for d in snap.get('diagnostics', [])],
        'callbacks': [cb(c) for c in snap.get('callbacks', [])],
        'issues': [issue(i) for i in snap.get('issues', [])],
    }


def merge_snapshots(host_snapshots) -> dict:
    """Merge ``{host: snapshot_dict}`` (or a list of ``(host, snapshot)``) into a
    single combined snapshot, namespaced by host.

    The result is a normal snapshot dict (so the web UI, Markdown briefing, and
    health rollup work on it unchanged) plus ``hosts`` — a per-host health
    verdict — and a fleet ``profile`` of ``'federated'``."""
    items = (host_snapshots.items() if isinstance(host_snapshots, dict)
             else list(host_snapshots))

    combined = {'timestamp': None, 'profile': 'federated', 'nodes': [],
                'topics': [], 'edges': [], 'tf_edges': [], 'diagnostics': [],
                'callbacks': [], 'issues': [], 'hosts': []}

    for host, snap in items:
        part = _merge_one(host, snap or {})
        for key in ('nodes', 'topics', 'edges', 'tf_edges', 'diagnostics',
                    'callbacks', 'issues'):
            combined[key].extend(part[key])
        h = summarize_health(snap or {})
        combined['hosts'].append({'host': host, 'verdict': h['verdict'],
                                  'issue_count': h['issue_count'],
                                  'headline': h['headline']})

    # Sort issues fleet-wide by severity so the worst surfaces first.
    order = {'critical': 0, 'warning': 1, 'info': 2}
    combined['issues'].sort(key=lambda i: order.get(i.get('severity'), 9))
    return combined


def http_fetch_snapshot(base: str) -> dict:
    """GET ``/api/v1/snapshot`` from one agent as a dict."""
    with urllib.request.urlopen(base.rstrip('/') + '/api/v1/snapshot',
                                timeout=5) as r:
        return json.loads(r.read())


class _Frame:
    """Minimal wrapper so create_app can call ``.to_dict()`` uniformly."""

    __slots__ = ('_d',)

    def __init__(self, d: dict):
        self._d = d

    def to_dict(self) -> dict:
        return self._d


class FederatedStore:
    """A drop-in store (like RuntimeGraphStore/ReplayStore) that serves the
    merged snapshot of several agents, refreshed by a background poller.

    ``fetch`` is injectable so the store is testable without a network."""

    def __init__(self, agents, fetch=None, poll_interval: float = 2.0):
        self._agents = list(agents)  # [(host, base_url)]
        self._fetch = fetch or http_fetch_snapshot
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._merged = merge_snapshots([])
        self.refresh()

    def refresh(self) -> None:
        snaps = []
        for host, base in self._agents:
            try:
                snaps.append((host, self._fetch(base)))
            except Exception:
                continue  # unreachable agent is skipped this cycle
        merged = merge_snapshots(snaps)
        with self._lock:
            self._merged = merged

    def snapshot(self) -> _Frame:
        with self._lock:
            return _Frame(self._merged)

    def start_polling(self) -> None:
        def _loop():
            while True:
                time.sleep(self.poll_interval)
                try:
                    self.refresh()
                except Exception:
                    pass
        threading.Thread(target=_loop, daemon=True).start()
