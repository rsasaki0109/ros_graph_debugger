"""Run unchanged Autoware components with the public DLR AR-tag input bag.

Standalone opt-in scenario: the public bag and upstream packages are required;
missing prerequisites are failures, never skips. No synthetic sensor inputs.
"""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
import urllib.request

from ros_graph_debugger.recording import read_recording
from run_scenario import Processes, cli, run

IMU = '/sensing/imu/tamagawa/imu_raw'
VELOCITY = '/vehicle/status/velocity_status'
CONVERTED = '/sensing/vehicle_velocity_converter/twist_with_covariance'
OUTPUT = '/localization/twist_estimator/twist_with_covariance'
GYRO = '/gyro_odometer'
CONVERTER = '/vehicle_velocity_converter'
DIAGNOSTIC = 'gyro_odometer: gyro_odometer_status'
BAG_SHA256 = '224683a07c61e21a0ae416d9fec80e558cffde76534f340b96492f6772a28797'
MIN_HZ = 20  # The unchanged public inputs are independently measured at 30 Hz.


def audit_bag(bag):
    files = list(bag.glob('*.db3'))
    assert len(files) == 1, files
    with files[0].open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    assert digest == BAG_SHA256, 'requires the documented, unchanged public DLR input bag'
    with closing(sqlite3.connect(f'file:{files[0]}?mode=ro', uri=True)) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        counts = dict(db.execute('SELECT t.name, COUNT(*) FROM messages m JOIN topics t '
                                 'ON t.id=m.topic_id GROUP BY t.name'))
        assert counts[IMU] == counts[VELOCITY] == 1735 and counts['/tf_static'] == 2, counts
        first, last = db.execute('SELECT MIN(timestamp), MAX(timestamp) FROM messages').fetchone()
    return {'sqlite_sha256': digest, 'message_counts': counts,
            'first_receive_ns': first, 'last_receive_ns': last}


def validate_snapshot(sample, phase):
    nodes = {n['id'] for n in sample['nodes']}
    assert {GYRO, CONVERTER} <= nodes, nodes
    topics = {t['name']: t for t in sample['topics']}
    assert CONVERTER in topics[CONVERTED]['publishers'], topics[CONVERTED]
    assert GYRO in topics[CONVERTED]['subscribers'], topics[CONVERTED]
    assert GYRO in topics[OUTPUT]['publishers'], topics[OUTPUT]
    assert GYRO in topics[IMU]['subscribers'], topics[IMU]
    # This recorded calibration is sufficient for gyro's frame conversion;
    # the standard listener also subscribes to unused dynamic /tf.
    static_edges = {(e['parent'], e['child']) for e in sample['tf_edges']
                    if e['static'] is True and e['status'] == 'ok'}
    assert {('base_link', 'sensor_kit_base_link'),
            ('sensor_kit_base_link', 'tamagawa/imu_link')} <= static_edges, static_edges
    converted = topics[CONVERTED]
    assert converted['probed'] and converted['rate_hz'] is not None \
        and converted['rate_hz'] >= MIN_HZ, converted
    diagnostic = next(d for d in sample['diagnostics'] if d['name'] == DIAGNOSTIC)
    issues = sample['issues']
    if phase == 'missing-imu':
        assert not topics[IMU]['publishers'], topics[IMU]
        assert diagnostic['level'] == 2 and 'IMU msg is timeout' in diagnostic['message'], diagnostic
        assert any(i['kind'] == 'diagnostics_error'
                   and i.get('related_diagnostics') == [DIAGNOSTIC]
                   and i['related_nodes'] == []
                   and 'node attribution: unknown' in i['evidence']
                   and 'level: 2' in i['evidence'] for i in issues), issues
        assert any(i['kind'] == 'no_publisher' and IMU in i['related_topics']
                   and GYRO in i['related_nodes'] and 'publishers: 0' in i['evidence']
                   for i in issues), issues
        assert any(i['kind'] == 'topic_stale' and OUTPUT in i['related_topics']
                   and GYRO in i['related_nodes'] for i in issues), issues
    else:
        assert diagnostic['level'] == 0, diagnostic
        assert topics[IMU]['publishers'], topics[IMU]
        for name in (IMU, OUTPUT):
            assert topics[name]['probed'] and topics[name]['rate_hz'] is not None \
                and topics[name]['rate_hz'] >= MIN_HZ, topics[name]
        forbidden = {'diagnostics_error', 'diagnostics_warn', 'no_publisher',
                     'topic_stale', 'qos_mismatch', 'rate_drop', 'bottleneck'}
        unexpected = []
        for issue in issues:
            static_only_listener = (
                issue['kind'] == 'no_publisher' and issue.get('related_topics') == ['/tf']
                and issue.get('related_nodes')
                and all(n.startswith('/transform_listener_impl_') for n in issue['related_nodes'])
                and 'publishers: 0' in issue.get('evidence', []))
            if issue['kind'] in forbidden and not static_only_listener:
                unexpected.append(issue)
        assert not unexpected, unexpected
    return sample


