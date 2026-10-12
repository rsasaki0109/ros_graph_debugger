"""Strict, standalone multiprocess ROS integration test (not pytest-collected)."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

from ros_graph_debugger.recording import read_recording

STAGES = ('camera', 'detector', 'tracker', 'planner', 'controller')
ROLES = ('lifecycle', 'listener', 'clients', 'processing', 'deadline_pub', 'deadline_sub')
OBJECTS = '/perception/object_recognition/objects'
TOPICS = ('/sensing/camera/image_raw', OBJECTS, '/perception/tracked_objects',
          '/planning/scenario_planning/trajectory', '/control/command/control_cmd')
KINDS = ('rate_drop', 'bottleneck', 'lifecycle_not_active', 'no_service_server',
         'no_action_server', 'qos_mismatch', 'slow_callback')


class Processes:
    """Own whole process groups, including grandchildren of ros2 wrappers."""

    def __init__(self, directory):
        self.directory = directory
        self.children = []

    def start(self, name, command):
        with (self.directory / f'{name}.log').open('wb') as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.children.append((name, child))
        return child

    @staticmethod
    def signal(child, sig):
        # Signal even if the group leader exited: grandchildren may still live.
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass

    def check(self):
        for name, child in self.children:
            if child.poll() is not None:
                raise AssertionError(f'{name} exited {child.returncode}; see {name}.log')

    def stop(self, child):
        self.signal(child, signal.SIGINT)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.signal(child, signal.SIGKILL)
            child.wait(timeout=5)
            raise AssertionError('bag recorder did not shut down cleanly')
        self.signal(child, signal.SIGKILL)
        self.children = [(n, p) for n, p in self.children if p is not child]
        assert child.returncode == 0, f'bag recorder exited {child.returncode}'

    def close(self):
        # Defer the scenario alarm until cleanup finishes; timeout must not
        # interrupt the escalation that kills an unresponsive process group.
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        signal.setitimer(signal.ITIMER_REAL, 0)
        started = time.monotonic()
        try:
            for sig, budget in ((signal.SIGINT, 5), (signal.SIGTERM, 2), (signal.SIGKILL, 2)):
                for _, child in self.children:
                    self.signal(child, sig)
                deadline = time.monotonic() + budget
                for _, child in self.children:
                    try:
                        child.wait(timeout=max(0.01, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            if remaining:
                remaining -= time.monotonic() - started
                signal.setitimer(signal.ITIMER_REAL, max(0.001, remaining), interval)


def cli(*args):
    # ros2 run appends its own status line to stdout on exit 1, corrupting JSON.
    # Invoke the exact rgd entry point directly; the agent still uses ros2 run.
    return [sys.executable, '-m', 'ros_graph_debugger.cli', *map(str, args)]


def run(command, directory, name, expected=0, timeout=40):
    """Capture diagnostics without hiding the underlying exit status."""
    suffix = 'json' if '--json' in command else 'stdout.log'
    with (directory / f'{name}.{suffix}').open('wb') as out, \
            (directory / f'{name}.stderr.log').open('wb') as err:
        child = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
        try:
            status = child.wait(timeout=timeout)
        finally:
            Processes.signal(child, signal.SIGKILL)
            child.wait(timeout=5)
    assert status == expected, f'{name}: exit {status}, expected {expected}; see artifacts'


def wait_for_graph(base, processes, directory, recorder=False):
    deadline = time.monotonic() + 35
    last = 'no response'
    while time.monotonic() < deadline:
        processes.check()
        try:
            with urllib.request.urlopen(base + '/api/v1/snapshot', timeout=2) as response:
                snapshot = json.load(response)
            (directory / 'last-snapshot.json').write_text(json.dumps(snapshot, indent=2))
            nodes = {n['id']: n for n in snapshot['nodes']}
            topics = {t['name']: t for t in snapshot['topics']}
            wanted = {'/' + s for s in STAGES} | {
                '/integration/managed', '/integration/listener', '/integration/clients',
                '/integration/deadline_pub', '/integration/deadline_sub',
                '/control/integration/processing'}
            assert wanted <= nodes.keys(), f'missing nodes: {wanted - nodes.keys()}'
            assert nodes['/integration/managed']['lifecycle_state'] == 'inactive'
            for topic in TOPICS:
                assert topics[topic]['probed'] and (topics[topic]['rate_hz'] or 0) > 0, topic
                if recorder:
                    assert any('rosbag' in n for n in topics[topic]['subscribers']), topic
            assert any(c['callback'] == 'processing_time' and c['count'] >= 5
                       for c in snapshot['callbacks'])
            return
        except (OSError, ValueError, AssertionError, KeyError) as exc:
            last = str(exc)
            time.sleep(0.2)
    raise AssertionError(f'DDS/agent readiness failed: {last}; see last-snapshot.json')


def validate_live(directory, mode):
    result = json.loads((directory / 'assert.json').read_text())
    assert result['passed'] is False and result['samples'] >= 12, result
    checks = {c['name']: c for c in result['checks']}
    for kind in KINDS[2:]:
        assert checks[f'no {kind} issues']['breaches'] > 0, kind
    _, snapshots = read_recording(str(directory / 'run.rgd.json'))
    assert snapshots
    settled = [s for s in snapshots if s['timestamp'] - snapshots[0]['timestamp'] >= 5]
    assert len(settled) == result['samples']
    issues = [i for s in settled for i in s['issues']]
    for kind in KINDS[2:]:
        assert any(i['kind'] == kind for i in issues), kind
    assert any(i['kind'] == 'qos_mismatch'
               and '/integration/deadline' in i['related_topics']
               and any('deadline' in e and '200 ms' in e and '100 ms' in e
                       for e in i['evidence']) for i in issues)
    for kind in ('no_service_server', 'no_action_server'):
        assert any(i['kind'] == kind and '/integration/clients' in i['related_nodes']
                   and 'servers: 0' in i['evidence'] for i in issues), kind
    assert any(i['kind'] == 'slow_callback'
               and '/control/integration/processing' in i['related_nodes']
               and '/control/integration/processing/debug/processing_time_ms' in i['related_topics']
               and 'p95: 120 ms (limit 15 ms)' in i['evidence']
               and 'duration source: application-reported processing time' in i['evidence']
               for i in issues)
    assert any(c['callback'] == 'processing_time' and c['p95_ms'] == 120
               and c.get('source') == 'application_report'
               and c['node'] == '/control/integration/processing'
               for s in settled for c in s['callbacks'])
    assert any(i['kind'] == 'lifecycle_not_active'
               and '/integration/managed' in i['related_nodes']
               and 'lifecycle state: inactive' in i['evidence']
               and any('/integration/managed/output:' in e and 'waiting' in e
                       for e in i['evidence']) for i in issues)
    detector_issues = [i for i in issues if '/detector' in i['related_nodes']
                       and i['kind'] in ('rate_drop', 'bottleneck', 'slow_callback')]
    if mode == 'slow':
        assert any(i['kind'] in ('rate_drop', 'bottleneck') for i in detector_issues)
        assert any(checks[f'no {kind} issues']['breaches'] > 0 for kind in KINDS[:2])
    else:
        assert not detector_issues, f'healthy detector issues: {detector_issues[:3]}'


def scenario(output, mode, port):
    directory = output / mode
    directory.mkdir(parents=True, exist_ok=False)
    processes = Processes(directory)
    base = f'http://127.0.0.1:{port}'
    helper = Path(__file__).with_name('helper_node.py')
    try:
        for stage in STAGES:
            processes.start(stage, [sys.executable, '-m',
                                   'ros_graph_debugger.examples.demo_pipeline',
                                   '--stage', stage, '--mode', mode])
        for role in ROLES:
            processes.start(role, [sys.executable, str(helper), role])
        processes.start('agent', ['ros2', 'run', 'ros_graph_debugger', 'agent',
                                 '--no-browser', '--profile', 'autoware', '--port', str(port),
                                 '--probe-regex', '^/(sensing|perception|planning|control)/',
                                 '--max-probe-topics', '20'])
        wait_for_graph(base, processes, directory)
        bag = directory / 'bag'
        recorder = processes.start('bag-record', ['ros2', 'bag', 'record',
                                                '-s', 'sqlite3', '-o', str(bag), *TOPICS])
        wait_for_graph(base, processes, directory, recorder=True)
        command = cli('assert', base, '--duration', '20', '--settle', '5',
                      '--json', '--record', directory / 'run.rgd.json', '--fail-on', 'never',
                      '--require-node', '/detector')
        for kind in KINDS:
            command += ['--forbid', kind]
        run(command, directory, 'assert', expected=1)
        processes.check()
        processes.stop(recorder)
        run(cli('report', directory / 'run.rgd.json', '--md', directory / 'report.md',
                '--html', directory / 'report.html', '--path', '/detector'), directory, 'report')
        assert '## Pipeline path over time' in (directory / 'report.md').read_text()
        run(cli('bag', 'analyze', bag, '--out', directory / 'bag.rgd.json',
                '--expect', OBJECTS + '=8'), directory, 'bag-analyze')
        run(cli('assert', directory / 'bag.rgd.json', '--json', '--fail-on', 'never',
                '--settle', '5', '--expect', OBJECTS + '=8', '--forbid', 'rate_drop'),
            directory, 'bag-assert',
            expected=1 if mode == 'slow' else 0)
        bag_result = json.loads((directory / 'bag-assert.json').read_text())
        assert bag_result['samples'] > 0
        _, bag_snapshots = read_recording(str(directory / 'bag.rgd.json'))
        settled_bag = [s for s in bag_snapshots
                       if s['timestamp'] - bag_snapshots[0]['timestamp'] >= 5]
        assert len(settled_bag) == bag_result['samples']
        dropped = [i for s in settled_bag for i in s['issues']
                   if i['kind'] == 'rate_drop' and OBJECTS in i['related_topics']]
        assert bool(dropped) == (mode == 'slow'), dropped
        validate_live(directory, mode)
        print(f'{mode}: live issue contracts, report and real bag passed', flush=True)
    finally:
        processes.close()
        # Preserve a report even when an assertion failed before the normal report step.
        recording = directory / 'run.rgd.json'
        if recording.exists() and not (directory / 'report.md').exists():
            try:
                run(cli('report', recording, '--md', directory / 'report.md',
                        '--html', directory / 'report.html', '--path', '/detector'),
                    directory, 'failure-report')
            except Exception as exc:
                print(f'Could not render partial recording: {exc}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
                        help='fresh artifact directory; existing scenario outputs are never reused')
    parser.add_argument('--port', type=int, default=3950)
    args = parser.parse_args()

    def terminate(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    def timeout(_signum, _frame):
        raise TimeoutError('integration scenarios exceeded their 180-second budget')

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 180)
    os.environ['ROS_DOMAIN_ID'] = '91'
    try:
        for mode in ('slow', 'healthy'):
            scenario(args.output.resolve(), mode, args.port)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
