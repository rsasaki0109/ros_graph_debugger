"""Synthetic rubric controls; distinct from the optional public-input experiment."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'benchmarks'))
from investigation_cases import negative_controls, save, score
from phase_investigation import LIMITS, PHASES, phase_rubric, prepare, validate_audit
from ros_graph_debugger.recording import make_header, read_recording, write_recording


@pytest.fixture
def evidence():
    target = dict(node='/worker', topic='/imu', callback='sub /imu')
    other = dict(node='/other', topic='/velocity', callback='sub /velocity')
    phases = []
    for index, phase in enumerate(PHASES):
        rows = [dict(other, count=7, mean_ms=0.0, p95_ms=0.0, max_ms=0.1, source='trace')]
        if index != 1:
            rows.append(dict(target, count=7, mean_ms=0.0, p95_ms=0.0, max_ms=0.1, source='trace'))
        phases.append(dict(phase=phase, begin_ns=index*100, end_ns=index*100+50,
                           clock='ctf_raw_1ghz:12345678-1234-1234-1234-123456789abc',
                           boundary_crossing=1, invocations=sum(r['count'] for r in rows), callbacks=rows))
    return dict(target_callback=target, phase_audit=dict(run_id='synthetic-test', marker_pid=5,
                markers=6, total_paired=40, phases=phases))


def test_absence_is_zero_contained_count_but_unknown_duration(evidence):
    rubric = phase_rubric('case-001', evidence)
    claims = {c['id']: c for c in rubric['claims']}
    assert claims['phase_1_contained_count']['value'] == 0
    assert claims['phase_1_contained_count']['evidence'] == ['/phase_audit/phases/1/callbacks']
    for key in ('mean_ms', 'p95_ms', 'max_ms', 'source'):
        assert claims['phase_1_' + key]['status'] == 'unknown'
        assert claims['phase_1_' + key]['value'] is None
        assert claims['phase_1_' + key]['evidence'] == ['/phase_audit/phases/1/callbacks']
    assert claims['phase_0_p95_ms']['value'] == 0.0
    assert claims['phase_0_p95_ms']['evidence'] == ['/phase_audit/phases/0/callbacks/1/p95_ms']
    assert {c['id'] for c in rubric['limitations']} == set(LIMITS)
    assert score(evidence, rubric, rubric)['passed']
    controls = negative_controls(rubric)
    assert len(controls) == 19
    for label, answer in controls.items():
        assert not score(evidence, rubric, answer)['passed'], label


@pytest.mark.parametrize('value', [0.0, False, '0'])
def test_equal_numeric_boundaries_still_require_integer(evidence, value):
    rubric = phase_rubric('case-001', evidence)
    answer = copy.deepcopy(rubric)
    next(c for c in answer['claims'] if c['id'] == 'phase_0_begin_ns')['value'] = value
    assert not score(evidence, rubric, answer)['passed']


def test_same_topic_and_equal_counts_do_not_allow_other_owner_citations(evidence):
    evidence['phase_audit']['phases'][0]['callbacks'][0]['topic'] = '/imu'
    rubric = phase_rubric('case-001', evidence)
    answer = copy.deepcopy(rubric)
    next(c for c in answer['claims'] if c['id'] == 'phase_0_contained_count')['evidence'] = [
        '/phase_audit/phases/0/callbacks/0/count']
    assert not score(evidence, rubric, answer)['passed']


@pytest.mark.parametrize('mutation', [
    'markers', 'marker_bool', 'missing_run', 'phase_order', 'overlap', 'float_boundary',
    'negative_boundary', 'unknown_clock', 'epoch_clock', 'mixed_clock', 'duplicate_row',
    'zero_count', 'boolean_count', 'source', 'nan', 'negative_duration', 'inconsistent_duration',
    'sum_mismatch', 'small_total', 'crossing_bool',
])
def test_inconsistent_audit_cannot_prepare_observed_claims(evidence, mutation):
    audit = evidence['phase_audit']
    p, row = audit['phases'][0], audit['phases'][0]['callbacks'][0]
    if mutation == 'markers': audit['markers'] = 5
    elif mutation == 'marker_bool': audit['marker_pid'] = True
    elif mutation == 'missing_run': audit['run_id'] = ''
    elif mutation == 'phase_order': audit['phases'].reverse()
    elif mutation == 'overlap': audit['phases'][1]['begin_ns'] = p['end_ns']
    elif mutation == 'float_boundary': p['begin_ns'] = 0.0
    elif mutation == 'negative_boundary': p['begin_ns'] = -1
    elif mutation == 'unknown_clock': p['clock'] = 'ctf_raw_1ghz:unknown'
    elif mutation == 'epoch_clock': p['clock'] = p['clock'].replace('ctf_raw_1ghz:', 'ctf_ns_from_origin:')
    elif mutation == 'mixed_clock': p['clock'] = 'ctf_raw_1ghz:00000000-0000-0000-0000-000000000000'
    elif mutation == 'duplicate_row': p['callbacks'].append(copy.deepcopy(row))
    elif mutation == 'zero_count': row['count'] = 0
    elif mutation == 'boolean_count': row['count'] = True
    elif mutation == 'source': row['source'] = 'application_report'
    elif mutation == 'nan': row['p95_ms'] = float('nan')
    elif mutation == 'negative_duration': row['p95_ms'] = -1
    elif mutation == 'inconsistent_duration': row['max_ms'] = 0; row['mean_ms'] = 1
    elif mutation == 'sum_mismatch': p['invocations'] += 1
    elif mutation == 'small_total': audit['total_paired'] = 1
    elif mutation == 'crossing_bool': p['boundary_crossing'] = False
    with pytest.raises((AssertionError, ValueError)):
        phase_rubric('case-001', evidence)


def test_target_typo_is_rejected_and_changed_sidecar_breaks_digest(evidence):
    rubric = phase_rubric('case-001', evidence)
    changed = copy.deepcopy(evidence)
    changed['phase_audit']['phases'][0]['callbacks'][1]['p95_ms'] = 0.1
    assert not score(changed, rubric, rubric)['passed']
    evidence['target_callback']['node'] = '/typo'
    with pytest.raises(AssertionError, match='absent from all phases'):
        phase_rubric('case-001', evidence)


def test_export_preserves_all_original_frames_headers_and_separate_audit(evidence, tmp_path):
    audit = tmp_path / 'audit.json'; save(audit, evidence['phase_audit'])
    recordings = tmp_path / 'recordings'
    original = []
    for index, phase in enumerate(PHASES):
        folder = recordings / phase; folder.mkdir(parents=True)
        header = make_header(1000+index, 1)
        header['header_age_clock'] = dict(selected='ros', original_phase_metadata=phase)
        frame = dict(timestamp=1000+index, nodes=[dict(id='/worker')], callbacks=[], issues=[],
                     unknown_extension=dict(phase=phase))
        write_recording(str(folder / 'run.rgd.json'), header, [dict(frame, timestamp=999), frame])
        original.append((header, frame))
    output = tmp_path / 'bundle'
    prepare(audit, recordings, [('/worker', '/imu')], output)
    participant = output / 'participant/case-001'
    exported = json.loads((participant / 'evidence.json').read_text())
    assert exported['phase_audit'] == evidence['phase_audit']
    assert exported['snapshot'] == original[-1][1] and exported['snapshot']['callbacks'] == []
    for phase, (header, frame), context in zip(PHASES, original, exported['phase_recordings']):
        assert read_recording(str(participant / (phase+'.rgd.json'))) == (header, [frame])
        assert context == dict(phase=phase, snapshot=frame, recording_metadata=dict(mode='replay', recording_header=header))
    provenance = json.loads((output / 'evaluator/case-001/provenance.json').read_text())
    assert [p['snapshot_index'] for p in provenance['recordings']] == [1, 1, 1]
    assert not (participant / 'rubric.json').exists()
    with pytest.raises(FileExistsError):
        prepare(audit, recordings, [('/worker', '/imu')], output)
