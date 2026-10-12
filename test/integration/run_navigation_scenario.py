"""Real Jazzy Nav2 navigation, stopped DDS, recovery, bag and LTTng evidence."""

import argparse
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import urllib.request
import uuid

from ros_graph_debugger.recording import read_recording
from run_scenario import Processes, cli, run

RATES = {'/scan': 5, '/odom': 20, '/cmd_vel': 10}
MANAGED = ('/map_server', '/planner_server', '/controller_server',
           '/global_costmap/global_costmap', '/local_costmap/local_costmap')
CHAIN = (('map', 'odom'), ('odom', 'base_footprint'))
FORBID = ('tf_stale', 'topic_stale', 'rate_drop', 'bottleneck', 'stale_data',
          'lifecycle_not_active', 'qos_mismatch', 'no_service_server', 'no_action_server')


def poll(check, processes, description, timeout=30):
    deadline, last = time.monotonic() + timeout, 'not ready'
    while time.monotonic() < deadline:
        processes.check()
        try:
            return check()
        except (AssertionError, OSError, ValueError, KeyError) as exc:
            last = str(exc)
        time.sleep(0.1)
    raise AssertionError(f'{description}: {last}; see artifacts')


def validate_snapshot(snapshot, paused):
    assert snapshot['profile'] == 'nav2'
    nodes = {n['id']: n for n in snapshot['nodes']}
    assert '/loopback_simulator' in nodes
    for name in MANAGED:
        assert nodes[name]['lifecycle_state'] == 'active', nodes[name]
    topics = {t['name']: t for t in snapshot['topics']}
    for name in RATES:
        topic = topics[name]
        assert topic['probed'] and topic['publisher_count'] > 0
        publisher = '/controller_server' if name == '/cmd_vel' else '/loopback_simulator'
        assert publisher in topic['publishers'], topic
        assert topic['last_seen_time'] is not None
        if not paused:
            assert topic['rate_hz'] is not None and topic['rate_hz'] >= RATES[name], topic
            assert topic['age_ms'] <= 2000, topic
    edges = {(e['parent'], e['child']): e for e in snapshot['tf_edges']}
    for frames in CHAIN:
        edge = edges[frames]
        assert not edge['static'] and edge['age_ms'] is not None
        assert (edge['status'] == 'critical') == paused, edge
        assert (edge['age_ms'] > 1000) == paused, edge
    issues = snapshot['issues']
    if not paused:
        assert not [i for i in issues if i['kind'] in FORBID], issues
    else:
        for frames in CHAIN:
            assert any(i['kind'] == 'tf_stale' and i['severity'] == 'critical'
                       and i['related_frames'] == list(frames)
                       and any(e.startswith('age: ') for e in i['evidence']) for i in issues), frames
        for name in ('/scan', '/odom'):
            assert topics[name]['age_ms'] > 2000, topics[name]
            assert any(i['kind'] == 'topic_stale' and i['severity'] == 'critical'
                       and i['related_topics'] == [name]
                       and '/loopback_simulator' in i['related_nodes']
                       and 'expected within: 2000 ms' in i['evidence'] for i in issues), name


def validate_phase(directory, paused):
    result = json.loads((directory / 'assert.json').read_text())
    assert result['samples'] >= 3 and result['max_breach'] == 0, result
    assert result['passed'] == (not paused), result
    if paused:
        checks = {c['name']: c for c in result['checks']}
        for kind in ('tf_stale', 'topic_stale'):
            assert checks[f'no {kind} issues']['breaches'] == result['samples'], checks
    else:
        assert all(c['breaches'] == 0 for c in result['checks']), result
    _, samples = read_recording(str(directory / 'run.rgd.json'))
    settled = [s for s in samples if s['timestamp'] - samples[0]['timestamp'] >= 1]
    assert len(settled) == result['samples']
    for sample in settled:
        validate_snapshot(sample, paused)
    if paused:
        for name in ('/scan', '/odom'):
            receipts = [next(t['last_seen_time'] for t in s['topics'] if t['name'] == name)
                        for s in settled]
            assert len(set(receipts)) == 1, (name, receipts)


def validate_diff(output):
    result = json.loads((output / 'diff.json').read_text())
    assert result['verdict'] == 'improved' and not result['new_issues'], result
    for frames in CHAIN:
        assert any(i['kind'] == 'tf_stale' and i['related_frames'] == list(frames)
                   for i in result['resolved_issues']), frames
    for name in ('/scan', '/odom'):
        assert any(i['kind'] == 'topic_stale' and name in i['related_topics']
                   for i in result['resolved_issues']), name


