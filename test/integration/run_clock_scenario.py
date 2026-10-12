"""Separate-process ROS-clock startup, stale-data fault and recovery."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.request

os.environ['ROS_DOMAIN_ID'] = '91'
import rclpy
from rclpy.node import Node
from std_srvs.srv import SetBool, Trigger

from run_scenario import Processes, cli, run

TOPIC = '/localization/kinematic_state'
BASE = 'http://127.0.0.1:3959'


def snapshot():
    with urllib.request.urlopen(BASE + '/api/v1/snapshot', timeout=1) as response:
        return json.load(response)


def wait_for(check, processes, directory, label):
    deadline = time.monotonic() + 25
    last = 'not ready'
    while time.monotonic() < deadline:
        processes.check()
        try:
            data = snapshot()
            (directory / 'last-snapshot.json').write_text(json.dumps(data, indent=2))
            topic = next(t for t in data['topics'] if t['name'] == TOPIC)
            check(data, topic)
            return data
        except (OSError, ValueError, AssertionError, StopIteration) as error:
            last = str(error)
            time.sleep(0.1)
    raise AssertionError(f'{label}: {last}')


def call(node, client, request, processes):
    deadline = time.monotonic() + 10
    while not client.wait_for_service(timeout_sec=0.1):
        processes.check()
        assert time.monotonic() < deadline, client.srv_name
    future = client.call_async(request)
    while not future.done():
        processes.check()
        assert time.monotonic() < deadline, client.srv_name
        rclpy.spin_once(node, timeout_sec=0.1)
    assert future.result().success


def phase(directory, processes, delayed):
    directory.mkdir()
    def ready(data, topic):
        assert topic['probed'] and topic['rate_hz'] >= 30, topic
        age = topic['header_age_p95_ms']
        assert age is not None and ((age > 100) if delayed else (age <= 100)), topic
        hits = [i for i in data['issues'] if i['kind'] == 'stale_data' and TOPIC in i['related_topics']]
        assert bool(hits) == delayed, hits
        if delayed:
            assert len(hits) == 1
            assert hits[0]['evidence'] == [f'p95 header age: {age:.0f} ms',
                                            'expected within: 100 ms']
            assert hits[0]['related_nodes'] == ['/integration/clock_fixture']
    data = wait_for(ready, processes, directory, 'stamped phase')
    (directory / 'snapshot.json').write_text(json.dumps(data, indent=2))
    run(cli('assert', BASE, '--duration', '6', '--settle', '1', '--json',
            '--expect', TOPIC + '=30', '--require-topic', TOPIC,
            '--forbid', 'stale_data', '--record', directory / 'run.rgd.json'),
        directory, 'assert', expected=1 if delayed else 0)
    result = json.loads((directory / 'assert.json').read_text())
    assert result['samples'] >= 4 and result['passed'] is not delayed
    if delayed:
        check = next(c for c in result['checks'] if c['name'] == 'no stale_data issues')
        assert check['breaches'] == result['samples'], check
    run(cli('report', directory / 'run.rgd.json', '--md', directory / 'report.md'), directory, 'report')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    processes = Processes(args.output)
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('clock scenario budget')))
    signal.alarm(120)
    node = None
    try:
        rclpy.init(args=[])
        node = Node('clock_scenario_driver')
        start = node.create_client(Trigger, '/integration/clock_fixture/start')
        lag = node.create_client(SetBool, '/integration/clock_fixture/set_lag')
        pause = node.create_client(SetBool, '/integration/clock_fixture/pause_clock')
        processes.start('agent', ['ros2', 'run', 'ros_graph_debugger', 'agent',
            '--no-browser', '--profile', 'autoware', '--port', '3959',
            '--probe-regex', '^/localization/kinematic_state$',
            '--ros-args', '-p', 'use_sim_time:=true', '-r', '__node:=sim_clock_agent'])
        processes.start('publisher', [sys.executable, 'test/integration/clock_publisher.py'])
        def unstarted(data, topic):
            assert any(n['id'] == '/sim_clock_agent' for n in data['nodes'])
            assert topic['probed'] and (topic['rate_hz'] or 0) >= 30, topic
            assert topic['header_age_p95_ms'] is None, topic
            assert not any(i['kind'] == 'stale_data' and TOPIC in i['related_topics'] for i in data['issues'])
        data = wait_for(unstarted, processes, args.output, 'unstarted clock')
        (args.output / 'unstarted.json').write_text(json.dumps(data, indent=2))
        call(node, start, Trigger.Request(), processes)
        phase(args.output / 'healthy', processes, False)
        request = SetBool.Request()
        request.data = True
        call(node, lag, request, processes)
        phase(args.output / 'delayed', processes, True)
        request.data = False
        call(node, lag, request, processes)
        phase(args.output / 'recovered', processes, False)
        request.data = True
        call(node, pause, request, processes)
        call(node, lag, request, processes)
        phase(args.output / 'paused-delayed', processes, True)
        request.data = False
        call(node, lag, request, processes)
        phase(args.output / 'paused-recovered', processes, False)
        print('ROS clock: startup, healthy/fault/recovery and paused-clock collection passed')
    finally:
        processes.close()
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        signal.alarm(0)


if __name__ == '__main__':
    main()
