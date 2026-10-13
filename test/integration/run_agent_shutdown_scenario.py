"""Require clean agent SIGINT exits while a separate DDS publisher stays live."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.request

from run_scenario import Processes

TOPIC = '/integration/shutdown_stream'


def scenario(output, port):
    processes = Processes(output)
    results = []
    directory = Path(__file__).resolve().parent
    try:
        graph_file = output / 'publisher-graph.json'
        processes.start('publisher', [sys.executable, str(directory / 'shutdown_publisher.py'),
                                      '--graph-out', str(graph_file)])
        for index in range(5):
            cycle = output / str(index)
            cycle.mkdir()
            started = time.monotonic()
            agent = processes.start('agent-' + str(index), [sys.executable, '-m',
                'ros_graph_debugger.agent', '--no-browser', '--port', str(port),
                '--probe-topic', TOPIC])
            deadline = time.monotonic() + 15
            last = 'not ready'
            while True:
                processes.check()
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/snapshot', timeout=2) as response:
                        sample = json.load(response)
                    (cycle / 'last-snapshot.json').write_text(json.dumps(sample, indent=2) + '\n')
                    topic = next(t for t in sample['topics'] if t['name'] == TOPIC)
                    assert topic['probed'] and (topic['rate_hz'] or 0) > 0, topic
                    assert '/agent_shutdown_publisher' in topic['publishers'], topic
                    # RGD intentionally excludes its own node/endpoints from
                    # graph snapshots; use the independent publisher's graph.
                    graph = json.loads(graph_file.read_text())
                    assert graph['monotonic_time'] >= started, graph
                    assert '/ros_graph_debugger' in graph['subscriber_nodes'], graph
                    (cycle / 'publisher-graph.json').write_text(json.dumps(graph, indent=2) + '\n')
                    break
                except (OSError, ValueError, AssertionError, KeyError, StopIteration) as exc:
                    last = repr(exc)
                assert time.monotonic() < deadline, f'agent {index} readiness: {last}'
                time.sleep(.05)
            # Publisher remains live: SIGINT races with active DDS callbacks.
            # stop() requires exit0 and reaps/kills the entire owned group.
            processes.stop(agent)
            processes.check()
            results.append({'cycle': index, 'agent_exit': agent.returncode,
                            'observed_topic': TOPIC, 'ready_rate_hz': topic['rate_hz']})
            (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        assert len(results) == 5 and all(r['agent_exit'] == 0 for r in results)
    finally:
        processes.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=3983)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault('ROS_DOMAIN_ID', '96')

    def timeout(_signum, _frame):
        raise TimeoutError('agent shutdown scenario exceeded 90 seconds')

    def terminate(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 90)
    try:
        scenario(args.output, args.port)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
