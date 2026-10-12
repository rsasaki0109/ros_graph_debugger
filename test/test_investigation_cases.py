"""Rubric controls are synthetic logic tests, distinct from real-source tasks."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'benchmarks'))
from investigation_cases import negative_controls, pointer, prepare, rubric_for, score
from ros_graph_debugger.recording import make_header, read_recording, write_recording


@pytest.fixture
def task():
    evidence = {'snapshot': {'timestamp': 1000, 'topics': [
        {'name': '/image', 'rate_hz': 6, 'rate_measurement_scope': 'probe_receipt',
         'probe_source_receipt_gap_p95_ms': -20, 'probe_callback_delay_p95_ms': 400,
         'header_age_p95_ms': 1300}], 'issues': []},
        'recording_metadata': {'mode': 'replay', 'recording_header': {}}}
    return evidence, rubric_for('case-001', evidence, 'topic', '/image')


def test_signed_and_missing_observations_have_distinct_citations(task):
    evidence, rubric = task
    assert score(evidence, rubric, rubric)['passed']
    claims = {c['id']: c for c in rubric['claims']}
    assert claims['dds_stamp_gap_p95_ms']['value'] == -20
    assert claims['local_probe_wait_p95_ms']['value'] == 400
    assert claims['header_age_p95_ms']['value'] == 1300
    assert claims['recorded_header_age_clock']['status'] == 'unknown'
    assert claims['recorded_header_age_clock']['evidence'] == ['/recording_metadata/recording_header']


@pytest.mark.parametrize('control', ['wrong_frame', 'wrong_citation', 'missing_fact',
                                    'duplicate_fact', 'invented_value', 'unsupported_cause',
                                    'irrelevant_next_check'])
def test_bad_structured_answers_fail(task, control):
    evidence, rubric = task
    assert not score(evidence, rubric, negative_controls(rubric)[control])['passed']


def test_unknown_cpu_cannot_be_reported_as_idle():
    evidence = {'snapshot': {'nodes': [{'id': '/worker', 'pid': 123, 'cpu_percent': None,
                                        'process_mapping_confidence': 'high'}], 'issues': []}}
    rubric = rubric_for('case-001', evidence, 'cpu', '/worker')
    answer = copy.deepcopy(rubric)
    next(c for c in answer['claims'] if c['id'] == 'cpu_percent').update(status='observed', value=0)
    assert not score(evidence, rubric, answer)['passed']
    assert 'idle_from_missing_cpu_sample' in {c['id'] for c in rubric['limitations']}


def test_changed_evidence_and_boolean_rate_are_rejected(task):
    evidence, rubric = task
    changed = copy.deepcopy(evidence); changed['snapshot']['topics'][0]['rate_hz'] = 7
    assert not score(changed, rubric, rubric)['passed']
    evidence['snapshot']['topics'][0]['rate_hz'] = 1
    rubric = rubric_for('case-001', evidence, 'topic', '/image')
    answer = copy.deepcopy(rubric)
    next(c for c in answer['claims'] if c['id'] == 'rate_hz')['value'] = True
    assert not score(evidence, rubric, answer)['passed']


def test_missing_value_field_and_unknown_clock_basis_fail(task):
    evidence, rubric = task
    answer = copy.deepcopy(rubric)
    del next(c for c in answer['claims'] if c['id'] == 'recorded_header_age_clock')['value']
    assert not score(evidence, rubric, answer)['passed']
    evidence['recording_metadata']['recording_header']['header_age_clock'] = {'selected': 'future_clock'}
    rubric = rubric_for('case-001', evidence, 'topic', '/image')
    clock = next(c for c in rubric['claims'] if c['id'] == 'recorded_header_age_clock')
    assert clock['status'] == 'unknown' and clock['value'] is None


def test_pointer_escaping_and_invalid_indices():
    document = {'a/b': {'c~d': [42]}}
    assert pointer(document, '/a~1b/c~0d/0') == 42
    for path in ('/a~2b', '/a~1b/c~0d/-1', '/a~1b/c~0d/00'):
        with pytest.raises((KeyError, ValueError)):
            pointer(document, path)


def duration_task(rows=None, *, include_callbacks=True):
    snapshot = {'timestamp': 1000, 'nodes': [{'id': '/worker'}], 'issues': []}
    if include_callbacks:
        snapshot['callbacks'] = rows if rows is not None else []
    evidence = {'snapshot': snapshot, 'recording_metadata': {'recording_header': {}}}
    return evidence, rubric_for('case-001', evidence, 'duration', '/worker')


def duration_row(source='application_report', **metrics):
    row = {'node': '/worker', 'callback': 'processing_time', 'topic': '/debug/time',
           'count': 8, 'mean_ms': 80, 'p95_ms': 120, 'max_ms': 130, 'source': source}
    row.update(metrics)
    return row


@pytest.mark.parametrize('source,limitation,next_check', [
    ('application_report', 'callback_start_end_from_application_report', 'collect_paired_callback_start_end'),
    ('trace', 'application_cycle_time_from_callback_trace', 'inspect_application_cycle_boundaries'),
    ('synthetic', 'measured_runtime_duration_from_synthetic_stats', 'collect_real_duration_measurements'),
    (None, 'callback_start_end_from_unknown_source', 'establish_duration_measurement_method'),
])
def test_duration_source_semantics_and_direct_citations(source, limitation, next_check):
    evidence, rubric = duration_task([duration_row(source)])
    assert score(evidence, rubric, rubric)['passed']
    claims = {c['id']: c for c in rubric['claims']}
    declared = claims['duration_0_declared_source']
    assert (declared['status'], declared['value']) == ('unknown' if source is None else 'observed', source)
    assert claims['duration_0_p95_ms']['evidence'] == ['/snapshot/callbacks/0/p95_ms']
    limits = {c['id']: c for c in rubric['limitations']}
    assert limits['duration_0_' + limitation]['next_check'] == next_check
    assert all(c['status'] == 'not_established' for c in limits.values())
    assert 'duration_0_authenticated_provenance_from_source_field' in limits
    for label, answer in negative_controls(rubric).items():
        assert not score(evidence, rubric, answer)['passed'], label
    assert len(negative_controls(rubric)) == 10


@pytest.mark.parametrize('source', ['future_source', True, ['trace'], {'source': 'trace'}])
def test_unrecognized_source_does_not_establish_callback_method(source):
    evidence, rubric = duration_task([duration_row(source)])
    declared = next(c for c in rubric['claims'] if c['id'] == 'duration_0_declared_source')
    assert declared == {'id': 'duration_0_declared_source', 'status': 'unknown', 'value': None,
                        'evidence': ['/snapshot/callbacks/0']}
    assert score(evidence, rubric, rubric)['passed']


def test_legacy_label_filename_and_numbers_do_not_infer_trace_source():
    row = duration_row('trace'); del row['source']
    evidence, rubric = duration_task([row])
    # The rubric receives no path: even a processing_time label / plausible numbers
    # cannot replace missing source metadata.
    source = next(c for c in rubric['claims'] if c['id'] == 'duration_0_declared_source')
    assert source['status'] == 'unknown' and source['value'] is None
    assert source['evidence'] == ['/snapshot/callbacks/0']
    assert not score(evidence, rubric, negative_controls(rubric)['duration_source_confusion'])['passed']


def test_cross_callback_citations_and_equal_percentiles_are_not_interchangeable():
    evidence, rubric = duration_task([duration_row('application_report'), duration_row('trace')])
    answer = copy.deepcopy(rubric)
    claim = next(c for c in answer['claims'] if c['id'] == 'duration_0_p95_ms')
    claim['evidence'] = ['/snapshot/callbacks/1/p95_ms']
    assert not score(evidence, rubric, answer)['passed']
    answer = copy.deepcopy(rubric)
    claim = next(c for c in answer['claims'] if c['id'] == 'duration_0_declared_source')
    claim.update(value='trace', evidence=['/snapshot/callbacks/1/source'])
    assert not score(evidence, rubric, answer)['passed']


def test_target_filter_preserves_original_callback_indexes():
    unrelated = duration_row('trace', node='/other')
    evidence, rubric = duration_task([unrelated, duration_row()])
    claims = {c['id']: c for c in rubric['claims']}
    assert claims['duration_observation_count']['value'] == 1
    assert claims['duration_1_p95_ms']['evidence'] == ['/snapshot/callbacks/1/p95_ms']
    assert not any(ident.startswith('duration_0_') for ident in claims)
    assert score(evidence, rubric, rubric)['passed']


@pytest.mark.parametrize('metric', ['count', 'mean_ms', 'p95_ms', 'max_ms'])
def test_missing_duration_metrics_are_not_zero_or_boolean(metric):
    row = duration_row(); del row[metric]
    evidence, rubric = duration_task([row])
    ident = 'duration_0_' + metric
    claim = next(c for c in rubric['claims'] if c['id'] == ident)
    assert claim['status'] == 'unknown' and claim['value'] is None
    assert claim['evidence'] == ['/snapshot/callbacks/0']
    for value in (0, False):
        answer = copy.deepcopy(rubric)
        next(c for c in answer['claims'] if c['id'] == ident).update(status='observed', value=value)
        assert not score(evidence, rubric, answer)['passed']


@pytest.mark.parametrize('include_callbacks', [True, False])
def test_no_duration_observations_do_not_establish_zero_callback_work(include_callbacks):
    evidence, rubric = duration_task(include_callbacks=include_callbacks)
    count = next(c for c in rubric['claims'] if c['id'] == 'duration_observation_count')
    assert count['value'] == (0 if include_callbacks else None)
    assert count['status'] == ('observed' if include_callbacks else 'unknown')
    assert 'no_callback_work_from_missing_observations' in {c['id'] for c in rubric['limitations']}
    assert score(evidence, rubric, rubric)['passed']


def preparation_args(output, **options):
    defaults = dict(output=output, integration=None, nav_recordings=None,
                    trace_recordings=None, duration_recording=None, duration_target=None,
                    public_live=None, public_legacy=None, public_bag=None, synthetic_demo=False)
    defaults.update(options)
    return SimpleNamespace(**defaults)


def trace_sources(root):
    """Constructed fixture for exporter gates, not an actual trace experiment."""
    frames = {}
    for phase in ('slow', 'healthy'):
        target = '/trace_' + phase
        value = 150 if phase == 'slow' else 1
        callbacks = [duration_row('trace', node=target, callback=label,
                                  topic=topic, p95_ms=value, mean_ms=value, max_ms=value)
                     for label, topic in ((f'sub {target}/input', target + '/input'),
                                          ('timer 200 ms', ''), (f'srv {target}/reset', ''))]
        issues = [{'kind': 'slow_callback', 'related_nodes': [target],
                   'evidence': ['duration source: paired callback trace']} for _ in callbacks] \
            if phase == 'slow' else []
        frames[phase] = [{'timestamp': t, 'nodes': [{'id': target}], 'topics': [],
                          'callbacks': copy.deepcopy(callbacks), 'issues': copy.deepcopy(issues)}
                         for t in (100, 102)]
        (root / phase).mkdir(parents=True)
        write_recording(str(root / phase / 'run.rgd.json'), make_header(100, 1), frames[phase])
    return frames


def test_trace_export_preserves_original_frame_and_header(tmp_path):
    root = tmp_path / 'traces'; frames = trace_sources(root)
    output = tmp_path / 'bundle'
    prepare(preparation_args(output, trace_recordings=root))
    manifest = json.loads((output / 'manifest.json').read_text())
    assert len(manifest) == 2
    for case, phase in zip(manifest, ('slow', 'healthy')):
        header, exported = read_recording(str(output / 'participant' / case['case_id'] / 'input.rgd.json'))
        original_header, _ = read_recording(str(root / phase / 'run.rgd.json'))
        assert header == original_header and exported == [frames[phase][1]]
        provenance = json.loads((output / 'evaluator' / case['case_id'] / 'provenance.json').read_text())
        assert provenance['snapshot_index'] == 1 and provenance['source_kind'] == 'provided_source'


@pytest.mark.parametrize('fault', ['missing_source', 'wrong_source', 'missing_callback',
                                  'wrong_callback_node', 'low_count', 'missing_evidence', 'wrong_issue_node'])
def test_trace_export_rejects_incomplete_original_contracts(tmp_path, fault):
    root = tmp_path / 'traces'; frames = trace_sources(root)
    frame = frames['slow'][1]
    if fault == 'missing_source':
        del frame['callbacks'][0]['source']
    elif fault == 'wrong_source':
        frame['callbacks'][0]['source'] = 'application_report'
    elif fault == 'missing_callback':
        frame['callbacks'].pop()
    elif fault == 'wrong_callback_node':
        frame['callbacks'][0]['node'] = '/unrelated'
    elif fault == 'low_count':
        frame['callbacks'][0]['count'] = 7
    elif fault == 'missing_evidence':
        frame['issues'][0]['evidence'] = []
    else:
        frame['issues'][0]['related_nodes'] = ['/unrelated']
    write_recording(str(root / 'slow' / 'run.rgd.json'), make_header(100, 1), frames['slow'])
    with pytest.raises(AssertionError, match='original trace contracts not met'):
        prepare(preparation_args(tmp_path / 'bundle', trace_recordings=root))


def test_synthetic_control_is_explicitly_separate_from_provided_data(tmp_path, capsys):
    prepare(preparation_args(tmp_path / 'bundle', synthetic_demo=True))
    provenance = json.loads((tmp_path / 'bundle/evaluator/case-001/provenance.json').read_text())
    assert provenance['source_kind'] == 'synthetic_control'
    assert '0 source cases and 1 synthetic control cases' in capsys.readouterr().out
    evidence = json.loads((tmp_path / 'bundle/participant/case-001/evidence.json').read_text())
    rubric = json.loads((tmp_path / 'bundle/evaluator/case-001/rubric.json').read_text())
    assert next(c for c in rubric['claims'] if c['id'].endswith('_declared_source'))['value'] == 'synthetic'
    assert score(evidence, rubric, rubric)['passed']
