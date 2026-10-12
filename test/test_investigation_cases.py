"""Rubric controls are synthetic logic tests, distinct from real-source tasks."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'benchmarks'))
from investigation_cases import negative_controls, pointer, rubric_for, score


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
