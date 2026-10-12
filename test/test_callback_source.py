"""Preserve duration provenance across aggregation, analysis and presentation."""

import json

import pytest

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.diff import diff_reports, render_diff_markdown
from ros_graph_debugger.markdown import snapshot_to_markdown
from ros_graph_debugger.model import CallbackStat, RuntimeGraphStore
from ros_graph_debugger.pipeline import pipeline_timeline, trace_pipeline_path
from ros_graph_debugger.recording import make_header, read_recording, write_recording
from ros_graph_debugger.report import build_report, render_html, render_markdown
from ros_graph_debugger.tracing import callbacks_from_trace_file


def snapshot(callbacks, timestamp=100.0):
    return {'timestamp': timestamp,
            'nodes': [{'id': '/a', 'name': 'a', 'publishers': ['/t'], 'subscribers': []},
                      {'id': '/n', 'name': 'n', 'publishers': [], 'subscribers': ['/t']}],
            'topics': [{'name': '/t', 'type': 'std_msgs/msg/String', 'rate_hz': 10.0,
                        'publisher_count': 1, 'subscriber_count': 1,
                        'publishers': ['/a'], 'subscribers': ['/n']}],
            'callbacks': callbacks, 'issues': [], 'tf_edges': [], 'diagnostics': []}


def callback(source=None, p95=210.0):
    return {'node': '/n', 'callback': 'processing_time', 'topic': '/t',
            'p95_ms': p95, 'mean_ms': 100.0, 'source': source}


@pytest.mark.parametrize('source', [None, 'future_source', True, ['trace']])
def test_unknown_import_is_not_inferred_from_filename_or_callback_label(tmp_path, source):
    row = callback(source)
    row['duration_ms'] = row.pop('p95_ms')
    path = tmp_path / 'trace.ndjson'
    path.write_text(json.dumps(row) + '\n')
    [stat] = callbacks_from_trace_file(str(path))
    assert stat.source is None and stat.p95_ms == 210
    del row['source']
    path.write_text(json.dumps(row) + '\n')
    assert callbacks_from_trace_file(str(path))[0].source is None


def test_import_does_not_pool_durations_from_different_sources(tmp_path):
    rows = [dict(node='/n', callback='same', topic='/t', source=source, duration_ms=value)
            for source, value in [('trace', 5), ('application_report', 120),
                                  ('synthetic', 250), (None, 300)]]
    path = tmp_path / 'mixed.ndjson'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    stats = callbacks_from_trace_file(str(path))
    assert {(c.source, c.count, c.p95_ms) for c in stats} == {
        ('trace', 1, 5), ('application_report', 1, 120), ('synthetic', 1, 250),
        (None, 1, 300)}


@pytest.mark.parametrize('source,label', [
    ('trace', 'paired callback trace'),
    ('application_report', 'application-reported processing time'),
    ('synthetic', 'synthetic'), (None, 'unknown'), ('future_source', 'unknown')])
def test_source_changes_evidence_without_changing_duration_budget(source, label):
    store = RuntimeGraphStore()
    store.set_callbacks([CallbackStat('/n', 'processing_time', '/t', 5,
                                      100, 210, 300, source)])
    [issue] = [i for i in analyze(store, Thresholds()) if i.kind == 'slow_callback']
    assert issue.severity == 'critical'
    assert 'p95: 210 ms (limit 100 ms)' in issue.evidence
    assert f'duration source: {label}' in issue.evidence
    assert label in issue.explanation
    assert 'does not establish' in issue.explanation
    assert store.snapshot().to_dict()['callbacks'][0]['source'] == source


def test_report_and_diff_keep_source_series_separate():
    sources = ['trace', 'application_report', 'synthetic', None]
    first = snapshot([callback(source, (i + 1) * 10) for i, source in enumerate(sources)])
    base = build_report(make_header(100.0, 1.0), [first])
    assert len(base['callbacks']) == 4
    assert {c['source']: c['max_p95'] for c in base['callbacks']} == dict(zip(sources, [10, 20, 30, 40]))
    changed = snapshot([callback(source, (i + 1) * 20) for i, source in enumerate(sources)])
    cur = build_report(make_header(100.0, 1.0), [changed])
    regressions = diff_reports(base, cur)['callback_regressions']
    assert len(regressions) == 4 and {c['source'] for c in regressions} == set(sources)
    trace = build_report(make_header(100.0, 1.0), [snapshot([callback('trace', 10)])])
    report = build_report(make_header(100.0, 1.0), [snapshot([callback('application_report', 200)])])
    difference = diff_reports(trace, report)
    assert difference['callback_regressions'] == []
    assert difference['callback_incomparable'] == [{
        'node': '/n', 'callback': 'processing_time', 'topic': '/t',
        'base_sources': ['trace'], 'cur_sources': ['application_report']}]
    assert 'not assessed for regression' in render_diff_markdown(difference)
    for text in (render_markdown(base), render_html(base)):
        for label in ('paired callback trace', 'application-reported processing time', 'synthetic', 'unknown'):
            assert label in text


def test_legacy_recording_remains_unknown_in_briefing_and_report(tmp_path):
    row = callback()
    del row['source']
    original = snapshot([row])
    path = tmp_path / 'legacy.rgd.json'
    write_recording(str(path), make_header(100.0, 1.0), [original])
    header, frames = read_recording(str(path))
    assert frames == [original] and 'source' not in frames[0]['callbacks'][0]
    assert '| /n | processing_time | unknown | 210 ms |' in snapshot_to_markdown(original)
    summary = build_report(header, frames)
    assert summary['callbacks'][0]['source'] is None
    assert '| /n | processing_time | unknown | 210 ms |' in render_markdown(summary)


def test_pipeline_duration_and_source_follow_same_selected_observation():
    app = callback('application_report', 120)
    app['topic'] = '/n/debug/processing_time_ms'
    first = snapshot([app])
    assert trace_pipeline_path(first, '/n')['hops'][0]['cb_source'] == 'application_report'
    traced = dict(callback('trace', 7), callback='sub /t')
    second = snapshot([app, traced], timestamp=101)
    hop = trace_pipeline_path(second, '/n')['hops'][0]
    assert (hop['cb_p95_ms'], hop['cb_source']) == (7, 'trace')
    timeline = pipeline_timeline([first, second], '/n')['hops'][0]
    assert timeline['cb_p95_ms'] == [120, 7]
    assert timeline['cb_source'] == ['application_report', 'trace']
    summary = build_report(make_header(100.0, 1.0), [first, second], path_focus='/n')
    assert 'application-reported processing time, paired callback trace' in render_markdown(summary)


def test_tied_pipeline_observations_do_not_invent_a_source():
    trace = dict(callback('trace', 7), callback='sub /t')
    unknown = dict(trace)
    del unknown['source']
    for rows in ([trace, unknown], [unknown, trace]):
        hop = trace_pipeline_path(snapshot(rows), '/n')['hops'][0]
        assert (hop['cb_p95_ms'], hop['cb_source']) == (7, None)
