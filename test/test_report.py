"""Recording round-trip + report builder/renderers — no ROS, fully synthetic."""

import os
import tempfile

from ros_graph_debugger.recording import (
    make_header,
    read_recording,
    write_recording,
)
from ros_graph_debugger.report import build_report, render_html, render_markdown

PROFILE = {'name': 'autoware', 'groups': {
    'perception': {'topic_patterns': ['^/perception/.*']},
    'planning': {'topic_patterns': ['^/planning/.*']},
}}


def _snapshot(ts, objects_rate, bottleneck=False):
    topics = [
        {'name': '/perception/object_recognition/objects',
         'type': 'std_msgs/msg/String', 'publisher_count': 1, 'subscriber_count': 1,
         'rate_hz': objects_rate, 'bandwidth_bps': 80_000_000,
         'p95_msg_size_bytes': 8_000_000,
         'status': 'warning' if bottleneck else 'ok'},
        {'name': '/planning/scenario_planning/trajectory', 'type': 's', 'rate_hz': 10.0,
         'bandwidth_bps': 1000, 'status': 'ok', 'publisher_count': 1, 'subscriber_count': 1},
    ]
    issues = []
    if bottleneck:
        issues.append({
            'severity': 'critical', 'kind': 'bottleneck',
            'title': 'Likely bottleneck: detector',
            'evidence': ['/perception/object_recognition/objects: 4.0 Hz (expected >= 10.0)'],
            'suggested_actions': ['Inspect callback'],
            'related_nodes': ['/detector'],
            'related_topics': ['/perception/object_recognition/objects']})
    tf = [{'parent': 'map', 'child': 'base_link', 'status': 'critical', 'age_ms': 450.0}] \
        if bottleneck else []
    return {'timestamp': ts, 'profile': 'autoware', 'nodes': [], 'topics': topics,
            'edges': [], 'tf_edges': tf, 'diagnostics': [], 'issues': issues}


def test_recording_roundtrip_tolerates_garbage(tmp_path=None):
    path = os.path.join(tempfile.mkdtemp(), 'r.rgd.json')
    header = make_header(started=100.0, interval=1.0, profile=PROFILE)
    snaps = [_snapshot(100.0, 10.0), _snapshot(101.0, 4.0, bottleneck=True)]
    write_recording(path, header, snaps)
    # Append a truncated/garbage line as a Ctrl-C would.
    with open(path, 'a') as f:
        f.write('{not valid json\n')

    h, loaded = read_recording(path)
    assert h['profile']['name'] == 'autoware'
    assert len(loaded) == 2  # garbage line skipped


def test_build_report_aggregates():
    header = make_header(100.0, 1.0, PROFILE)
    snaps = [_snapshot(100.0 + i, 10.0) for i in range(8)]
    snaps += [_snapshot(108.0 + i, 4.0, bottleneck=True) for i in range(2)]  # 2 bad samples
    summary = build_report(header, snaps)

    assert summary['meta']['samples'] == 10
    assert summary['meta']['profile'] == 'autoware'
    assert summary['meta']['duration_s'] == 9.0

    # bottleneck captured and ranked critical
    assert summary['bottlenecks']
    b = summary['bottlenecks'][0]
    assert b['kind'] == 'bottleneck'
    assert b['max_severity'] == 'critical'
    assert b['pct'] == 20.0  # 2 / 10

    # rate stats for the objects topic
    obj = [t for t in summary['topics']
           if t['name'].endswith('/objects')][0]
    assert obj['rate_min'] == 4.0
    assert obj['rate_max'] == 10.0

    # tf stale captured
    assert summary['tf_stale'][0]['child'] == 'base_link'
    assert summary['tf_stale'][0]['max_age_ms'] == 450.0

    # bandwidth ranking
    assert summary['bandwidth_top'][0]['name'].endswith('/objects')

    # readiness present with stages
    stages = {r['stage'] for r in summary['readiness']}
    assert {'perception', 'planning'} <= stages
    perc = [r for r in summary['readiness'] if r['stage'] == 'perception'][0]
    assert perc['error_pct'] == 20.0  # 2/10 samples had a critical issue


def test_render_html_and_markdown():
    header = make_header(100.0, 1.0, PROFILE)
    snaps = [_snapshot(100.0, 10.0), _snapshot(101.0, 4.0, bottleneck=True)]
    summary = build_report(header, snaps)

    html = render_html(summary)
    assert '<!DOCTYPE html>' in html
    assert 'Likely bottleneck: detector' in html
    assert '<svg' in html  # issue timeline
    assert 'Engage readiness' not in html  # html uses a different header
    assert 'readiness' in html  # the css class / section

    md = render_markdown(summary)
    assert '# ROS Graph Debugger — Recording Report' in md
    assert 'Engage readiness' in md
    assert 'Likely bottleneck: detector' in md
    assert 'map → base_link' in md


def test_empty_profile_no_readiness():
    header = make_header(100.0, 1.0, None)
    snaps = [{'timestamp': 1.0, 'nodes': [], 'topics': [], 'edges': [],
              'tf_edges': [], 'diagnostics': [], 'issues': []}]
    summary = build_report(header, snaps)
    assert summary['readiness'] is None
    # still renders
    assert '<!DOCTYPE html>' in render_html(summary)
