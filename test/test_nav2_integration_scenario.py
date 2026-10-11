"""The real Nav2 runner must require lifecycle and data-delivery evidence."""

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
    spec = importlib.util.spec_from_file_location('nav2_scenario', integration / 'run_nav2_scenario.py')
    scenario = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scenario)
finally:
    sys.path.pop(0)


def capture(directory, state):
    issues = [{'kind': 'lifecycle_not_active', 'severity': 'critical',
               'related_nodes': ['/map_server'], 'related_topics': ['/map'],
               'evidence': ['lifecycle state: inactive', '/map: 1 subscriber(s) waiting']}]
    samples = [{'timestamp': float(t),
                'nodes': [{'id': '/map_server', 'lifecycle_state': state}],
                'topics': [{'name': '/map', 'publishers': ['/map_server'],
                            'subscribers': ['/nav2_map_observer']}],
                'issues': copy.deepcopy(issues) if state == 'inactive' else []}
               for t in range(5)]
    result = {'passed': state == 'active', 'samples': 4, 'max_breach': 0,
              'checks': [{'name': 'no lifecycle_not_active issues',
                          'breaches': 4 if state == 'inactive' else 0},
                         {'name': 'node /map_server present', 'breaches': 0},
                         {'name': 'topic /map present', 'breaches': 0}]}
    (directory / 'assert.json').write_text(json.dumps(result))
    write_recording(str(directory / 'run.rgd.json'), make_header(0, 1), samples)
    return samples


@pytest.mark.parametrize('state', ['inactive', 'active'])
def test_lifecycle_fault_and_recovered_contracts(tmp_path, state):
    capture(tmp_path, state)
    scenario.validate_recording(tmp_path, state)


@pytest.mark.parametrize('failure', ['missing_issue', 'wrong_topic', 'missing_waiting', 'unknown_state'])
def test_fault_requires_recorded_evidence(tmp_path, failure):
    samples = capture(tmp_path, 'inactive')
    sample = samples[1]
    if failure == 'missing_issue':
        sample['issues'] = []
    elif failure == 'wrong_topic':
        sample['issues'][0]['related_topics'] = ['/wrong']
    elif failure == 'missing_waiting':
        sample['issues'][0]['evidence'] = ['lifecycle state: inactive']
    else:
        sample['nodes'][0]['lifecycle_state'] = 'unknown'
    write_recording(str(tmp_path / 'run.rgd.json'), make_header(0, 1), samples)
    with pytest.raises(AssertionError):
        scenario.validate_recording(tmp_path, 'inactive')


def test_successful_cli_cannot_hide_ongoing_lifecycle_fault(tmp_path):
    samples = capture(tmp_path, 'active')
    samples[1]['issues'] = [{'kind': 'lifecycle_not_active', 'related_nodes': ['/map_server']}]
    write_recording(str(tmp_path / 'run.rgd.json'), make_header(0, 1), samples)
    with pytest.raises(AssertionError):
        scenario.validate_recording(tmp_path, 'active')


def test_receipt_requires_expected_map_contents(tmp_path):
    path = tmp_path / 'map-receipt.json'
    result = {'count': 1, 'map': copy.deepcopy(scenario.EXPECTED_MAP)}
    path.write_text(json.dumps(result))
    scenario.receipt_ready(path)
    # Correct dimensions are insufficient if map orientation/content is wrong.
    result['map']['data'][0] = 100
    path.write_text(json.dumps(result))
    with pytest.raises(AssertionError):
        scenario.receipt_ready(path)


def test_diff_requires_resolved_lifecycle_issue_for_actual_server(tmp_path):
    result = {'verdict': 'improved', 'new_issues': [],
              'resolved_issues': [{'kind': 'lifecycle_not_active',
                                   'related_nodes': ['/map_server'], 'related_topics': ['/map']}]}
    (tmp_path / 'diff.json').write_text(json.dumps(result))
    scenario.validate_diff(tmp_path)
    result['resolved_issues'][0]['related_nodes'] = ['/another_server']
    (tmp_path / 'diff.json').write_text(json.dumps(result))
    with pytest.raises(AssertionError):
        scenario.validate_diff(tmp_path)