def trace_quality(path):
    import bt2
    counts = Counter(type(m).__name__ for m in bt2.TraceCollectionMessageIterator(str(path)))
    assert not [name for name in counts if 'Discarded' in name], counts
    return dict(counts)


def check_cleanup():
    import psutil
    names = {'map_server', 'planner_server', 'controller_server', 'loopback_simulator',
             'agent', 'lttng-sessiond', 'lttng-consumerd'}
    remaining = []
    for process in psutil.process_iter(['pid', 'cmdline', 'status']):
        args = process.info['cmdline'] or []
        if process.info['status'] != psutil.STATUS_ZOMBIE and any(
                Path(arg).name in names for arg in args[:2]):
            remaining.append(process.pid)
    assert not remaining, f'Owned navigation/tracing processes remain: {remaining}'


def validate_bag(output, timeline):
    import rosbag2_py
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(output / 'bag'), storage_id='sqlite3'),
                rosbag2_py.ConverterOptions('', ''))
    stamps = {name: [] for name in (*RATES, '/map', '/tf', '/tf_static')}
    while reader.has_next():
        name, _, timestamp = reader.read_next()
        if name in stamps:
            stamps[name].append(timestamp / 1e9)
    assert len(stamps['/map']) == 1 and stamps['/tf_static'], {k: len(v) for k, v in stamps.items()}
    for name in ('/scan', '/odom'):
        values = stamps[name]
        assert sum(t < timeline['paused'] for t in values) >= 20
        assert sum(t > timeline['resumed'] for t in values) >= 20
        assert not [t for t in values if timeline['paused'] + 1 < t < timeline['resumed'] - 1], name
    _, snapshots = read_recording(str(output / 'bag.rgd.json'))
    for name, minimum in (('/scan', 5), ('/odom', 20)):
        fault = [s for s in snapshots if timeline['paused'] + 3 < s['timestamp']
                 < timeline['resumed'] - 1]
        assert fault, timeline
        assert any(any(i['kind'] == 'rate_drop' and name in i['related_topics']
                       and f'expected: {minimum:.1f} Hz' in i['evidence'] for i in s['issues'])
                   for s in fault), name
        recovered = [s for s in snapshots if timeline['resumed'] + 5 < s['timestamp']
                     < timeline['recovered_done'] - 1]
        assert recovered, timeline
        assert all(next(t['rate_hz'] for t in s['topics'] if t['name'] == name) >= minimum
                   for s in recovered), name
    result = json.loads((output / 'bag-assert.json').read_text())
    assert not result['passed'] and result['samples'] >= 20, result
    assert any(c['name'] == 'no rate_drop issues' and c['breaches'] > 0 for c in result['checks'])


