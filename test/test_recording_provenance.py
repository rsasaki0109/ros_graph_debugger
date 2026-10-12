"""Provenance must survive replay and stay unknown when absent."""
from contextlib import contextmanager
import json
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn

from ros_graph_debugger.cli import main
from ros_graph_debugger.mcp_server import READ_TOOLS, fetch
from ros_graph_debugger.model import RuntimeGraphStore
from ros_graph_debugger.recording import make_header, write_recording
from ros_graph_debugger.replay import ReplayStore
from ros_graph_debugger.report import build_report, render_html, render_markdown
from ros_graph_debugger.server import create_app

FRAME = {'timestamp': 42.0, 'nodes': [], 'topics': [], 'edges': [],
         'tf_edges': [], 'callbacks': [], 'diagnostics': [], 'issues': []}


def header(selected='ros'):
    h = make_header(40, 1)
    h['header_age_clock'] = {'requested': 'auto', 'selected': selected,
                            'clock_messages': 9, 'unreadable_clock_messages': 1}
    h['future_extension'] = {'retain': [1, 2]}
    return h


@contextmanager
def serving(app):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        base = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        worker = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while True:
                assert worker.is_alive()
                try:
                    with urllib.request.urlopen(base + '/api/v1/health', timeout=0.5) as response:
                        assert json.load(response)['status'] == 'ok'
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            yield base
        finally:
            server.should_exit = True
            worker.join(timeout=5)
            assert not worker.is_alive()


@pytest.mark.parametrize('h', [header(), header('bag'), make_header(40, 1), None])
def test_replay_metadata_round_trips_and_snapshot_is_unchanged(h):
    replay = ReplayStore([FRAME])
    app = create_app(replay, '/nonexistent', replay=replay, recording_header=h)
    path = next(p for name, p, _ in READ_TOOLS if name == 'get_recording_metadata')
    with serving(app) as base:
        before = json.loads(fetch('/api/v1/snapshot', base))
        assert before == FRAME
        assert json.loads(fetch(path, base)) == {'mode': 'replay', 'recording_header': h}
        req = urllib.request.Request(base + '/api/v1/replay/seek?index=0', data=b'')
        with urllib.request.urlopen(req, timeout=2) as response:
            assert not json.load(response)['playing']
        assert json.loads(fetch(path, base)) == {'mode': 'replay', 'recording_header': h}
        assert json.loads(fetch('/api/v1/snapshot', base)) == before
        md = fetch('/api/v1/snapshot.md?focus=missing', base)
        assert '## Header age clock' in md
        if h and h.get('header_age_clock'):
            assert ('recorded ROS clock (/clock)' if h['header_age_clock']['selected'] == 'ros'
                    else 'bag receive time') in md
        else:
            assert 'Unknown: this recording does not identify the header-age clock.' in md


def test_live_metadata_does_not_claim_a_recording_or_infer_clock():
    app = create_app(RuntimeGraphStore(), '/nonexistent', recording_header=header())
    with serving(app) as base:
        assert json.loads(fetch('/api/v1/recording', base)) == {'mode': 'live', 'recording_header': None}
        assert '## Header age clock' not in fetch('/api/v1/snapshot.md', base)


@pytest.mark.parametrize('selected', ['ros', 'bag', None])
def test_cli_and_html_reports_explain_basis_without_changing_issues(tmp_path, capsys, selected):
    h = header(selected) if selected else make_header(40, 1)
    file = tmp_path / 'run.rgd.json'
    write_recording(str(file), h, [FRAME])
    assert main(['report', str(file)]) == 0
    md = capsys.readouterr().out
    summary = build_report(h, [FRAME])
    html = render_html(summary)
    for text in (md, html):
        assert 'Header age clock' in text
        if selected:
            assert ('recorded ROS clock (/clock)' if selected == 'ros' else 'bag receive time') in text
            assert 'requested mode: auto' in text
        else:
            assert 'Unknown: this recording does not identify the header-age clock.' in text
    assert summary['issue_catalog'] == [] and FRAME['issues'] == []
    if selected == 'ros':
        assert 'Clock messages used: 9; unreadable: 1.' in md


def test_malformed_provenance_is_unknown_without_interpreting_header_markup():
    h = header()
    h['header_age_clock'] = {'selected': '<script>alert(1)</script>', 'requested': 'auto'}
    summary = build_report(h, [FRAME])
    assert summary['meta']['header_age_clock'] == h['header_age_clock']
    assert 'Unknown:' in render_markdown(summary)
    assert '<script>alert(1)</script>' not in render_html(summary)


def test_missing_clock_counts_remain_unknown():
    h = header()
    h['header_age_clock'] = {'selected': 'ros'}
    md = render_markdown(build_report(h, [FRAME]))
    assert 'requested mode: unknown' in md and 'Clock messages used: unknown; unreadable: unknown.' in md


@pytest.mark.parametrize('h', [header(), None])
def test_serve_cli_passes_original_or_headerless_provenance(tmp_path, monkeypatch, h):
    file = tmp_path / 'run.rgd.json'
    if h is None:
        file.write_text(json.dumps(FRAME) + '\n')
    else:
        write_recording(str(file), h, [FRAME])
    captured = []
    def run(app, **kwargs):
        endpoint = next(r.endpoint for r in app.routes if r.path == '/api/v1/recording')
        captured.append(endpoint())
    monkeypatch.setattr(uvicorn, 'run', run)
    assert main(['serve', str(file), '--no-browser']) == 0
    expected = h if h is not None else make_header(0, 0)
    assert captured == [{'mode': 'replay', 'recording_header': expected}]
