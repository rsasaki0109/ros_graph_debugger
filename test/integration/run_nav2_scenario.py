"""Diagnose and recover a real Nav2 map_server, including latched-map bags."""

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time
import urllib.request

from ros_graph_debugger.recording import read_recording
from run_scenario import Processes, cli, run

EXPECTED_MAP = {
    'frame_id': 'map', 'width': 4, 'height': 3, 'resolution': 0.5,
    'origin': [1.0, -2.0, 0.0], 'orientation': [0.0, 0.0, 0.0, 1.0],
    'data': [0, 0, 100, -1, 0, 100, -1, 0, 100, 0, -1, 100],
}


def poll(check, processes, description, timeout=30):
    deadline = time.monotonic() + timeout
    last = 'not ready'
    while time.monotonic() < deadline:
        processes.check()
        try:
            return check()
        except (AssertionError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
            last = str(exc)
        time.sleep(0.1)
    raise AssertionError(f'{description}: {last}; see artifacts')


def snapshot(base, directory, state):
    with urllib.request.urlopen(base + '/api/v1/snapshot', timeout=2) as response:
        result = json.load(response)
    (directory / 'last-snapshot.json').write_text(json.dumps(result, indent=2))
    nodes = {n['id']: n for n in result['nodes']}
    assert nodes['/map_server']['lifecycle_state'] == state, nodes['/map_server']
    assert '/nav2_map_observer' in nodes
    topic = next((t for t in result['topics'] if t['name'] == '/map'), None)
    assert topic is not None, '/map not discovered yet'
    assert '/map_server' in topic['publishers'], topic
    assert '/nav2_map_observer' in topic['subscribers'], topic
    issues = [i for i in result['issues'] if i['kind'] == 'lifecycle_not_active'
              and '/map_server' in i['related_nodes']]
    if state == 'inactive':
        assert any('/map' in i['related_topics'] and i['severity'] == 'critical'
                   and 'lifecycle state: inactive' in i['evidence']
                   and any('/map:' in e and 'waiting' in e for e in i['evidence'])
                   for i in issues), issues
    else:
        assert not issues, issues
    return result


def validate_recording(directory, state):
    result = json.loads((directory / 'assert.json').read_text())
    assert result['samples'] >= 3 and result['passed'] == (state == 'active'), result
    assert result['max_breach'] == 0
    checks = {c['name']: c for c in result['checks']}
    assert checks['node /map_server present']['breaches'] == 0
    assert checks['topic /map present']['breaches'] == 0
    assert checks['no lifecycle_not_active issues']['breaches'] == (
        result['samples'] if state == 'inactive' else 0)
    _, samples = read_recording(str(directory / 'run.rgd.json'))
    settled = [s for s in samples if s['timestamp'] - samples[0]['timestamp'] >= 1]
    assert len(settled) == result['samples']
    for sample in settled:
        node = next(n for n in sample['nodes'] if n['id'] == '/map_server')
        assert node['lifecycle_state'] == state, node
        topic = next(t for t in sample['topics'] if t['name'] == '/map')
        assert '/map_server' in topic['publishers'] and '/nav2_map_observer' in topic['subscribers']
        issues = [i for i in sample['issues'] if i['kind'] == 'lifecycle_not_active'
                  and '/map_server' in i['related_nodes']]
        if state == 'inactive':
            assert any(i['severity'] == 'critical' and '/map' in i['related_topics']
                       and 'lifecycle state: inactive' in i['evidence']
                       and any('/map:' in e and 'waiting' in e for e in i['evidence'])
                       for i in issues), issues
        else:
            assert not issues, issues


def validate_diff(directory):
    result = json.loads((directory / 'diff.json').read_text())
    assert result['verdict'] == 'improved', result
    assert not result['new_issues'], result
    assert any(i['kind'] == 'lifecycle_not_active' and '/map_server' in i['related_nodes']
               and '/map' in i['related_topics'] for i in result['resolved_issues']), result


def receipt_ready(path):
    result = json.loads(path.read_text())
    assert result['count'] == 1, result
    assert result['map'] == EXPECTED_MAP, result
    return result


def bag_ready(bag):
    files = list(bag.glob('*.db3'))
    assert len(files) == 1, files
    with sqlite3.connect(f'file:{files[0]}?mode=ro', uri=True) as db:
        counts = dict(db.execute('SELECT topics.name, COUNT(*) FROM messages JOIN topics '
                                 'ON topics.id = messages.topic_id GROUP BY topics.name'))
        span = db.execute('SELECT (MAX(timestamp)-MIN(timestamp))/1e9 FROM messages').fetchone()[0]
    assert counts.get('/map') == 1, counts
    assert counts.get('/integration/nav2/heartbeat', 0) >= 40 and span >= 4, (counts, span)
    return counts


def validate_bag(directory):
    import rosbag2_py
    from nav_msgs.msg import OccupancyGrid
    from rclpy.serialization import deserialize_message
    from nav2_map_observer import map_fields

    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(directory / 'bag'), storage_id='sqlite3'),
                rosbag2_py.ConverterOptions('', ''))
    maps = []
    while reader.has_next():
        name, raw, _ = reader.read_next()
        if name == '/map':
            maps.append(map_fields(deserialize_message(raw, OccupancyGrid)))
    assert maps == [EXPECTED_MAP], maps
    _, snapshots = read_recording(str(directory / 'bag.rgd.json'))
    assert len(snapshots) >= 3, snapshots
    for sample in snapshots:
        topic = next(t for t in sample['topics'] if t['name'] == '/map')
        assert topic['type'] == 'nav_msgs/msg/OccupancyGrid'
        assert not any('/map' in i['related_topics'] and i['kind'] in ('rate_drop', 'stale_topic')
                       for i in sample['issues']), sample['issues']
    result = json.loads((directory / 'bag-assert.json').read_text())
    assert result['passed'] and result['samples'] >= 3, result


