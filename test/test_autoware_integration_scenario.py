"""Reject misleading evidence in the optional public-input Autoware scenario."""

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
    spec = importlib.util.spec_from_file_location('autoware_scenario',
                                                 integration / 'run_autoware_scenario.py')
    scenario = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scenario)
finally:
    sys.path.pop(0)


def sample(phase):
    fault = phase == 'missing-imu'
    return {
        'timestamp': 0.0,
        'nodes': [{'id': scenario.GYRO}, {'id': scenario.CONVERTER}],
        'topics': [
            {'name': scenario.IMU, 'publishers': [] if fault else ['/rosbag2_player'],
             'subscribers': [scenario.GYRO], 'probed': True, 'rate_hz': 30},
            {'name': scenario.CONVERTED, 'publishers': [scenario.CONVERTER],
             'subscribers': [scenario.GYRO], 'probed': True, 'rate_hz': 30},
            {'name': scenario.OUTPUT, 'publishers': [scenario.GYRO],
             'subscribers': ['/driver'], 'probed': True, 'rate_hz': 30}],
        'diagnostics': [{'name': scenario.DIAGNOSTIC, 'level': 2 if fault else 0,
                         'message': 'IMU msg is timeout. tolerance 0.2[sec]' if fault else 'OK'}],
        'tf_edges': [
            {'parent': 'base_link', 'child': 'sensor_kit_base_link', 'static': True, 'status': 'ok'},
            {'parent': 'sensor_kit_base_link', 'child': 'tamagawa/imu_link', 'static': True, 'status': 'ok'}],
        'issues': [
            {'kind': 'diagnostics_error', 'related_nodes': [scenario.DIAGNOSTIC],
             'related_topics': [], 'evidence': ['level: 2']},
            {'kind': 'no_publisher', 'related_nodes': [scenario.GYRO],
             'related_topics': [scenario.IMU], 'evidence': ['publishers: 0']},
            {'kind': 'topic_stale', 'related_nodes': [scenario.GYRO],
             'related_topics': [scenario.OUTPUT], 'evidence': ['last message: 3000 ms ago']}]
        if fault else [],
    }


@pytest.mark.parametrize('phase', ['healthy', 'missing-imu', 'recovered'])
def test_phase_requires_actual_graph_and_diagnostic(phase):
    scenario.validate_snapshot(sample(phase), phase)


@pytest.mark.parametrize('failure', [
    'wrong_node', 'wrong_publisher', 'wrong_subscriber', 'unprobed', 'slow_converter',
    'unknown_converter_rate', 'unknown_output_rate', 'unknown_imu_rate',
    'healthy_has_error', 'healthy_has_missing_input', 'healthy_has_qos', 'healthy_has_rate_drop',
])
def test_healthy_rejects_missing_or_contradictory_evidence(failure):
    capture = sample('healthy')
    if failure == 'wrong_node':
        capture['nodes'][0]['id'] = '/substitute'
    elif failure == 'wrong_publisher':
        capture['topics'][2]['publishers'] = ['/substitute']
    elif failure == 'wrong_subscriber':
        capture['topics'][1]['subscribers'] = ['/substitute']
    elif failure == 'unprobed':
        capture['topics'][2]['probed'] = False
    elif failure == 'slow_converter':
        capture['topics'][1]['rate_hz'] = scenario.MIN_HZ - 0.01
    elif failure.startswith('unknown_'):
        index = {'unknown_converter_rate': 1, 'unknown_output_rate': 2, 'unknown_imu_rate': 0}[failure]
        capture['topics'][index]['rate_hz'] = None
    elif failure == 'healthy_has_error':
        capture['diagnostics'][0]['level'] = 2
    elif failure == 'healthy_has_missing_input':
        capture['topics'][0]['publishers'] = []
    else:
        capture['issues'].append({'kind': 'qos_mismatch' if failure == 'healthy_has_qos' else 'rate_drop'})
    with pytest.raises(AssertionError):
        scenario.validate_snapshot(capture, 'healthy')


@pytest.mark.parametrize('failure', [
    'input_still_published', 'unrelated_diagnostic', 'wrong_error',
    'missing_issue', 'wrong_kind', 'wrong_topic', 'wrong_node', 'wrong_evidence',
])
def test_missing_imu_requires_attributed_fault_evidence(failure):
    capture = sample('missing-imu')
    if failure == 'input_still_published':
        capture['topics'][0]['publishers'] = ['/rosbag2_player']
    elif failure == 'unrelated_diagnostic':
        capture['diagnostics'][0]['name'] = 'other component'
    elif failure == 'wrong_error':
        capture['diagnostics'][0]['message'] = 'Please publish TF'
    elif failure == 'missing_issue':
        capture['issues'].pop(2)
    elif failure == 'wrong_kind':
        capture['issues'][1]['kind'] = 'another_kind'
    elif failure == 'wrong_topic':
        capture['issues'][1]['related_topics'] = ['/another/input']
    elif failure == 'wrong_node':
        capture['issues'][2]['related_nodes'] = ['/another/node']
    else:
        capture['issues'][1]['evidence'] = ['publishers: 1']
    with pytest.raises((AssertionError, StopIteration)):
        scenario.validate_snapshot(capture, 'missing-imu')


def test_one_bad_settled_sample_cannot_be_hidden_by_summary(tmp_path):
    captures = [copy.deepcopy(sample('recovered')) for _ in range(7)]
    for index, capture in enumerate(captures):
        capture['timestamp'] = float(index)
    result = {'passed': True, 'samples': 6, 'max_breach': 0}
    (tmp_path / 'assert.json').write_text(json.dumps(result))
    path = str(tmp_path / 'run.rgd.json')
    write_recording(path, make_header(0, 1), captures)
    scenario.validate_recording(tmp_path, 'recovered')
    captures[4]['diagnostics'][0]['level'] = 2
    write_recording(path, make_header(0, 1), captures)
    with pytest.raises(AssertionError):
        scenario.validate_recording(tmp_path, 'recovered')


@pytest.mark.parametrize('failure', [None, 'missing_transform', 'dynamic_transform', 'wrong_subscriber',
                                   'wrong_topic', 'missing_evidence'])
def test_static_only_listener_warning_requires_known_calibration(failure):
    capture = sample('healthy')
    warning = {'kind': 'no_publisher', 'related_topics': ['/tf'],
               'related_nodes': ['/transform_listener_impl_123'], 'evidence': ['publishers: 0']}
    capture['issues'].append(warning)
    if failure is None:
        scenario.validate_snapshot(capture, 'healthy')
        assert warning in capture['issues']  # The fact stays visible in the recording.
        return
    if failure == 'missing_transform':
        capture['tf_edges'].pop()
    elif failure == 'dynamic_transform':
        capture['tf_edges'][0]['static'] = False
    elif failure == 'wrong_subscriber':
        warning['related_nodes'] = [scenario.GYRO]
    elif failure == 'wrong_topic':
        warning['related_topics'] = [scenario.IMU]
    else:
        warning['evidence'] = []
    with pytest.raises(AssertionError):
        scenario.validate_snapshot(capture, 'healthy')
