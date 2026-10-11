"""The integration runner must reject missing evidence and clean descendants."""

import copy
import importlib.util
import json
from pathlib import Path
import signal
import sys
import time

import pytest

from ros_graph_debugger.recording import make_header, write_recording

spec = importlib.util.spec_from_file_location(
    'integration_scenario', Path(__file__).parent / 'integration' / 'run_scenario.py')
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


def capture(directory, mode='slow'):
    issues = [{'kind': kind, 'related_nodes': [], 'related_topics': [], 'evidence': []}
              for kind in scenario.KINDS[2:]]
    by_kind = {i['kind']: i for i in issues}
    by_kind['qos_mismatch'].update(
        related_topics=['/integration/deadline'], evidence=['deadline: 200 ms > 100 ms'])
    by_kind['lifecycle_not_active'].update(
        related_nodes=['/integration/managed'], evidence=[
            'lifecycle state: inactive', '/integration/managed/output: 1 subscriber(s) waiting'])
    for kind in ('no_service_server', 'no_action_server'):
        by_kind[kind].update(related_nodes=['/integration/clients'], evidence=['servers: 0'])
    by_kind['slow_callback'].update(
        related_nodes=['/control/integration/processing'],
        related_topics=['/control/integration/processing/debug/processing_time_ms'],
        evidence=['p95: 120 ms (limit 15 ms)'])
    if mode == 'slow':
        issues.append({'kind': 'rate_drop', 'related_nodes': ['/detector']})
    snapshots = [{'timestamp': float(t), 'issues': copy.deepcopy(issues),
                  'callbacks': [{'callback': 'processing_time', 'p95_ms': 120,
                                 'node': '/control/integration/processing'}]}
                 for t in range(20)]
    write_recording(str(directory / 'run.rgd.json'), make_header(0, 1), snapshots)
    result = {'passed': False, 'samples': 15,
              'checks': [{'name': f'no {kind} issues', 'breaches': 15}
                         for kind in scenario.KINDS]}
    (directory / 'assert.json').write_text(json.dumps(result))
    return snapshots


def rewrite(directory, snapshots):
    write_recording(str(directory / 'run.rgd.json'), make_header(0, 1), snapshots)


def test_complete_slow_and_helper_only_healthy_contracts(tmp_path):
    capture(tmp_path)
    scenario.validate_live(tmp_path, 'slow')
    capture(tmp_path, 'healthy')
    scenario.validate_live(tmp_path, 'healthy')


@pytest.mark.parametrize('kind', scenario.KINDS[2:])
def test_json_names_alone_do_not_prove_issue_collection(tmp_path, kind):
    snapshots = capture(tmp_path)
    for snapshot in snapshots:
        snapshot['issues'] = [i for i in snapshot['issues'] if i['kind'] != kind]
    rewrite(tmp_path, snapshots)
    with pytest.raises(AssertionError):
        scenario.validate_live(tmp_path, 'slow')


def test_qos_requires_deadline_evidence(tmp_path):
    snapshots = capture(tmp_path)
    for snapshot in snapshots:
        for issue in snapshot['issues']:
            if issue['kind'] == 'qos_mismatch':
                issue['evidence'] = ['reliability: incompatible']
    rewrite(tmp_path, snapshots)
    with pytest.raises(AssertionError):
        scenario.validate_live(tmp_path, 'slow')


def test_processing_requires_real_measurement_attribution(tmp_path):
    snapshots = capture(tmp_path)
    for snapshot in snapshots:
        snapshot['callbacks'][0]['node'] = '/unrelated'
    rewrite(tmp_path, snapshots)
    with pytest.raises(AssertionError):
        scenario.validate_live(tmp_path, 'slow')


def test_healthy_does_not_ignore_detector_regressions(tmp_path):
    capture(tmp_path, 'slow')
    with pytest.raises(AssertionError, match='healthy detector issues'):
        scenario.validate_live(tmp_path, 'healthy')


def test_cleanup_kills_descendant_after_its_parent_exits(tmp_path):
    processes = scenario.Processes(tmp_path)
    pid_file = tmp_path / 'descendant.pid'
    command = [sys.executable, '-c', '''
import os, signal, sys, time
child = os.fork()
if child:
    with open(sys.argv[1], 'w') as f:
        f.write(str(child))
    raise SystemExit(0)
signal.signal(signal.SIGINT, signal.SIG_IGN)
while True:
    time.sleep(1)
''', str(pid_file)]
    descendant = None
    try:
        parent = processes.start('parent', command)
        parent.wait(timeout=5)
        descendant = int(pid_file.read_text())
        processes.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            path = Path(f'/proc/{descendant}/stat')
            if not path.exists() or path.read_text().split()[2] == 'Z':
                break
            time.sleep(0.05)
        else:
            pytest.fail('descendant survived process-group cleanup')
    finally:
        processes.close()
        if descendant is not None:
            try:
                import os
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass
