"""Documentation drift guard: the API the code serves must match docs/api.md.

If you add or remove a route, this test fails until EXPECTED and docs/api.md are
both updated — so the documented surface never silently drifts from the code.
"""

import os

from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.model import RuntimeGraphStore
from ros_graph_debugger.server import create_app

# The canonical HTTP surface. Keep in lockstep with docs/api.md.
EXPECTED = {
    ('GET', '/api/v1/health'),
    ('GET', '/api/v1/snapshot'),
    ('GET', '/api/v1/snapshot.md'),
    ('GET', '/api/v1/graph'),
    ('GET', '/api/v1/nodes'),
    ('GET', '/api/v1/topics'),
    ('GET', '/api/v1/tf'),
    ('GET', '/api/v1/diagnostics'),
    ('GET', '/api/v1/issues'),
    ('GET', '/api/v1/path'),
    ('GET', '/api/v1/profile'),
    ('GET', '/api/v1/config'),
    ('POST', '/api/v1/config'),
    ('GET', '/api/v1/replay'),
    ('POST', '/api/v1/replay/seek'),
    ('POST', '/api/v1/replay/play'),
    ('WS', '/api/v1/stream'),
}

_HTTP_METHODS = {'GET', 'POST', 'PUT', 'DELETE', 'PATCH'}
_DOCS = os.path.join(os.path.dirname(__file__), '..', 'docs', 'api.md')


def _actual_routes():
    app = create_app(RuntimeGraphStore(), '/nonexistent',
                     profile_data={'name': 'x', 'groups': {}},
                     thresholds=Thresholds())
    out = set()
    for r in app.routes:
        path = getattr(r, 'path', None)
        if not path or not path.startswith('/api'):
            continue
        methods = getattr(r, 'methods', None)
        if methods:
            for m in methods:
                if m in _HTTP_METHODS:
                    out.add((m, path))
        else:  # WebSocket route
            out.add(('WS', path))
    return out


def test_code_matches_expected_surface():
    actual = _actual_routes()
    assert actual == EXPECTED, (
        f'API surface drift.\n  added:   {actual - EXPECTED}\n'
        f'  missing: {EXPECTED - actual}')


def test_docs_mention_every_endpoint():
    with open(_DOCS) as f:
        doc = f.read()
    missing = sorted({path for _, path in EXPECTED if path not in doc})
    assert not missing, f'docs/api.md does not mention: {missing}'
