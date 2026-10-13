"""Structured grading controls, constructed inputs, no model or live ROS."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'benchmarks'))
from investigation_cases import TRACE_WINDOW_FIELDS, TRACE_WINDOW_LIMITS, negative_controls, prepare, rubric_for, score
from ros_graph_debugger.recording import make_header, read_recording, write_recording

CLOCK = 'ctf_ns_from_origin:4f3ccacf-8e09-45a6-8083-1761c14b6027'
START = 1_790_000_000_000_000_001


def observation(**changes):
    return dict(dict(node='/old_worker', callback='sub /input', topic='/input',
                     count=10, mean_ms=5, p95_ms=7, max_ms=9, source='trace',
                     trace_window_start_ns=START, trace_window_end_ns=START + 123_456_789,
                     trace_window_clock=CLOCK), **changes)


def task(callbacks=None):
    evidence = {'snapshot': {'timestamp': 2000, 'nodes': [], 'topics': [], 'issues': [],
                            'callbacks': callbacks if callbacks is not None else [observation()]},
                'recording_metadata': {'mode': 'replay', 'recording_header': {
                    'header_age_clock': {'selected': 'ros'}}}}
    return evidence, rubric_for('case-001', evidence, 'duration', '/old_worker')


def windows(rubric, prefix='duration_0'):
    return {key: next(c for c in rubric['claims'] if c['id'] == prefix + '_' + key)
            for key in TRACE_WINDOW_FIELDS}


def test_exact_boundaries_and_clock_cite_same_original_callback_without_graph_node():
    evidence, rubric = task([observation(node='/other'), observation()])
    claims = windows(rubric, 'duration_1')
    for key, claim in claims.items():
        assert claim['status'] == 'observed'
        assert claim['value'] == evidence['snapshot']['callbacks'][1][key]
        assert claim['evidence'] == ['/snapshot/callbacks/1/' + key]
    assert score(evidence, rubric, rubric)['passed']
    assert all(c['status'] == 'not_established' for c in rubric['limitations'])
    assert set(TRACE_WINDOW_LIMITS) <= {c['id'] for c in rubric['limitations']}


@pytest.mark.parametrize('changes', [
    {'trace_window_start_ns': None}, {'trace_window_end_ns': None},
    {'trace_window_clock': None}, {'trace_window_clock': 'ros'},
    {'trace_window_clock': 'ctf_ns_from_origin:invalid'},
    {'trace_window_start_ns': True}, {'trace_window_end_ns': float(START)},
    {'trace_window_end_ns': START - 1}, {'source': None},
    {'source': 'application_report'}, {'source': 'synthetic'},
])
def test_incomplete_invalid_or_other_source_tuple_is_entirely_unknown(changes):
    evidence, rubric = task([observation(**changes)])
    for claim in windows(rubric).values():
        assert (claim['status'], claim['value'], claim['evidence']) == (
            'unknown', None, ['/snapshot/callbacks/0'])
    assert score(evidence, rubric, rubric)['passed']


def test_legacy_boundaries_are_not_reconstructed_from_count_duration_or_timestamp():
    legacy = observation()
    for key in TRACE_WINDOW_FIELDS:
        del legacy[key]
    evidence, rubric = task([legacy])
    assert all(c['status'] == 'unknown' for c in windows(rubric).values())
    assert all(key not in evidence['snapshot']['callbacks'][0] for key in TRACE_WINDOW_FIELDS)
    for bad in negative_controls(rubric).values():
        assert not score(evidence, rubric, bad)['passed']


@pytest.mark.parametrize('value', [0, START])
def test_zero_and_large_nanoseconds_require_exact_integer_answers(value):
    evidence, rubric = task([observation(trace_window_start_ns=value, trace_window_end_ns=value)])
    assert windows(rubric)['trace_window_start_ns']['value'] == value
    assert score(evidence, rubric, rubric)['passed']
    for answer_value in (float(value), True, str(value)):
        answer = copy.deepcopy(rubric)
        windows(answer)['trace_window_start_ns']['value'] = answer_value
        assert not score(evidence, rubric, answer)['passed']


def test_equal_boundaries_from_different_rows_cannot_replace_citations():
    evidence, rubric = task([observation(), observation(callback='timer')])
    answer = copy.deepcopy(rubric)
    windows(answer)['trace_window_start_ns']['evidence'] = ['/snapshot/callbacks/1/trace_window_start_ns']
    assert not score(evidence, rubric, answer)['passed']
    assert score(evidence, rubric, rubric)['passed']


@pytest.mark.parametrize('limit', list(TRACE_WINDOW_LIMITS))
def test_declared_window_and_ros_header_clock_cannot_establish_conclusions(limit):
    evidence, rubric = task()
    controls = negative_controls(rubric)
    assert controls[limit]['limitations'] != rubric['limitations']
    assert not score(evidence, rubric, controls[limit])['passed']


def test_known_and_legacy_negative_controls_all_fail():
    for callbacks, expected in (([observation()], 19), ([dict(node='/old_worker', source='trace')], 18)):
        evidence, rubric = task(callbacks)
        controls = negative_controls(rubric)
        assert len(controls) == expected
        assert all(not score(evidence, rubric, bad)['passed'] for bad in controls.values())


def test_unknown_first_callback_does_not_hide_later_integer_precision_control():
    evidence, rubric = task([observation(trace_window_clock=None), observation(callback='timer')])
    controls = negative_controls(rubric)
    assert len(controls) == 19 and 'trace_boundary_float' in controls
    assert type(windows(controls['trace_boundary_float'], 'duration_1')['trace_window_start_ns']['value']) is float
    assert not score(evidence, rubric, controls['trace_boundary_float'])['passed']


@pytest.mark.parametrize('present', [True, False])
def test_export_preserves_frame_header_hash_and_selects_existing_graph_or_full_briefing(tmp_path, present):
    evidence, _ = task()
    if present:
        evidence['snapshot']['nodes'] = [{'id': '/old_worker'}]
    source = tmp_path / 'source.ndjson'
    header = make_header(2000, 1)
    header['custom_clock_note'] = 'Unconfirmed CTF/ROS relationship'
    write_recording(str(source), header, [evidence['snapshot']])
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / 'bundle'
    args = SimpleNamespace(output=output, duration_recording=source, duration_target='/old_worker',
                           integration=None, nav_recordings=None, trace_recordings=None,
                           public_live=None, public_legacy=None, public_bag=None, synthetic_demo=False)
    prepare(args)
    [case] = json.loads((output / 'manifest.json').read_text())
    assert case.get('briefing_tool', 'get_node_briefing') == (
        'get_node_briefing' if present else 'get_runtime_briefing')
    participant = output / 'participant/case-001'
    exported_header, exported = read_recording(str(participant / 'input.rgd.json'))
    assert exported_header == header and exported == [evidence['snapshot']]
    provenance = json.loads((output / 'evaluator/case-001/provenance.json').read_text())
    assert provenance['source_sha256'] == source_hash and provenance['snapshot_index'] == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    prompt = (participant / 'prompt.md').read_text()
    assert 'historical invocations' in prompt and 'exact JSON integers' in prompt
