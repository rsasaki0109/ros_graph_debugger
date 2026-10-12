"""Controlled public-bag replay benchmark; no production dependencies added.

Run from a sourced Jazzy checkout with --bag and a fresh --output. This measures replay and
observer cost, not the CPU or callback duration of the original Autoware stack.
"""
import argparse
from collections import Counter
import json
import hashlib
import os
from pathlib import Path
import random
import signal
import sqlite3
import sys
import time
import urllib.request

BAG = None
ODOM = '/localization/kinematic_state'
CLOUD = '/sensing/lidar/concatenated/pointcloud'
SELECTED = [ODOM, CLOUD, '/tf', '/tf_static']
START, END = 10.0, 20.0
PORT = 3961


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(path)


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))] if ordered else None


def receiver(directory, bag_start):
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from nav_msgs.msg import Odometry
    rclpy.init(args=[])
    node = Node('benchmark_receiver', parameter_overrides=[Parameter('use_sim_time', value=True)])
    rows = []
    def receive(message, info):
        stamp = message.header.stamp.sec * 1000000000 + message.header.stamp.nanosec
        rows.append({'stamp_ns': stamp, 'receipt_ns': (info or {}).get('received_timestamp', 0),
                     'wall': time.monotonic(), 'ros': node.get_clock().now().nanoseconds / 1e9})
    sub = node.create_subscription(Odometry, ODOM, receive,
        QoSProfile(depth=1000, reliability=ReliabilityPolicy.BEST_EFFORT))
    deadline = time.monotonic() + 65
    updated = 0
    try:
        save(directory / 'receiver-progress.json', {'ready': True, 'ros': 0, 'count': 0})
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.02)
            now = node.get_clock().now().nanoseconds / 1e9
            if time.monotonic() - updated >= 0.1:
                save(directory / 'receiver-progress.json', {'ready': True, 'ros': now, 'count': len(rows)})
                updated = time.monotonic()
            if now >= bag_start + END + 2:
                save(directory / 'receiver.json', rows)
                # Keep the group alive until the controller finishes sampling.
                while True:
                    rclpy.spin_once(node, timeout_sec=0.1)
        raise TimeoutError('receiver did not reach the measurement interval')
    finally:
        node.destroy_node()
        rclpy.shutdown()


def group_stats(groups):
    hz = os.sysconf('SC_CLK_TCK')
    page = os.sysconf('SC_PAGE_SIZE')
    stats = {name: {'cpu_seconds': 0, 'rss_bytes': 0, 'pids': []} for name in groups}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            pgrp = int(fields[2])
            for name, group in groups.items():
                if pgrp == group:
                    stats[name]['cpu_seconds'] += (int(fields[11]) + int(fields[12])) / hz
                    stats[name]['rss_bytes'] += int(fields[21]) * page
                    stats[name]['pids'].append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return stats


def network():
    # Interface counters omit Fast DDS shared memory; also include other users
    # and benchmark HTTP. Preserve them as scoped observations, never DDS totals.
    result = {}
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name, values = line.split(':')
        values = values.split()
        result[name.strip()] = {'rx_bytes': int(values[0]), 'tx_bytes': int(values[8])}
    return result


def snapshot():
    with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/v1/snapshot', timeout=0.5) as response:
        return json.load(response)


def source():
    from rclpy.serialization import deserialize_message
    from nav_msgs.msg import Odometry
    conn = sqlite3.connect('file:' + str(BAG / 'input_bag_0.db3') + '?mode=ro', uri=True)
    start = conn.execute('select min(timestamp) from messages').fetchone()[0] / 1e9
    wanted = Counter()
    sizes = {}
    for topic in SELECTED:
        count, size = conn.execute('select count(*),sum(length(data)) from messages where topic_id=(select id from topics where name=?) and timestamp>? and timestamp<=?',
                                  (topic, int((start + START) * 1e9), int((start + END) * 1e9))).fetchone()
        sizes[topic] = {'messages': count, 'serialized_bytes': size or 0}
    # Avoid ambiguous /clock boundary ticks. Match each original stamp and its
    # multiplicity against all received messages, including the trailing drain.
    for raw, in conn.execute('select data from messages where topic_id=(select id from topics where name=?) and timestamp>? and timestamp<=?',
                            (ODOM, int((start + START + 0.05) * 1e9), int((start + END - 0.05) * 1e9))):
        msg = deserialize_message(raw, Odometry)
        wanted[msg.header.stamp.sec * 1000000000 + msg.header.stamp.nanosec] += 1
    conn.close()
    return start, wanted, sizes