def validate_recording(directory, phase):
    result = json.loads((directory / 'assert.json').read_text())
    assert result['passed'] == (phase != 'missing-imu') and result['samples'] >= 5, result
    assert result['max_breach'] == 0, result
    _, samples = read_recording(str(directory / 'run.rgd.json'))
    settled = [s for s in samples if s['timestamp'] - samples[0]['timestamp'] >= 1]
    assert len(settled) == result['samples'], result
    for sample in settled:
        validate_snapshot(sample, phase)


def scenario(bag, output, port):
    # Keep the validators importable without ROS.
    import rclpy
    from diagnostic_msgs.msg import DiagnosticArray
    from geometry_msgs.msg import TwistWithCovarianceStamped
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rosgraph_msgs.msg import Clock
    from rosbag2_interfaces.srv import Resume

    audit = audit_bag(bag)
    (output / 'input-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
    # The bag records no offered QoS. Player defaults are volatile, while
    # standard TF listeners require transient-local static transforms.
    # This is an explicit replay policy, not a claim about the original publisher.
    qos = output / 'replay-qos.yaml'
    qos.write_text('/tf_static:\n  reliability: reliable\n  durability: transient_local\n'
                   '  history: keep_all\n')
    processes = Processes(output)
    rclpy.init()
    driver = Node('autoware_public_scenario_driver')
    executor = SingleThreadedExecutor()
    executor.add_node(driver)
    receipt_lock = threading.Lock()
    receipts = []
    clock = [None]
    callback_errors = []
    receipt_file = (output / 'receipts.ndjson').open('w')

    def receive(msg):
        stamp = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
        values = {'stamp_ns': stamp, 'frame_id': msg.header.frame_id,
                  'linear_x': msg.twist.twist.linear.x, 'angular_z': msg.twist.twist.angular.z,
                  'received_monotonic': time.monotonic()}
        with receipt_lock:
            receipts.append(values)
            receipt_file.write(json.dumps(values) + '\n')
            receipt_file.flush()

    def diagnostic(msg):
        with (output / 'raw-diagnostics.ndjson').open('a') as raw:
            for status in msg.status:
                if status.name == DIAGNOSTIC:
                    raw.write(json.dumps({'stamp_ns': msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec,
                                          'name': status.name,
                                          'level': status.level[0] if isinstance(status.level, bytes) else status.level,
                                          'message': status.message,
                                          'values': {v.key: v.value for v in status.values}}) + '\n')

    driver.create_subscription(TwistWithCovarianceStamped, OUTPUT, receive, 10)
    driver.create_subscription(DiagnosticArray, '/diagnostics', diagnostic, 10)

    def on_clock(msg):
        clock[0] = msg.clock.sec * 10**9 + msg.clock.nanosec

    driver.create_subscription(Clock, '/clock', on_clock,
                               QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT))
    resume = driver.create_client(Resume, '/rosbag2_player/resume')

    def spin():
        try:
            executor.spin()
        except Exception as exc:
            callback_errors.append(repr(exc))

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{port}'

    def poll(check, description, timeout=30):
        deadline = time.monotonic() + timeout
        last = 'not ready'
        while time.monotonic() < deadline:
            processes.check()
            assert thread.is_alive() and not callback_errors, callback_errors
            try:
                return check()
            except (AssertionError, OSError, KeyError, ValueError, StopIteration) as exc:
                last = repr(exc)
            time.sleep(0.1)
        raise AssertionError(f'{description}: {last}; see artifacts')

    def current(phase):
        with urllib.request.urlopen(base + '/api/v1/snapshot', timeout=2) as response:
            sample = json.load(response)
        (output / 'last-snapshot.json').write_text(json.dumps(sample, indent=2))
        return validate_snapshot(sample, phase)

    player = None
    try:
        processes.start('converter', ['ros2', 'launch', 'autoware_vehicle_velocity_converter',
                                      'vehicle_velocity_converter.launch.xml',
                                      'input_vehicle_velocity_topic:=' + VELOCITY,
                                      'output_twist_with_covariance:=' + CONVERTED])
        processes.start('gyro', ['ros2', 'run', 'autoware_gyro_odometer', 'autoware_gyro_odometer_node',
                                 '--ros-args', '-p', 'use_sim_time:=true', '-p', 'output_frame:=base_link',
                                 '-p', 'message_timeout_sec:=0.2',
                                 '-r', 'vehicle/twist_with_covariance:=' + CONVERTED,
                                 '-r', 'imu:=' + IMU, '-r', 'twist_with_covariance:=' + OUTPUT])
        processes.start('agent', ['ros2', 'run', 'ros_graph_debugger', 'agent', '--no-browser',
                                  '--profile', 'autoware', '--port', str(port),
                                  '--probe-regex', '^(?:' + '|'.join((IMU, CONVERTED, OUTPUT)) + ')$',
                                  '--expect', OUTPUT + '=' + str(MIN_HZ),
                                  '--ros-args', '-p', 'use_sim_time:=true'])
        summary = {}
        start_offset = 0.0
        for phase in ('healthy', 'missing-imu', 'recovered'):
            directory = output / phase
            directory.mkdir()
            topics = [VELOCITY, '/tf_static'] + ([] if phase == 'missing-imu' else [IMU])
            player = processes.start('player-' + phase,
                                     ['ros2', 'bag', 'play', str(bag), '--clock', '50',
                                      '--start-offset', str(start_offset),
                                      '--start-paused', '--disable-keyboard-controls',
                                      '--qos-profile-overrides-path', str(qos), '--topics', *topics])

            def discovered():
                assert resume.service_is_ready(), 'player resume service'
                assert driver.count_publishers(OUTPUT) == 1, 'gyro publisher'
                assert driver.count_subscribers(VELOCITY) >= 1, 'converter subscription'
                assert driver.count_subscribers(CONVERTED) >= 2, 'gyro and agent subscriptions'
                assert driver.count_subscribers('/clock') >= 2, 'gyro and agent clocks'
                assert driver.count_subscribers(IMU) >= 2, 'gyro and agent IMU subscriptions'

            poll(discovered, 'paused playback discovery')
            future = resume.call_async(Resume.Request())

            def resumed():
                assert future.done(), 'resume response'
                assert future.exception() is None, future.exception()
                assert future.result() is not None, 'missing resume response'

            poll(resumed, 'resume playback')
            poll(lambda: current(phase), phase + ' diagnosis')
            with receipt_lock:
                before = len(receipts)
            args = cli('assert', base, '--duration', '7', '--settle', '1', '--fail-on', 'never',
                       '--expect', OUTPUT + '=' + str(MIN_HZ), '--require-node', GYRO,
                       '--require-node', CONVERTER, '--forbid', 'diagnostics_error',
                       '--forbid', 'diagnostics_warn',
                       '--forbid', 'topic_stale', '--forbid', 'qos_mismatch',
                       '--json', '--record', directory / 'run.rgd.json')
            run(args, directory, 'assert', expected=1 if phase == 'missing-imu' else 0)
            processes.check()
            assert thread.is_alive() and not callback_errors, callback_errors
            validate_recording(directory, phase)
            with receipt_lock:
                delivered = list(receipts[before:])
            expected_count = len(delivered) == 0 if phase == 'missing-imu' else len(delivered) >= 100
            assert expected_count, {'phase': phase, 'receipts': len(delivered)}
            assert all(m['frame_id'] == 'base_link' for m in delivered), 'unexpected output frame'
            summary[phase] = {'assert_exit': 1 if phase == 'missing-imu' else 0,
                              'output_receipts_during_assert': len(delivered),
                              'playback_start_offset_s': start_offset}
            run(cli('report', directory / 'run.rgd.json', '--md', directory / 'report.md',
                    '--path', GYRO), directory, 'report')
            processes.stop(player)
            player = None
            assert clock[0] is not None, 'no replay clock received'
            start_offset = (clock[0] - audit['first_receive_ns']) / 1e9 + 0.1
            assert 0 < start_offset < (audit['last_receive_ns'] - audit['first_receive_ns']) / 1e9

            def disappeared():
                assert not resume.service_is_ready(), 'previous player service still discovered'
                assert driver.count_publishers(VELOCITY) == 0, 'previous player still discovered'

            poll(disappeared, 'previous player shutdown')
        run(cli('diff', output / 'missing-imu/run.rgd.json', output / 'recovered/run.rgd.json',
                '--json', '--fail-on-regression'), output, 'recovery-diff')
        diff = json.loads((output / 'recovery-diff.json').read_text())
        assert diff['verdict'] == 'improved' and not diff['new_issues'], diff
        assert {'diagnostics_error', 'no_publisher', 'topic_stale'} <= {
            i['kind'] for i in diff['resolved_issues']}, diff
        processes.check()
        assert thread.is_alive() and not callback_errors, callback_errors
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary), flush=True)
    finally:
        processes.close()
        executor.shutdown(timeout_sec=5)
        thread.join(timeout=5)
        driver.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        receipt_file.close()
        assert not thread.is_alive(), 'observer thread did not stop'
        for phase in ('healthy', 'missing-imu', 'recovered'):
            directory = output / phase
            recording = directory / 'run.rgd.json'
            if recording.exists() and not (directory / 'report.md').exists():
                try:
                    run(cli('report', recording, '--md', directory / 'report.md', '--path', GYRO),
                        directory, 'partial-report', timeout=15)
                except Exception as exc:
                    (directory / 'partial-report-error.txt').write_text(repr(exc) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bag', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help='fresh artifact directory')
    parser.add_argument('--port', type=int, default=3975)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / 'executed-scenario.py').write_bytes(Path(__file__).read_bytes())
    os.environ['ROS_DOMAIN_ID'] = '97'

    def terminate(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    def timeout(_signum, _frame):
        raise TimeoutError('Autoware scenario exceeded its 180-second budget')

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 180)
    try:
        scenario(args.bag.resolve(), output, args.port)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
