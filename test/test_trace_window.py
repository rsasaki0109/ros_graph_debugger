"""Imported execution bounds are historical, clock-scoped and never inferred."""

from dataclasses import asdict
import json

import pytest

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.markdown import snapshot_to_markdown
from ros_graph_debugger.model import RuntimeGraphStore, TRACE_WINDOW_NOTE, callback_trace_window
from ros_graph_debugger.recording import make_header, read_recording, write_recording
from ros_graph_debugger.report import build_report, render_html, render_markdown
from ros_graph_debugger.tracing import aggregate_callback_durations, pair_callback_events

CLOCK = 'ctf_ns_from_origin:4f3ccacf-8e09-45a6-8083-1761c14b6027'
OTHER = 'ctf_ns_from_origin:17928b61-b18e-4975-83f9-dc0c2648ad5f'
# Beyond float's exact integer range: retain nanoseconds without reconstructing
# the end from millisecond durations rounded to three decimals.
START = 1_790_000_000_000_000_001


def row(**changes):
    return dict(dict(node='/n', callback='sub /t', topic='/t', source='trace',
                     duration_ms=210.000, start_ns=START, end_ns=START + 210_000_123,
                     clock=CLOCK), **changes)


def test_exact_end_bounds_survive_rounding_nested_invocations_and_json():
    marks = [dict(kind=kind, handle=1, t_ns=stamp, clock=CLOCK)
             for kind, stamp in [('start', START), ('start', START + 1),
                                 ('end', START + 200_000_007),
                                 ('end', START + 210_000_123)]]
    paired = pair_callback_events(marks)
    assert paired[-1]['t_ns'] == START and paired[-1]['end_ns'] == START + 210_000_123
    assert paired[-1]['duration_ms'] == 210.0
    rows = [row(start_ns=r['t_ns'], end_ns=r['end_ns'], duration_ms=r['duration_ms'])
            for r in paired]
    [stat] = aggregate_callback_durations(json.loads(json.dumps(rows)))
    assert callback_trace_window(stat) == dict(start_ns=START, end_ns=START + 210_000_123,
                                               clock=CLOCK)
    assert stat.count == 2 and stat.p95_ms == 210.0


def test_start_and_end_on_different_clocks_are_not_paired():
    assert pair_callback_events([dict(kind='start', handle=1, t_ns=0, clock=CLOCK),
                                 dict(kind='end', handle=1, t_ns=1, clock=OTHER)]) == []


@pytest.mark.parametrize('change', [
    {'start_ns': None}, {'end_ns': None}, {'clock': None}, {'clock': 'ROS time'},
    {'clock': 'ctf_ns_from_origin:not-a-uuid'}, {'clock': OTHER},
    {'start_ns': True}, {'end_ns': 1.5}, {'start_ns': float(START)},
    {'end_ns': START - 1},
])
def test_incomplete_invalid_or_mixed_metadata_does_not_describe_partial_window(change):
    [stat] = aggregate_callback_durations([row(), row(**change)])
    assert stat.count == 2 and stat.p95_ms == 210.0  # Same duration contract.
    assert stat.trace_window_start_ns is stat.trace_window_end_ns is stat.trace_window_clock is None


@pytest.mark.parametrize('source', [None, 'application_report', 'synthetic'])
def test_bounds_do_not_turn_other_sources_into_traces(source):
    [stat] = aggregate_callback_durations([row(source=source)])
    assert stat.source == source and callback_trace_window(stat) is None


def test_zero_is_valid_and_not_missing():
    [stat] = aggregate_callback_durations([row(start_ns=0, end_ns=0, duration_ms=0)])
    assert callback_trace_window(stat) == dict(start_ns=0, end_ns=0, clock=CLOCK)


def test_recording_replay_does_not_rebase_imported_bounds(tmp_path):
    [stat] = aggregate_callback_durations([row()])
    store = RuntimeGraphStore()
    store.set_callbacks([stat])
    snapshots = [store.snapshot().to_dict() for _ in range(2)]
    assert snapshots[0]['callbacks'] == snapshots[1]['callbacks'] == [asdict(stat)]
    path = tmp_path / 'recording.ndjson'
    header = make_header(100, 1)
    write_recording(str(path), header, snapshots)
    _, frames = read_recording(str(path))
    assert frames == snapshots
    assert all('/n' not in {n['id'] for n in s['nodes']} for s in frames)
    [issue] = [i for i in analyze(store, Thresholds()) if i.kind == 'slow_callback']
    assert issue.severity == 'critical'
    assert 'p95: 210 ms (limit 100 ms)' in issue.evidence
    assert any(str(START) in e and CLOCK in e for e in issue.evidence)
    assert 'Imported trace duration does not establish current node activity.' in issue.evidence


def test_presentations_deduplicate_same_capture_and_keep_unknown_windows(tmp_path):
    store = RuntimeGraphStore()
    store.set_callbacks(aggregate_callback_durations([row()]))
    original = store.snapshot().to_dict()
    legacy = json.loads(json.dumps(original))
    for key in ('trace_window_start_ns', 'trace_window_end_ns', 'trace_window_clock'):
        del legacy['callbacks'][0][key]
    path = tmp_path / 'legacy.ndjson'
    write_recording(str(path), make_header(100, 1), [legacy])
    _, frames = read_recording(str(path))
    assert frames == [legacy]  # No retroactive field additions.
    summary = build_report(make_header(100, 1), [original, original, legacy])
    assert summary['callbacks'][0]['trace_windows'] == [dict(
        start_ns=START, end_ns=START + 210_000_123, clock=CLOCK), None]
    for text in (snapshot_to_markdown(original), render_markdown(summary), render_html(summary)):
        assert TRACE_WINDOW_NOTE in text and str(START) in text and CLOCK in text
    for text in (snapshot_to_markdown(legacy), render_markdown(summary), render_html(summary)):
        assert 'unknown' in text


def test_html_escapes_callback_labels_in_window_section():
    store = RuntimeGraphStore()
    store.set_callbacks(aggregate_callback_durations([row(callback='<script>bad</script>')]))
    html = render_html(build_report(make_header(100, 1), [store.snapshot().to_dict()]))
    assert '<script>bad</script>' not in html and '&lt;script&gt;bad&lt;/script&gt;' in html
