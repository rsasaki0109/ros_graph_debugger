"""Missing observation or misattributed evidence cannot count as Nav2 recovery."""

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from ros_graph_debugger.recording import make_header, write_recording

integration = Path(__file__).parent / 'integration'
sys.path.insert(0, str(integration))
try:
    spec = importlib.util.spec_from_file_location('navigation_scenario',
                                                 integration / 'run_navigation_scenario.py')
    scenario = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scenario)
finally:
    sys.path.pop(0)


def snapshot(paused):
    issues = []
    if paused:
        issues += [{'kind': 'tf_stale', 'severity': 'critical', 'related_frames': list(frames),
                    'related_nodes': [], 'related_topics': [], 'evidence': ['age: 3000 ms']}
                   for frames in scenario.CHAIN]
        issues += [{'kind': 'topic_stale', 'severity': 'critical', 'related_frames': [],
                    'related_nodes': ['/loopback_simulator'], 'related_topics': [name],
                    'evidence': ['last message: 3000 ms ago', 'expected within: 2000 ms']}
                   for name in ('/scan', '/odom')]
    return {'timestamp': 0.0, 'profile': 'nav2',
            'nodes': [{'id': name, 'lifecycle_state': 'active'} for name in scenario.MANAGED]
                     + [{'id': '/loopback_simulator'}],
            'topics': [{'name': name, 'probed': True, 'publisher_count': 1,
                        'publishers': ['/controller_server' if name == '/cmd_vel'
                                       else '/loopback_simulator'],
                        'last_seen_time': 1000.0, 'rate_hz': rate * 2,
                        'age_ms': 3000 if paused else 100}
                       for name, rate in scenario.RATES.items()],
            'tf_edges': [{'parent': parent, 'child': child, 'static': False,
                          'age_ms': 3000 if paused else 100,
                          'status': 'critical' if paused else 'ok'}
                         for parent, child in scenario.CHAIN], 'issues': issues}


@pytest.mark.parametrize('paused', [False, True])
def test_navigation_phase_requires_state_and_observation(paused):
    scenario.validate_snapshot(snapshot(paused), paused)


@pytest.mark.parametrize('failure', ['wrong_frames', 'unknown_state', 'missing_topic_issue',
                                     'fresh_transform', 'wrong_profile', 'wrong_publisher'])
def test_fault_rejects_incomplete_or_misattributed_evidence(failure):
    sample = snapshot(True)
    if failure == 'wrong_frames':
        sample['issues'][0]['related_frames'] = ['map', 'wrong']
    elif failure == 'unknown_state':
        sample['nodes'][0]['lifecycle_state'] = 'unknown'
    elif failure == 'missing_topic_issue':
        sample['issues'] = [i for i in sample['issues'] if i['related_topics'] != ['/scan']]
    elif failure == 'fresh_transform':
        sample['tf_edges'][0]['age_ms'] = 100
    elif failure == 'wrong_profile':
        sample['profile'] = 'unknown'
    else:
        sample['topics'][0]['publishers'] = ['/another_simulator']
    with pytest.raises(AssertionError):
        scenario.validate_snapshot(sample, True)


def recording(directory, paused):
    samples = [copy.deepcopy(snapshot(paused)) for _ in range(5)]
    for i, sample in enumerate(samples):
        sample['timestamp'] = float(i)
    result = {'passed': not paused, 'samples': 4, 'max_breach': 0,
              'checks': [{'name': f'no {kind} issues', 'breaches': 4 if paused else 0}
                         for kind in ('tf_stale', 'topic_stale')]}
    (directory / 'assert.json').write_text(json.dumps(result))
    write_recording(str(directory / 'run.rgd.json'), make_header(0, 1), samples)
    return samples


def test_stopped_recording_cannot_refresh_receipt_time(tmp_path):
    samples = recording(tmp_path, True)
    scenario.validate_phase(tmp_path, True)
    samples[2]['topics'][0]['last_seen_time'] += 1
    write_recording(str(tmp_path / 'run.rgd.json'), make_header(0, 1), samples)
    with pytest.raises(AssertionError):
        scenario.validate_phase(tmp_path, True)


def test_cli_pass_cannot_hide_recorded_fault(tmp_path):
    samples = recording(tmp_path, False)
    scenario.validate_phase(tmp_path, False)
    samples[2]['issues'] = snapshot(True)['issues']
    write_recording(str(tmp_path / 'run.rgd.json'), make_header(0, 1), samples)
    with pytest.raises(AssertionError):
        scenario.validate_phase(tmp_path, False)


def test_diff_requires_actual_sensor_and_frame_issues_resolved(tmp_path):
    result = {'verdict': 'improved', 'new_issues': [], 'resolved_issues': snapshot(True)['issues']}
    (tmp_path / 'diff.json').write_text(json.dumps(result))
    scenario.validate_diff(tmp_path)
    result['resolved_issues'][-1]['related_topics'] = ['/unrelated']
    (tmp_path / 'diff.json').write_text(json.dumps(result))
    with pytest.raises(AssertionError):
        scenario.validate_diff(tmp_path)