def scenario(output, port):
    # ROS-only imports are deferred so evidence validation can run without ROS.
    import rclpy
    from lifecycle_msgs.msg import State, Transition
    from lifecycle_msgs.srv import ChangeState, GetState
    from rclpy.node import Node

    processes = Processes(output)
    rclpy.init()
    driver = Node('nav2_scenario_driver')
    state_client = driver.create_client(GetState, '/map_server/get_state')
    transition_client = driver.create_client(ChangeState, '/map_server/change_state')
    base = f'http://127.0.0.1:{port}'
    (output / 'map.pgm').write_text('P2\n4 3\n255\n0 255 128 0\n255 0 128 255\n255 255 0 128\n')
    (output / 'map.yaml').write_text('image: map.pgm\nmode: trinary\nresolution: 0.5\n'
                                   'origin: [1.0, -2.0, 0.0]\nnegate: 0\n'
                                   'occupied_thresh: 0.65\nfree_thresh: 0.196\n')

    def call(client, request):
        deadline = time.monotonic() + 10
        while not client.service_is_ready():
            processes.check()
            assert time.monotonic() < deadline, client.srv_name + ' discovery timed out'
            rclpy.spin_once(driver, timeout_sec=0.1)
        future = client.call_async(request)
        while not future.done():
            processes.check()
            assert time.monotonic() < deadline, client.srv_name + ' response timed out'
            rclpy.spin_once(driver, timeout_sec=0.1)
        return future.result()

    def transition(target, expected):
        request = ChangeState.Request()
        request.transition.id = target
        assert call(transition_client, request).success
        assert call(state_client, GetState.Request()).current_state.id == expected

    try:
        processes.start('map-server', ['ros2', 'run', 'nav2_map_server', 'map_server',
                                      '--ros-args', '-p', 'yaml_filename:=' + str(output / 'map.yaml')])
        processes.start('observer', [sys.executable, str(Path(__file__).with_name('nav2_map_observer.py')),
                                     '--receipt', str(output / 'map-receipt.json')])
        processes.start('agent', ['ros2', 'run', 'ros_graph_debugger', 'agent',
                                 '--no-browser', '--no-probe', '--profile', 'nav2', '--port', str(port)])
        transition(Transition.TRANSITION_CONFIGURE, State.PRIMARY_STATE_INACTIVE)
        poll(lambda: snapshot(base, output, 'inactive'), processes, 'inactive Nav2 diagnosis')
        for phase, state in (('inactive', 'inactive'), ('recovered', 'active')):
            directory = output / phase
            directory.mkdir()
            if state == 'active':
                transition(Transition.TRANSITION_ACTIVATE, State.PRIMARY_STATE_ACTIVE)
                poll(lambda: receipt_ready(output / 'map-receipt.json'), processes, 'actual map delivery')
                poll(lambda: snapshot(base, output, 'active'), processes, 'active Nav2 diagnosis')
            else:
                assert not (output / 'map-receipt.json').exists(), 'inactive server delivered a map'
            run(cli('assert', base, '--duration', '6', '--settle', '1', '--fail-on', 'never',
                    '--forbid', 'lifecycle_not_active', '--require-node', '/map_server',
                    '--require-topic', '/map', '--json', '--record', directory / 'run.rgd.json'),
                directory, 'assert', expected=1 if state == 'inactive' else 0)
            validate_recording(directory, state)
            if state == 'inactive':
                assert not (output / 'map-receipt.json').exists(), 'inactive server delivered a map'
            run(cli('report', directory / 'run.rgd.json', '--md', directory / 'report.md',
                    '--html', directory / 'report.html', '--path', '/map_server'), directory, 'report')
        run(cli('diff', output / 'inactive/run.rgd.json', output / 'recovered/run.rgd.json',
                '--json', '--fail-on-regression'), output, 'diff')
        validate_diff(output)
        qos = output / 'bag-qos.yaml'
        qos.write_text('/map:\n  reliability: reliable\n  durability: transient_local\n'
                       '  history: keep_last\n  depth: 1\n')
        # Start after activation: the single map must arrive via transient-local history.
        recorder = processes.start('bag-record', ['ros2', 'bag', 'record', '-s', 'sqlite3',
                                                '--max-cache-size', '0', '--qos-profile-overrides-path',
                                                str(qos), '-o', str(output / 'bag'), '/map',
                                                '/integration/nav2/heartbeat'])
        poll(lambda: bag_ready(output / 'bag'), processes, 'latched map and heartbeat bag')
        processes.stop(recorder)
        run(cli('bag', 'analyze', output / 'bag', '--profile', 'nav2',
                '--out', output / 'bag.rgd.json'), output, 'bag-analyze')
        run(cli('assert', output / 'bag.rgd.json', '--settle', '0', '--fail-on', 'never',
                '--forbid', 'stale_topic', '--forbid', 'rate_drop', '--require-topic', '/map', '--json'),
            output, 'bag-assert')
        validate_bag(output)
        receipt_ready(output / 'map-receipt.json')
        processes.check()
        print('Nav2: inactive diagnosis, activation, actual map, regression diff and latched bag passed',
              flush=True)
    finally:
        processes.close()
        driver.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        for phase in ('inactive', 'recovered'):
            directory = output / phase
            recording = directory / 'run.rgd.json'
            if recording.exists() and not (directory / 'report.md').exists():
                try:
                    run(cli('report', recording, '--md', directory / 'report.md',
                            '--html', directory / 'report.html', '--path', '/map_server'),
                        directory, 'failure-report', timeout=10)
                except Exception as exc:
                    print(f'Could not render partial recording: {exc}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='fresh artifact directory')
    parser.add_argument('--port', type=int, default=3952)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ['ROS_DOMAIN_ID'] = '93'

    def terminate(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    def timeout(_signum, _frame):
        raise TimeoutError('Nav2 scenario exceeded its 120-second budget')

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 120)
    try:
        scenario(output, args.port)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
