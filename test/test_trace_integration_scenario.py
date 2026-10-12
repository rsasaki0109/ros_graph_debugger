"""Reject lost provenance, incomplete callback coverage and incorrect diagnoses."""

import copy
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from ros_graph_debugger.recording import make_header, write_recording
from ros_graph_debugger.tracing import aggregate_callback_durations

integration = Path(__file__).parent / 'integration'
sys.path.insert(0, str(integration))
try:
    spec = importlib.util.spec_from_file_location('trace_scenario', integration / 'run_trace_scenario.py')
    scenario = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scenario)
finally:
    sys.path.pop(0)


def conversion(tmp_path, monkeypatch, mode='slow'):
    node = '/trace_' + mode
    rows = [{'node': node, 'callback': cb, 'topic': topic,
             'duration_ms': 150.0 if mode == 'slow' else 1.0, 'source': 'trace'}
            for cb, topic in ((f'sub {node}/input', node + '/input'),
                              ('timer 200 ms', ''), (f'srv {node}/reset', ''))
            for _ in range(scenario.MIN_INVOCATIONS)]
    counts = {'attributed': len(rows), 'unattributed': 0}
    event = {'name': 'ros2:rcl_node_init', 'pid': 123, 'fields': {'node_name': 'trace_' + mode}}
    monkeypatch.setattr(scenario, 'read_ctf_events', lambda _: iter([event]))
    monkeypatch.setattr(scenario, 'convert_trace', lambda _: (rows, counts))
    (tmp_path / 'callbacks.ndjson').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return rows, counts, event


@pytest.mark.parametrize('failure', ['missing_owner', 'unattributed', 'missing_service',
                                     'cli_mismatch', 'missing_trace_source'])
def test_conversion_rejects_missing_real_trace_evidence(tmp_path, monkeypatch, failure):
    rows, counts, event = conversion(tmp_path, monkeypatch)
    if failure == 'missing_owner':
        event['pid'] = 456
    elif failure == 'unattributed':
        counts['unattributed'] = 1
    elif failure == 'missing_service':
        rows[:] = [r for r in rows if not r['callback'].startswith('srv ')]
        (tmp_path / 'callbacks.ndjson').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    elif failure == 'missing_trace_source':
        for row in rows:
            del row['source']
        (tmp_path / 'callbacks.ndjson').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    else:
        (tmp_path / 'callbacks.ndjson').write_text('')
    with pytest.raises(AssertionError):
        scenario.validate_conversion(tmp_path, 'slow', 123)


@pytest.mark.parametrize('mode', ['slow', 'healthy'])
def test_all_three_real_callback_contracts(tmp_path, monkeypatch, mode):
    conversion(tmp_path, monkeypatch, mode)
    stats = scenario.validate_conversion(tmp_path, mode, 123)
    node = '/trace_' + mode
    issues = [{'kind': 'slow_callback', 'related_nodes': [node],
               'related_topics': [c.topic] if c.topic else [],
               'evidence': [f'p95: {c.p95_ms:.0f} ms (limit 100 ms)', f'samples: {c.count}',
                            'duration source: paired callback trace']}
              for c in stats] if mode == 'slow' else []
    snapshots = [{'timestamp': float(t), 'callbacks': [asdict(c) for c in stats],
                  'issues': copy.deepcopy(issues)} for t in range(4)]
    result = {'passed': mode == 'healthy', 'samples': 3, 'checks': [
        {'name': 'no slow_callback issues', 'breaches': 3 if mode == 'slow' else 0},
        {'name': f'node {node} present', 'breaches': 0}]}
    (tmp_path / 'assert.json').write_text(json.dumps(result))
    write_recording(str(tmp_path / 'run.rgd.json'), make_header(0, 1), snapshots)
    scenario.validate_diagnosis(tmp_path, mode, stats)
    # A successful CLI status cannot hide incomplete or wrongly attributed data.
    snapshots[1]['callbacks'][0]['node'] = '/wrong_node'
    write_recording(str(tmp_path / 'run.rgd.json'), make_header(0, 1), snapshots)
    with pytest.raises(AssertionError):
        scenario.validate_diagnosis(tmp_path, mode, stats)


def test_healthy_trace_rejects_false_positive(tmp_path):
    node = '/trace_healthy'
    stats = aggregate_callback_durations([{'node': node, 'duration_ms': 1.0}])
    snapshots = [{'timestamp': float(t), 'callbacks': [asdict(c) for c in stats],
                  'issues': [{'kind': 'slow_callback', 'related_nodes': [node]}]}
                 for t in range(4)]
    (tmp_path / 'assert.json').write_text(json.dumps({
        'passed': True, 'samples': 3, 'checks': [
            {'name': 'no slow_callback issues', 'breaches': 0},
            {'name': f'node {node} present', 'breaches': 0}]}))
    write_recording(str(tmp_path / 'run.rgd.json'), make_header(0, 1), snapshots)
    with pytest.raises(AssertionError):
        scenario.validate_diagnosis(tmp_path, 'healthy', stats)