def run_case(directory, mode, start, wanted):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integration'))
    from run_scenario import Processes
    directory.mkdir(exist_ok=False)
    processes = Processes(directory)
    samples, snapshots = [], []
    commands = {}
    launched = time.monotonic()
    def launch(name, command):
        commands[name] = command
        return processes.start(name, command)
    try:
        launch('receiver', [sys.executable, __file__, '--receiver', str(directory), '--bag-start', str(start)])
        if mode != 'baseline':
            command = ['ros2', 'run', 'ros_graph_debugger', 'agent', '--no-browser',
                       '--profile', 'autoware', '--port', str(PORT)]
            if mode == 'no_probe':
                command += ['--no-probe']
            else:
                command += ['--probe-topic', ODOM]
                if mode == 'cloud':
                    command += ['--probe-topic', CLOUD]
            command += ['--ros-args', '-p', 'use_sim_time:=true']
            launch('agent', command)
        ready_deadline = time.monotonic() + 25
        while True:
            processes.check()
            try:
                progress = json.loads((directory / 'receiver-progress.json').read_text())
                assert progress['ready']
                if mode != 'baseline':
                    snapshot()
                break
            except (OSError, ValueError, AssertionError):
                if time.monotonic() > ready_deadline:
                    raise TimeoutError('receiver/agent startup')
                time.sleep(0.05)
        launch('player', ['ros2', 'bag', 'play', str(BAG), '--clock', '100', '--topics', *SELECTED])
        save(directory / 'commands.json', commands)
        deadline, next_snapshot = time.monotonic() + 40, time.monotonic()
        begin = finish = None
        probe_ready = mode in ('baseline', 'no_probe')
        while time.monotonic() < deadline:
            processes.check()
            progress = json.loads((directory / 'receiver-progress.json').read_text())
            offset = progress['ros'] - start
            if mode != 'baseline' and time.monotonic() >= next_snapshot:
                request_started = time.monotonic()
                snap = snapshot()
                snapshots.append({'wall': time.monotonic(), 'bag_offset': offset,
                                  'http_seconds': time.monotonic() - request_started,
                                  'snapshot': snap})
                topics = {t['name']: t for t in snap['topics']}
                required = [ODOM, CLOUD] if mode == 'cloud' else [ODOM]
                if mode != 'no_probe':
                    probe_ready = all(t in topics and topics[t]['probed'] and (topics[t]['rate_hz'] or 0) > 0 for t in required)
                # Keep a wall-clock cadence; response time must not accumulate
                # into a slower sampling period for busy probe conditions.
                next_snapshot += 1
            sample = {'wall': time.monotonic(), 'bag_offset': offset,
                      'groups': group_stats({n: p.pid for n, p in processes.children})}
            samples.append(sample)
            if begin is None and offset >= START:
                assert progress['count'] >= 10 and probe_ready, 'DDS/probes not ready at measurement start'
                begin = {**sample, 'interfaces': network()}
            if finish is None and offset >= END:
                finish = {**sample, 'interfaces': network()}
            if (directory / 'receiver.json').exists():
                break
            time.sleep(0.1)
        assert begin and finish and (directory / 'receiver.json').exists(), 'incomplete measurement'
        rows = json.loads((directory / 'receiver.json').read_text())
        interval = [r for r in rows if start + START <= r['ros'] < start + END]
        stamps = Counter(r['stamp_ns'] for r in rows)
        missing = wanted - stamps
        elapsed = finish['wall'] - begin['wall']
        resources = {}
        for name in begin['groups']:
            cpu = finish['groups'][name]['cpu_seconds'] - begin['groups'][name]['cpu_seconds']
            rss = [s['groups'][name]['rss_bytes'] for s in samples if START <= s['bag_offset'] <= END]
            resources[name] = {'cpu_seconds': cpu, 'cpu_percent_one_core': cpu / elapsed * 100,
                               'rss_median_bytes': percentile(rss, 0.5), 'rss_max_bytes': max(rss)}
        ages = [max(0, (r['ros'] - r['stamp_ns'] / 1e9) * 1000) for r in interval]
        signed_ages = [(r['ros'] - r['stamp_ns'] / 1e9) * 1000 for r in interval]
        receipts = sorted(r['receipt_ns'] for r in interval if r['receipt_ns'] > 0)
        assert len(receipts) >= 2, 'middleware receipt timestamps unavailable'
        rate = (len(receipts) - 1) * 1e9 / (receipts[-1] - receipts[0])
        window_rate = len(interval) / (END - START)
        agent_contract = []
        for sample in snapshots:
            if START <= sample['bag_offset'] < END:
                t = next(t for t in sample['snapshot']['topics'] if t['name'] == ODOM)
                if mode != 'no_probe':
                    agent_contract.append({'rate_hz': t['rate_hz'], 'age_p95_ms': t['header_age_p95_ms'],
                        'passed': t['rate_hz'] is not None and t['rate_hz'] >= 30 and t['header_age_p95_ms'] is not None and t['header_age_p95_ms'] <= 100,
                        'issues': [i for i in sample['snapshot']['issues'] if ODOM in i['related_topics']]})
        if mode in ('odom', 'cloud'):
            assert len(agent_contract) >= 8, 'insufficient agent contract observations'
        result = {'mode': mode, 'wall_elapsed_seconds': elapsed, 'bag_start_offset': begin['bag_offset'],
            'bag_end_offset': finish['bag_offset'], 'startup_wall_seconds': begin['wall'] - launched,
            'resources': resources, 'receiver_messages': len(interval), 'receiver_rate_hz': rate,
            'receiver_window_rate_hz': window_rate,
            'receiver_p95_age_ms': percentile(ages, 0.95), 'signed_age_min_ms': min(signed_ages),
            'signed_age_median_ms': percentile(signed_ages, 0.5),
            'source_interior_messages_expected': sum(wanted.values()), 'source_interior_messages_missing': sum(missing.values()),
            'receiver_contract_passed': rate >= 30 and window_rate >= 30 and percentile(ages, 0.95) <= 100 and not missing,
            'agent_contract_samples': agent_contract,
            'interface_deltas': {n: {k: finish['interfaces'][n][k] - v for k, v in values.items()}
                                 for n, values in begin['interfaces'].items()}}
        save(directory / 'result.json', result)
        print(json.dumps(result), flush=True)
        return result
    finally:
        try:
            save(directory / 'resource-samples.json', samples)
            save(directory / 'snapshots.json', snapshots)
        finally:
            processes.close()
        # Reap each group and verify no live grandchildren survived cleanup.
        live = group_stats({n: p.pid for n, p in processes.children})
        save(directory / 'cleanup.json', live)
        assert all(not s['pids'] for s in live.values()), live


