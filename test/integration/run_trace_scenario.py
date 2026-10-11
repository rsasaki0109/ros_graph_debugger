"""Record real LTTng/rclcpp callbacks, then test conversion and live diagnosis."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

from ros_graph_debugger.recording import read_recording
from ros_graph_debugger.trace_convert import convert_trace, read_ctf_events
from ros_graph_debugger.tracing import aggregate_callback_durations, load_duration_rows
from run_scenario import Processes, cli, run

MIN_INVOCATIONS = 8


def poll(check, processes, description, timeout=30):
    deadline = time.monotonic() + timeout
    last = 'not ready'
    while time.monotonic() < deadline:
        processes.check()
        try:
            return check()
        except (AssertionError, OSError, ValueError, KeyError) as exc:
            last = str(exc)
        time.sleep(0.1)
    raise AssertionError(f'{description}: {last}; see artifacts')


def progress(directory):
    lines = (directory / 'fixture.log').read_text().splitlines()
    rows = [line.split()[1:] for line in lines if line.startswith('RGD_PROGRESS ')]
    assert rows, 'no fixture callbacks completed'
    counts = [int(n) for n in rows[-1]]
    assert len(counts) == 3 and min(counts) >= MIN_INVOCATIONS, counts
    return counts


def validate_conversion(directory, mode, fixture_pid):
    events = list(read_ctf_events(str(directory / 'trace')))
    assert any(e['name'] == 'ros2:rcl_node_init' and e['pid'] == fixture_pid
               and e['fields']['node_name'] == 'trace_' + mode for e in events), \
        'fixture process registration/vpid missing from the real CTF trace'
    rows, counts = convert_trace(str(directory / 'trace'))
    (directory / 'conversion.json').write_text(json.dumps(counts, indent=2))
    assert counts['unattributed'] == 0, counts
    assert counts['attributed'] >= 3 * MIN_INVOCATIONS, counts
    assert load_duration_rows(str(directory / 'callbacks.ndjson')) == rows
    stats = aggregate_callback_durations(rows)
    node = '/trace_' + mode
    expected = {f'sub {node}/input', 'timer 200 ms', f'srv {node}/reset'}
    assert {c.callback for c in stats if c.node == node} == expected, stats
    assert all(c.node == node and c.count >= MIN_INVOCATIONS for c in stats), stats
    assert all(c.p95_ms >= 150 if mode == 'slow' else c.p95_ms < 100 for c in stats), stats
    (directory / 'callback-stats.json').write_text(json.dumps([asdict(c) for c in stats], indent=2))
    return stats


def snapshot_ready(base, directory, stats):
    with urllib.request.urlopen(base + '/api/v1/snapshot', timeout=2) as response:
        snapshot = json.load(response)
    (directory / 'last-snapshot.json').write_text(json.dumps(snapshot, indent=2))
    expected = [asdict(c) for c in stats]
    assert snapshot['callbacks'] == expected, 'agent did not load all real callback stats'
    assert stats[0].node in {n['id'] for n in snapshot['nodes']}, 'fixture not discovered'
    return snapshot


def validate_diagnosis(directory, mode, stats):
    result = json.loads((directory / 'assert.json').read_text())
    assert result['samples'] >= 3 and result['passed'] == (mode == 'healthy'), result
    checks = {c['name']: c for c in result['checks']}
    assert (checks['no slow_callback issues']['breaches'] > 0) == (mode == 'slow'), checks
    assert checks['node /trace_' + mode + ' present']['breaches'] == 0, checks
    _, snapshots = read_recording(str(directory / 'run.rgd.json'))
    settled = [s for s in snapshots if s['timestamp'] - snapshots[0]['timestamp'] >= 1]
    assert len(settled) == result['samples']
    node = '/trace_' + mode
    for snapshot in settled:
        assert snapshot['callbacks'] == [asdict(c) for c in stats]
        issues = [i for i in snapshot['issues'] if i['kind'] == 'slow_callback']
        if mode == 'healthy':
            assert not issues, issues
        else:
            # Subscription, timer and service must each produce their own issue.
            assert len(issues) == 3, issues
            expected_evidence = {
                (f'p95: {c.p95_ms:.0f} ms (limit 100 ms)', f'samples: {c.count}')
                for c in stats}
            for issue in issues:
                assert issue['related_nodes'] == [node], issue
                assert any(p95 in issue['evidence'] and samples in issue['evidence']
                           for p95, samples in expected_evidence), issue
            assert sum(i['related_topics'] == [node + '/input'] for i in issues) == 1
            assert sum(i['related_topics'] == [] for i in issues) == 2


def scenario(output, mode, executable, port):
    directory = output / mode
    directory.mkdir(parents=True, exist_ok=False)
    processes = Processes(directory)
    session = 'rgd-' + uuid.uuid4().hex
    created = False

    def lttng(*args):
        run(['lttng', '--no-sessiond', *args], directory,
            'lttng-' + args[0] + '-' + uuid.uuid4().hex[:6], timeout=15)

    try:
        # Complete infrastructure parameter events before the C++ time-source
        # subscription exists, so only the three controlled callbacks run.
        processes.start('driver', [sys.executable, str(Path(__file__).with_name('trace_driver.py')),
                                   mode])

        def driver_ready():
            assert 'RGD_DRIVER_READY' in (directory / 'driver.log').read_text()

        poll(driver_ready, processes, 'DDS driver initialization')
        lttng('create', session, '--output', str(directory / 'trace'))
        created = True
        # Small per-process buffers keep this test independent of host CPU count.
        lttng('enable-channel', '--userspace', '--buffers-pid', '--subbuf-size=16384',
              '--num-subbuf=4', 'callbacks', '--session', session)
        lttng('enable-event', '--userspace', '--channel', 'callbacks', 'ros2:*',
              '--session', session)
        lttng('add-context', '--userspace', '--channel', 'callbacks', '--type', 'vpid',
              '--session', session)
        lttng('start', session)
        # Registration events must be captured before any C++ node is constructed.
        fixture = processes.start('fixture', [str(executable), mode])
        poll(lambda: progress(directory), processes, 'subscription/timer/service callbacks', 40)
        lttng('stop', session)
        lttng('destroy', session)
        created = False
        run(cli('trace', 'convert', directory / 'trace', '--out', directory / 'callbacks.ndjson'),
            directory, 'trace-convert')
        stats = validate_conversion(directory, mode, fixture.pid)
        base = f'http://127.0.0.1:{port}'
        # Exercise both supported input forms: direct CTF and CLI-produced NDJSON.
        trace_file = directory / ('trace' if mode == 'slow' else 'callbacks.ndjson')
        processes.start('agent', ['ros2', 'run', 'ros_graph_debugger', 'agent',
                                 '--no-browser', '--no-probe', '--trace-file', str(trace_file),
                                 '--port', str(port)])
        poll(lambda: snapshot_ready(base, directory, stats), processes, 'agent trace loading')
        run(cli('assert', base, '--duration', '5', '--settle', '1', '--fail-on', 'never',
                '--forbid', 'slow_callback', '--require-node', '/trace_' + mode, '--json',
                '--record', directory / 'run.rgd.json'), directory, 'assert',
            expected=1 if mode == 'slow' else 0)
        validate_diagnosis(directory, mode, stats)
        run(cli('report', directory / 'run.rgd.json', '--md', directory / 'report.md',
                '--html', directory / 'report.html', '--path', '/trace_' + mode), directory, 'report')
        processes.check()
        print(f'{mode}: real LTTng callbacks, attribution, CLI and agent diagnosis passed', flush=True)
    finally:
        try:
            if created:
                lttng('destroy', session)
        finally:
            processes.close()
            recording = directory / 'run.rgd.json'
            if recording.exists() and not (directory / 'report.md').exists():
                try:
                    run(cli('report', recording, '--md', directory / 'report.md',
                            '--html', directory / 'report.html', '--path', '/trace_' + mode),
                        directory, 'failure-report', timeout=10)
                except Exception as exc:
                    print(f'Could not render partial recording: {exc}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='fresh artifact directory')
    parser.add_argument('--fixture', type=Path, required=True, help='compiled C++ fixture')
    parser.add_argument('--port', type=int, default=3951)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ['ROS_DOMAIN_ID'] = '92'
    os.environ['LTTNG_HOME'] = str(output / 'lttng-home')
    Path(os.environ['LTTNG_HOME']).mkdir()
    daemon = Processes(output)

    def terminate(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    def timeout(_signum, _frame):
        raise TimeoutError('LTTng scenarios exceeded their 150-second budget')

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 150)
    try:
        daemon.start('sessiond', ['lttng-sessiond', '--no-kernel'])

        def daemon_ready():
            status = subprocess.run(['lttng', '--no-sessiond', 'list'], capture_output=True,
                                    timeout=2)
            assert status.returncode == 0, status.stderr.decode(errors='replace')

        poll(daemon_ready, daemon, 'LTTng session daemon')
        for mode in ('slow', 'healthy'):
            scenario(output, mode, args.fixture.resolve(), args.port)
    finally:
        daemon.close()
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
