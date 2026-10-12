"""Strict process CPU warmup, real load, recovery and exit checks."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.request

import psutil
import rclpy
from rclpy.node import Node
from std_srvs.srv import SetBool

from run_scenario import Processes, cli, run

TARGET = '/integration/cpu_worker'
BASE = 'http://127.0.0.1:3962'


def fetch():
    with urllib.request.urlopen(BASE + '/api/v1/snapshot', timeout=1) as response:
        return json.load(response)


def wait_for(check, processes, output, phase):
    deadline = time.monotonic() + 20
    last = 'no response'
    while time.monotonic() < deadline:
        processes.check()
        try:
            snapshot = fetch()
            (output / 'last-snapshot.json').write_text(json.dumps(snapshot, indent=2))
            node = next(n for n in snapshot['nodes'] if n['id'] == TARGET)
            check(snapshot, node)
            (output / f'{phase}.json').write_text(json.dumps(snapshot, indent=2))
            return snapshot
        except (OSError, ValueError, AssertionError, StopIteration) as exc:
            last = str(exc)
        time.sleep(0.1)
    raise AssertionError(f'{phase}: {last}')


def call(node, client, value, processes):
    request = SetBool.Request()
    request.data = value
    future = client.call_async(request)
    deadline = time.monotonic() + 10
    while not future.done() and time.monotonic() < deadline:
        processes.check()
        rclpy.spin_once(node, timeout_sec=0.05)
    assert future.done() and future.result().success, 'CPU control service failed'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault('ROS_DOMAIN_ID', '103')
    processes = Processes(args.output)
    node = None
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('CPU scenario budget')))
    signal.alarm(85)
    try:
        rclpy.init(args=[])
        node = Node('cpu_scenario_driver')
        client = node.create_client(SetBool, TARGET + '/set_busy')
        worker = processes.start('worker', [sys.executable, 'test/integration/cpu_worker.py',
            '--ros-args', '-r', '__node:=cpu_worker', '-r', '__ns:=/integration'])
        deadline = time.monotonic() + 15
        while not client.wait_for_service(timeout_sec=0.1):
            processes.check()
            assert time.monotonic() < deadline, 'worker service did not become ready'
        processes.start('agent', ['ros2', 'run', 'ros_graph_debugger', 'agent',
                                 '--no-browser', '--no-probe', '--no-lifecycle', '--port', '3962'])
        def warmup(snapshot, measured):
            assert measured['pid'] == worker.pid, measured
            assert measured['process_mapping_confidence'] == 'high', measured
            assert measured['cpu_percent'] is None, 'first CPU sample must be unknown'
            assert measured['rss_bytes'] > 0, measured
        wait_for(warmup, processes, args.output, 'warmup')
        def cooled(snapshot, measured):
            assert measured['pid'] == worker.pid
            assert measured['cpu_percent'] is not None and measured['cpu_percent'] < 20, measured
            assert not any(i['kind'] == 'high_cpu' and TARGET in i['related_nodes'] for i in snapshot['issues'])
        wait_for(cooled, processes, args.output, 'idle')
        call(node, client, True, processes)
        reference = psutil.Process(worker.pid)
        reference.cpu_percent(interval=None)  # independent psutil baseline
        def loaded(snapshot, measured):
            assert measured['pid'] == worker.pid and measured['cpu_percent'] > 90, measured
            issue = next(i for i in snapshot['issues'] if i['kind'] == 'high_cpu' and TARGET in i['related_nodes'])
            assert 'mapping confidence: high' in issue['evidence'], issue
            assert any(e.startswith('CPU: ') for e in issue['evidence']), issue
            observed = reference.cpu_percent(interval=None)
            assert observed > 90, 'independent process sample not hot'
            (args.output / 'independent-cpu.json').write_text(json.dumps({
                'pid': worker.pid, 'cpu_percent': observed, 'psutil_version': psutil.__version__}, indent=2))
        wait_for(loaded, processes, args.output, 'loaded')
        for phase, expected in [('loaded-assert', 1), ('recovered-assert', 0)]:
            if expected == 0:
                call(node, client, False, processes)
                wait_for(cooled, processes, args.output, 'recovered')
            run(cli('assert', BASE, '--duration', '6', '--settle', '1', '--json',
                    '--forbid', 'high_cpu', '--record', args.output / f'{phase}.rgd.json'),
                args.output, phase, expected=expected)
            result = json.loads((args.output / f'{phase}.json').read_text())
            checks = {c['name']: c for c in result['checks']}
            assert result['samples'] >= 4, result
            breaches = checks['no high_cpu issues']['breaches']
            assert breaches == (result['samples'] if expected else 0), result
        # Retain the graph entry briefly, but ensure exited-process values clear.
        processes.signal(worker, signal.SIGINT)
        assert worker.wait(timeout=5) == 0, 'worker did not shut down cleanly'
        processes.signal(worker, signal.SIGKILL)
        processes.children = [(n, p) for n, p in processes.children if p is not worker]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            processes.check()
            snapshot = fetch()
            measured = next((n for n in snapshot['nodes'] if n['id'] == TARGET), None)
            if measured is None or all(measured[k] is None for k in ('pid', 'cpu_percent', 'rss_bytes', 'process_name')):
                (args.output / 'exited.json').write_text(json.dumps(snapshot, indent=2))
                break
            time.sleep(0.1)
        else:
            raise AssertionError('exited process retained stale metrics')
        print('Process CPU: unknown warmup, real load, strict assertion, recovery and exit passed')
    finally:
        processes.close()
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        signal.alarm(0)


if __name__ == '__main__':
    main()
