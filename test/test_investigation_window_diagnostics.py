"""Constructed rubric controls, distinct from the public actual-run cases."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'benchmarks'))
from investigation_cases import negative_controls, prepare, rubric_for, score
from ros_graph_debugger.recording import make_header, read_recording, write_recording


def evidence():
    return {'snapshot': {'timestamp': 1000, 'nodes': [{'id': '/worker'}], 'topics': [
        {'name': '/output', 'rate_hz': 30, 'rate_measurement_scope': 'probe_receipt',
         'probe_rate_window_samples': 50, 'probe_rate_window_duration_ms': 1633.333,
         'probe_rate_window_clock': 'rmw_received_timestamp', 'probe_last_sample_age_ms': 10000,
         'probe_source_receipt_gap_p95_ms': -.5, 'probe_callback_delay_p95_ms': 300,
         'header_age_p95_ms': 0}],
        'diagnostics': [{'name': 'unrelated', 'level': 0},
                        {'name': '/worker', 'level': 2, 'message': 'input timeout', 'hardware_id': '/worker'}],
        'issues': [{'kind': 'topic_stale', 'related_topics': ['/output']},
                   {'kind': 'diagnostics_error', 'related_diagnostics': ['/worker'],
                    'related_nodes': [], 'evidence': ['level: 2', 'node attribution: unknown']}]},
        'recording_metadata': {'mode': 'replay', 'recording_header': {}}}


def claims(rubric):
    return {c['id']: c for c in rubric['claims']}


def test_old_rate_and_recent_processing_are_separate_observations():
    data = evidence()
    rubric = rubric_for('case-001', data, 'topic_window', '/output')
    observed = claims(rubric)
    assert observed['rate_hz']['value'] == 30
    assert observed['probe_last_sample_age_ms']['value'] == 10000
    assert observed['probe_rate_window_clock']['value'] == 'rmw_received_timestamp'
    limits = {c['id']: c['next_check'] for c in rubric['limitations']}
    assert limits['current_delivery_from_retained_rate_window'] == 'compare_independent_receipts_over_time'
    assert limits['dds_receipt_age_from_processed_sample_age'] == 'inspect_raw_receipt_and_processing_timestamps'
    assert score(data, rubric, rubric)['passed']
    controls = negative_controls(rubric)
    assert {'processing_age_as_dds_gap', 'window_span_as_header_age',
            'retained_rate_proves_current_delivery', 'processing_age_proves_publisher_activity'} <= controls.keys()
    assert all(not score(data, rubric, answer)['passed'] for answer in controls.values())


@pytest.mark.parametrize('key', ['probe_rate_window_samples', 'probe_rate_window_duration_ms',
                                 'probe_rate_window_clock', 'probe_last_sample_age_ms'])
@pytest.mark.parametrize('invalid', ['missing', None, False])
def test_unavailable_window_fields_are_unknown_not_zero_or_inferred(key, invalid):
    data = evidence()
    if invalid == 'missing':
        del data['snapshot']['topics'][0][key]
    else:
        data['snapshot']['topics'][0][key] = invalid
    rubric = rubric_for('case-001', data, 'topic_window', '/output')
    claim = claims(rubric)[key]
    assert claim['status'] == 'unknown' and claim['value'] is None
    assert claim['evidence'] == ['/snapshot/topics/0']
    answer = copy.deepcopy(rubric)
    next(c for c in answer['claims'] if c['id'] == key).update(status='observed', value=0)
    assert not score(data, rubric, answer)['passed']


def test_legacy_window_controls_still_fail_when_all_metric_citations_share_parent():
    data = evidence()
    data['snapshot']['topics'][0] = {'name': '/output', 'rate_hz': 30}
    rubric = rubric_for('case-001', data, 'topic_window', '/output')
    assert all(not score(data, rubric, answer)['passed'] for answer in negative_controls(rubric).values())


def test_fallback_clock_is_observed_but_unrecognized_clock_stays_unknown():
    data = evidence()
    data['snapshot']['topics'][0]['probe_rate_window_clock'] = 'callback_monotonic'
    assert claims(rubric_for('case-001', data, 'topic_window', '/output'))['probe_rate_window_clock']['value'] == 'callback_monotonic'
    data['snapshot']['topics'][0]['probe_rate_window_clock'] = 'source_timestamp'
    assert claims(rubric_for('case-001', data, 'topic_window', '/output'))['probe_rate_window_clock']['status'] == 'unknown'


def test_task_hardware_and_graph_name_match_do_not_establish_node_origin():
    data = evidence()
    rubric = rubric_for('case-001', data, 'diagnostics', '/worker')
    observed = claims(rubric)
    assert observed['diagnostic_observation_count']['value'] == 1
    assert observed['diagnostic_1_name']['evidence'] == ['/snapshot/diagnostics/1/name']
    assert observed['diagnostic_issue_1_related_diagnostics']['value'] == ['/worker']
    assert observed['diagnostic_issue_1_related_nodes']['value'] == []
    assert not any(k.startswith('diagnostic_0_') for k in observed)
    assert all(c['status'] == 'not_established' for c in rubric['limitations'])
    assert score(data, rubric, rubric)['passed']
    controls = negative_controls(rubric)
    assert 'task_label_proves_node_origin' in controls
    assert 'diagnostic_reference_as_node_reference' in controls
    assert all(not score(data, rubric, answer)['passed'] for answer in controls.values())


def test_legacy_declared_node_label_is_preserved_without_retrofitting_diagnostic_ref():
    data = evidence()
    issue = data['snapshot']['issues'][1]
    del issue['related_diagnostics']
    issue['related_nodes'] = ['/worker']
    rubric = rubric_for('case-001', data, 'diagnostics', '/worker')
    observed = claims(rubric)
    assert observed['diagnostic_issue_1_related_nodes']['value'] == ['/worker']
    assert observed['diagnostic_issue_1_related_diagnostics'] == {
        'id': 'diagnostic_issue_1_related_diagnostics', 'status': 'unknown', 'value': None,
        'evidence': ['/snapshot/issues/1']}
    assert score(data, rubric, rubric)['passed']
    assert all(not score(data, rubric, answer)['passed'] for answer in negative_controls(rubric).values())


@pytest.mark.parametrize('diagnostics,expected', [('missing', None), ([], 0), ([{'name': 'unrelated'}], 0)])
def test_missing_diagnostics_and_absent_task_are_distinct(diagnostics, expected):
    data = evidence()
    if diagnostics == 'missing':
        del data['snapshot']['diagnostics']
    else:
        data['snapshot']['diagnostics'] = diagnostics
    rubric = rubric_for('case-001', data, 'diagnostics', '/worker')
    count = claims(rubric)['diagnostic_observation_count']
    assert count['value'] == expected
    assert count['status'] == ('unknown' if expected is None else 'observed')
    assert score(data, rubric, rubric)['passed']


def preparation_args(output, **options):
    defaults = dict(output=output, integration=None, nav_recordings=None,
                    trace_recordings=None, duration_recording=None, duration_target=None,
                    public_live=None, public_legacy=None, public_bag=None, synthetic_demo=False)
    defaults.update(options)
    return SimpleNamespace(**defaults)


def test_export_last_frame_preserves_sources_without_phase_labels_or_health_rewrite(tmp_path):
    source = tmp_path / 'healthy.rgd.json'
    first = evidence()['snapshot']
    last = copy.deepcopy(first); last['timestamp'] += 1
    header = make_header(1000, 1)
    write_recording(str(source), header, [first, last])
    bundle = tmp_path / 'bundle'
    prepare(preparation_args(bundle, window_recording=[source], window_topic='/output',
                             diagnostic_recording=[source], diagnostic_name='/worker'))
    manifest = json.loads((bundle / 'manifest.json').read_text())
    assert len(manifest) == 2
    assert 'briefing_tool' not in manifest[0]
    assert manifest[1]['briefing_tool'] == 'get_runtime_briefing'
    for case in manifest:
        ident = case['case_id']
        exported_header, frames = read_recording(str(bundle / 'participant' / ident / 'input.rgd.json'))
        assert exported_header == header and frames == [last]
        provenance = json.loads((bundle / 'evaluator' / ident / 'provenance.json').read_text())
        assert provenance['snapshot_index'] == 1 and provenance['source_kind'] == 'provided_source'
        assert 'healthy.rgd.json' not in (bundle / 'participant' / ident / 'prompt.md').read_text()
        assert source.resolve().as_posix() == provenance['source']


@pytest.mark.parametrize('option,target', [('window_recording', 'window_topic'),
                                         ('diagnostic_recording', 'diagnostic_name')])
def test_recording_target_is_required(tmp_path, option, target):
    with pytest.raises(AssertionError, match='requires --' + target.replace('_', '-')):
        prepare(preparation_args(tmp_path / 'bundle', **{option: [tmp_path / 'missing.rgd.json']}))