def scenario(output, port):
    import rclpy
    from lifecycle_msgs.msg import State, Transition
    from navigation_driver import Driver
    from ros_graph_debugger.trace_convert import convert_trace, read_ctf_events
    from ros_graph_debugger.tracing import aggregate_callback_durations, load_duration_rows

    processes = Processes(output)
    rclpy.init()
    driver = Driver(lambda check, description: poll(check, processes, description))
    base = f'http://127.0.0.1:{port}'
    session, created, simulator = 'nav2-' + uuid.uuid4().hex, False, None
    timeline = {}

    def lttng(*args):
        run(['lttng', '--no-sessiond', *args], output, 'lttng-' + args[0], timeout=15)

    def snapshot(paused):
        driver.check()
        with urllib.request.urlopen(base + '/api/v1/snapshot', timeout=2) as response:
            result = json.load(response)
        (output / 'last-snapshot.json').write_text(json.dumps(result, indent=2))
        validate_snapshot(result, paused)
        return result

    def observe(phase, paused):
        directory = output / phase
        directory.mkdir()
        options = ['--duration', '6', '--settle', '1', '--fail-on', 'never', '--json',
                   '--record', directory / 'run.rgd.json']
        for name, minimum in RATES.items():
            options.extend(['--expect', f'{name}={minimum}', '--require-topic', name])
        for name in MANAGED:
            options.extend(['--require-node', name])
        for kind in FORBID:
            options.extend(['--forbid', kind])
        run(cli('assert', base, *options), directory, 'assert', expected=1 if paused else 0)
        validate_phase(directory, paused)
        run(cli('report', directory / 'run.rgd.json', '--md', directory / 'report.md',
                '--html', directory / 'report.html', '--path', '/controller_server'), directory, 'report')

    try:
        processes.start('sessiond', ['lttng-sessiond', '--no-kernel'])
        def daemon_ready():
            result = subprocess.run(['lttng', '--no-sessiond', 'list'], capture_output=True, timeout=2)
            assert result.returncode == 0, result.stderr.decode(errors='replace')
        poll(daemon_ready, processes, 'owned LTTng daemon')
        lttng('create', session, '--output', str(output / 'trace'))
        created = True
        lttng('enable-channel', '--userspace', '--buffers-pid', '--subbuf-size=16384',
              '--num-subbuf=4', 'callbacks', '--session', session)
        lttng('enable-event', '--userspace', '--channel', 'callbacks', 'ros2:*', '--session', session)
        lttng('add-context', '--userspace', '--channel', 'callbacks', '--type', 'vpid', '--session', session)
        lttng('start', session)
        width, height = 100, 60
        image = [0 if x in (0, width - 1) or y in (0, height - 1) else 255
                 for y in range(height) for x in range(width)]
        (output / 'map.pgm').write_text('P2\n100 60\n255\n' + ' '.join(map(str, image)) + '\n')
        (output / 'map.yaml').write_text('image: map.pgm\nresolution: 0.1\norigin: [0.0, 0.0, 0.0]\n'
                                        'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
        params = output / 'params.yaml'
        shutil.copyfile(Path(__file__).with_name('nav2_navigation.yaml'), params)
        processes.start('map-server', ['ros2', 'run', 'nav2_map_server', 'map_server', '--ros-args',
                                      '-p', 'yaml_filename:=' + str(output / 'map.yaml'),
                                      '-p', 'bond_heartbeat_period:=0.0'])
        driver.transition('/map_server', Transition.TRANSITION_CONFIGURE, State.PRIMARY_STATE_INACTIVE)
        driver.transition('/map_server', Transition.TRANSITION_ACTIVATE, State.PRIMARY_STATE_ACTIVE)
        simulator = processes.start('simulator', ['ros2', 'run', 'nav2_loopback_sim', 'loopback_simulator',
                                                 '--ros-args', '-p', 'publish_clock:=false'])
        driver.initialize_pose()
        for name, package in (('planner_server', 'nav2_planner'), ('controller_server', 'nav2_controller')):
            processes.start(name, ['ros2', 'run', package, name, '--ros-args', '--params-file', str(params)])
            driver.transition('/' + name, Transition.TRANSITION_CONFIGURE, State.PRIMARY_STATE_INACTIVE)
            driver.transition('/' + name, Transition.TRANSITION_ACTIVATE, State.PRIMARY_STATE_ACTIVE)
        processes.start('agent', ['ros2', 'run', 'ros_graph_debugger', 'agent', '--no-browser',
                                 '--profile', 'nav2', '--probe-regex', '^/(scan|odom|cmd_vel)$',
                                 '--port', str(port)])
        qos = output / 'bag-qos.yaml'
        qos.write_text('/map:\n  reliability: reliable\n  durability: transient_local\n  depth: 1\n'
                       '/tf_static:\n  reliability: reliable\n  durability: transient_local\n  depth: 1\n')
        recorder = processes.start('bag-record', ['ros2', 'bag', 'record', '-s', 'sqlite3',
                                                '--max-cache-size', '0', '--qos-profile-overrides-path',
                                                str(qos), '-o', str(output / 'bag'), *RATES,
                                                '/map', '/tf', '/tf_static'])
        def recorder_ready():
            with urllib.request.urlopen(base + '/api/v1/snapshot', timeout=2) as response:
                result = json.load(response)
            topics = {t['name']: t for t in result['topics']}
            assert all(any('rosbag' in n for n in topics[name]['subscribers']) for name in RATES)
        poll(recorder_ready, processes, 'real recorder subscriptions')
        result, truth = driver.start_goal(4.0)
        poll(lambda: snapshot(False), processes, 'healthy moving Nav2')
        observe('healthy', False)
        truth = driver.finish_goal(result, truth, True)
        (output / 'healthy-motion.json').write_text(json.dumps(truth, indent=2))
        result, truth = driver.start_goal(8.0)
        poll(lambda: driver.ready(driver.moving_commands >= truth['commands_before'] + 10),
             processes, 'fault goal movement')
        Processes.signal(simulator, signal.SIGSTOP)
        timeline['paused'] = time.time()
        try:
            poll(lambda: snapshot(True), processes, 'stopped TF and actual topic receipt ages')
            observe('paused', True)
            truth = driver.finish_goal(result, truth, False)
            (output / 'fault-action.json').write_text(json.dumps(truth, indent=2))
        finally:
            Processes.signal(simulator, signal.SIGCONT)
            timeline['resumed'] = time.time()
        result, truth = driver.start_goal(8.0)
        poll(lambda: snapshot(False), processes, 'recovered moving Nav2')
        observe('recovered', False)
        truth = driver.finish_goal(result, truth, True)
        timeline['recovered_done'] = time.time()
        (output / 'recovered-motion.json').write_text(json.dumps(truth, indent=2))
        (output / 'commands.json').write_text(json.dumps(list(driver.commands), indent=2))
        (output / 'timeline.json').write_text(json.dumps(timeline, indent=2))
        processes.stop(recorder)
        run(cli('diff', output / 'paused/run.rgd.json', output / 'recovered/run.rgd.json',
                '--json', '--fail-on-regression'), output, 'diff')
        validate_diff(output)
        run(cli('bag', 'analyze', output / 'bag', '--profile', 'nav2', '--out', output / 'bag.rgd.json'),
            output, 'bag-analyze')
        run(cli('assert', output / 'bag.rgd.json', '--settle', '0', '--fail-on', 'never',
                '--forbid', 'rate_drop', '--json'), output, 'bag-assert', expected=1)
        validate_bag(output, timeline)
        lttng('stop', session)
        lttng('destroy', session)
        created = False
        quality = trace_quality(output / 'trace')
        (output / 'trace-quality.json').write_text(json.dumps(quality, indent=2))
        run(cli('trace', 'convert', output / 'trace', '--out', output / 'callbacks.ndjson'),
            output, 'trace-convert')
        rows, counts = convert_trace(str(output / 'trace'))
        assert load_duration_rows(str(output / 'callbacks.ndjson')) == rows
        assert counts['attributed'] + counts['unattributed'] == counts['invocations']
        events = list(read_ctf_events(str(output / 'trace')))
        for name in ('controller_server', 'planner_server', 'map_server'):
            assert any(e['name'] == 'ros2:rcl_node_init' and e['pid'] is not None
                       and e['fields']['node_name'] == name for e in events), name
        stats = [asdict(c) for c in aggregate_callback_durations(rows)]
        assert any(c['node'] == '/controller_server' and c['callback'] == 'sub /odom'
                   and c['topic'] == '/odom' and c['count'] >= 60 for c in stats), stats
        (output / 'trace-counts.json').write_text(json.dumps(counts, indent=2))
        (output / 'callback-stats.json').write_text(json.dumps(stats, indent=2))
        # The trace includes internal callbacks without a supported ownership chain.
        # Preserve those counts; successful odometry attribution is a subset claim.
        print(f"Trace coverage: {counts['attributed']} attributed, {counts['unattributed']} unattributed",
              flush=True)
        trace_base = f'http://127.0.0.1:{port + 1}'
        processes.start('trace-agent', ['ros2', 'run', 'ros_graph_debugger', 'agent', '--no-browser',
                                       '--no-probe', '--profile', 'nav2', '--trace-file',
                                       str(output / 'callbacks.ndjson'), '--port', str(port + 1)])
        def trace_loaded():
            with urllib.request.urlopen(trace_base + '/api/v1/snapshot', timeout=2) as response:
                result = json.load(response)
            (output / 'trace-snapshot.json').write_text(json.dumps(result, indent=2))
            assert '/controller_server' in {n['id'] for n in result['nodes']}
            assert result['callbacks'] == stats
        poll(trace_loaded, processes, 'actual Nav2 callback attribution in agent')
        processes.check()
        print('Nav2 navigation: real goals, TF/topic fault, recovery, bag and callback attribution passed',
              flush=True)
    finally:
        if simulator is not None:
            Processes.signal(simulator, signal.SIGCONT)
        try:
            if created:
                lttng('destroy', session)
        finally:
            processes.close()
            check_cleanup()
            driver.close()
            if rclpy.ok():
                rclpy.shutdown()
            for phase in ('healthy', 'paused', 'recovered'):
                directory = output / phase
                recording = directory / 'run.rgd.json'
                if recording.exists() and not (directory / 'report.md').exists():
                    try:
                        run(cli('report', recording, '--md', directory / 'report.md',
                                '--html', directory / 'report.html'), directory, 'failure-report', timeout=10)
                    except Exception as exc:
                        print(f'Could not render partial report: {exc}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='fresh artifact directory')
    parser.add_argument('--port', type=int, default=3956)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ['ROS_DOMAIN_ID'] = '94'
    os.environ['LTTNG_HOME'] = str(output / 'lttng-home')
    Path(os.environ['LTTNG_HOME']).mkdir()
    def terminate(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(128 + signum)
    def timeout(_signum, _frame):
        raise TimeoutError('Nav2 navigation exceeded its 150-second budget')
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 150)
    try:
        scenario(output, args.port)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