def main():
    global BAG, PORT
    parser = argparse.ArgumentParser()
    parser.add_argument('--receiver', type=Path)
    parser.add_argument('--bag-start', type=float)
    parser.add_argument('--output', type=Path, default=Path.cwd() / 'observer-benchmark')
    parser.add_argument('--bag', type=Path)
    parser.add_argument('--source-metadata', type=Path)
    parser.add_argument('--domain', type=int, default=96)
    parser.add_argument('--port', type=int, default=3961)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--source-label', default='unknown (supply --source-label)')
    args = parser.parse_args()
    if args.receiver:
        receiver(args.receiver, args.bag_start)
        return
    if args.bag is None:
        parser.error('--bag is required for the benchmark')
    if args.repetitions < 1:
        parser.error('--repetitions must be positive')
    BAG = args.bag.resolve()
    PORT = args.port
    os.environ['ROS_DOMAIN_ID'] = str(args.domain)
    args.output = args.output.resolve()
    args.output.mkdir(exist_ok=False)
    start, wanted, sizes = source()
    rng = random.Random(20261012)
    results, order = [], []
    for repetition in range(args.repetitions):
        modes = ['baseline', 'no_probe', 'odom', 'cloud']
        rng.shuffle(modes)
        order.extend((repetition, mode) for mode in modes)
    save(args.output / 'protocol.json', {'source_commit': args.source_label,
        'source_file_sha256': {name: hashlib.sha256((Path.cwd() / 'ros_graph_debugger' / name).read_bytes()).hexdigest()
                               for name in ('agent.py', 'node.py', 'probe_executor.py')},
        'archive': json.loads(args.source_metadata.read_text()) if args.source_metadata else {'bag': str(BAG), 'upstream_checksum': None}, 'selected_topics': SELECTED,
        'bag_start': start, 'measurement_offsets': [START, END], 'order': order,
        'source_interval': sizes, 'domain': args.domain, 'port': PORT, 'python': sys.version,
        'cpu_affinity': sorted(os.sched_getaffinity(0)), 'rmw': __import__('rclpy.utilities', fromlist=['get_rmw_implementation_identifier']).get_rmw_implementation_identifier(),
        'rss_definition': 'sum of resident pages across process group, may double-count shared pages',
        'network_definition': 'interface deltas, omit DDS shared memory, include unrelated and polling traffic',
        'receiver_definition': 'same separate best-effort depth-1000 Odometry receiver in every arm'} )
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    signal.signal(signal.SIGINT, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    for repetition, mode in order:
        results.append(run_case(args.output / f'{repetition + 1}-{mode}', mode, start, wanted))
        save(args.output / 'results.json', results)
    assert all(r['receiver_contract_passed'] and all(s['passed'] for s in r['agent_contract_samples']) for r in results), '30 Hz / 100 ms or delivery contract failed; see results.json'
    print('All measurements, strict contracts and process-group cleanup passed.', flush=True)


if __name__ == '__main__':
    main()
