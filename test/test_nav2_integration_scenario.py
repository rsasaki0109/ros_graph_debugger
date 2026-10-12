"""The real Nav2 runner must require lifecycle and data-delivery evidence."""

import copy
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sys
import sqlite3

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


def test_bag_analysis_cannot_hide_actual_topic_stale_kind(tmp_path):
    samples = [{'timestamp': float(t), 'issues': [],
                'topics': [{'name': '/map', 'type': 'nav_msgs/msg/OccupancyGrid'}]}
               for t in range(3)]
    (tmp_path / 'bag-assert.json').write_text(json.dumps({'passed': True, 'samples': 3}))
    write_recording(str(tmp_path / 'bag.rgd.json'), make_header(0, 1), samples)
    scenario.validate_bag_recording(tmp_path)
    samples[1]['issues'] = [{'kind': 'topic_stale', 'related_topics': ['/map']}]
    write_recording(str(tmp_path / 'bag.rgd.json'), make_header(0, 1), samples)
    with pytest.raises(AssertionError):
        scenario.validate_bag_recording(tmp_path)


@pytest.mark.parametrize('journal_mode', ['WAL', 'DELETE'])
def test_live_bag_polling_preserves_writes_and_read_snapshot(tmp_path, journal_mode):
    path = tmp_path / 'bag_0.db3'
    with closing(sqlite3.connect(path, timeout=0)) as writer:
        writer.execute(f'PRAGMA journal_mode={journal_mode}')
        writer.executescript('CREATE TABLE topics(id INTEGER PRIMARY KEY, name TEXT);'
                             'CREATE TABLE messages(topic_id INTEGER, timestamp INTEGER);'
                             "INSERT INTO topics VALUES(1, '/map'),"
                             "(2, '/integration/nav2/heartbeat');"
                             'INSERT INTO messages VALUES(1, 0);')
        writer.executemany('INSERT INTO messages VALUES(2, ?)',
                           [(i * 100_000_000,) for i in range(1, 41)])
        writer.commit()
        # Hold a real reader transaction while the timeout-free writer commits.
        # A rollback journal would block this commit; WAL preserves both views.
        with closing(sqlite3.connect(path)) as reader:
            reader.execute('BEGIN')
            assert reader.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 41
            writer.execute('INSERT INTO messages VALUES(2, 4100000000)')
            if journal_mode == 'DELETE':
                with pytest.raises(sqlite3.OperationalError, match='database is locked'):
                    writer.commit()
                writer.rollback()
                # Even a bag satisfying every count/span requirement is unsafe
                # to poll live with a rollback journal.
                with pytest.raises(AssertionError, match='requires WAL'):
                    scenario.bag_ready(tmp_path)
                return
            writer.commit()
            assert reader.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 41
            assert scenario.bag_ready(tmp_path) == {
                '/map': 1, '/integration/nav2/heartbeat': 41}
            # Successful readiness must never hide an additional map.
            writer.execute('INSERT INTO messages VALUES(1, 4200000000)')
            writer.commit()
            with pytest.raises(AssertionError):
                scenario.bag_ready(tmp_path)
