"""Reject real CTF loss while preserving an exactly counted normal control."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import bt2
from ros_graph_debugger.trace_convert import TraceDataLoss, convert_trace
from ros_graph_debugger.tracing import load_duration_rows
from run_scenario import Processes, cli, run


def identity(pid):
    path = Path('/proc') / str(pid)
    fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'group': int(fields[2]), 'start_ticks': int(fields[19]),
            'exe': (path / 'exe').resolve().name}


def consumers(daemon):
    members = []
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            row = identity(int(path.parent.name))
            if row['group'] == daemon.pid and row['exe'] == 'lttng-consumerd':
                members.append(row)
        except (OSError, ValueError):
            continue
    return members


def resume(paused):
    for member in paused:
        try:
            # A recycled PID never authorizes signaling a new process.
            if identity(member['pid']) == member:
                os.kill(member['pid'], signal.SIGCONT)
        except ProcessLookupError:
            pass
        except FileNotFoundError:
            pass
    paused.clear()


def poll(check, daemon, fixture=None, timeout=15):
    deadline = time.monotonic() + timeout
    last = 'not ready'
    while True:
        assert daemon.poll() is None, 'owned session daemon exited'
        try:
            result = check()
            assert result
            return result
        except (AssertionError, OSError, ValueError) as exc:
            last = repr(exc)
        if fixture is not None:
            assert fixture.poll() is None, f'fixture exited before readiness: {last}'
        assert time.monotonic() < deadline, last
        time.sleep(.02)


def quality(path):
    discarded = []
    for message in bt2.TraceCollectionMessageIterator(str(path)):
        if isinstance(message, (bt2._DiscardedEventsMessageConst,
                                bt2._DiscardedPacketsMessageConst)):
            discarded.append({'type': type(message).__name__, 'count': message.count})
    return discarded


def scenario(output, executable, loss_unit):
    packets = loss_unit == 'packets'
    processes = Processes(output)
    daemon = processes.start('sessiond', ['lttng-sessiond', '--no-kernel'])
    created = False
    paused = []
    results = {}

    def lttng(*args):
        command = ['lttng', '--no-sessiond', *args]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        with (output / 'lttng.log').open('a') as log:
            log.write(json.dumps({'command': command, 'exit': result.returncode,
                                 'stdout': result.stdout, 'stderr': result.stderr}) + '\n')
        assert result.returncode == 0, result.stderr

    try:
        poll(lambda: subprocess.run(['lttng', '--no-sessiond', 'list'],
             capture_output=True, timeout=2).returncode == 0, daemon)
        for mode in ('healthy', 'loss'):
            directory = output / mode
            directory.mkdir()
            session = 'rgd-trace-loss-' + mode
            expected = 5500 if packets else (500 if mode == 'healthy' else 5000)
            lttng('create', session, '--output', str(directory / 'trace'))
            created = True
            normal_size = '1048576' if packets else '262144'
            lttng('enable-channel', '--userspace', '--buffers-pid',
                  *(['--overwrite'] if packets else []),
                  '--subbuf-size=' + (normal_size if mode == 'healthy' else '4096'),
                  '--num-subbuf=' + ('4' if mode == 'healthy' else '2'),
                  'callbacks', '--session', session)
            lttng('enable-event', '--userspace', '--channel', 'callbacks',
                  'ros2:*', '--session', session)
            lttng('add-context', '--userspace', '--channel', 'callbacks',
                  '--type', 'vpid', '--session', session)
            lttng('start', session)
            # Pin only this owned fixture, so its tiny per-CPU buffer overflows
            # predictably while the owned consumer is deliberately suspended.
            cpu = min(os.sched_getaffinity(0))
            fixture = processes.start('fixture-' + mode, ['taskset', '--cpu-list', str(cpu),
                str(executable), str(directory), str(expected), *(['packets'] if packets else [])])
            poll(lambda: (directory / 'ready').exists(), daemon, fixture)
            if packets:
                (directory / 'warm-go').touch()
                poll(lambda: int((directory / 'warm-done').read_text()) == 500, daemon, fixture)
                # Drain initial packets before the burst so overwriting middle
                # packets produces an observable sequence gap in the real CTF.
                lttng('stop', session)
                warm_notifications = quality(directory / 'trace')
                assert not warm_notifications, warm_notifications
                warm_rows, warm_counts = convert_trace(str(directory / 'trace'))
                assert len(warm_rows) == warm_counts['attributed'] == 500, warm_counts
                assert warm_counts['unattributed'] == 0, warm_counts
                (directory / 'warm-quality.json').write_text(json.dumps(
                    {'discard_notifications': warm_notifications,
                     'conversion_counts': warm_counts}, indent=2) + '\n')
                lttng('start', session)
            if mode == 'loss':
                members = poll(lambda: consumers(daemon), daemon, fixture)
                (directory / 'paused-consumers.json').write_text(json.dumps(members, indent=2) + '\n')
                for member in members:
                    assert identity(member['pid']) == member
                    paused.append(member)
                    os.kill(member['pid'], signal.SIGSTOP)
                poll(lambda: all((Path('/proc') / str(m['pid']) / 'stat').read_text()
                     .rsplit(')', 1)[1].split()[0] == 'T' for m in paused), daemon, fixture)
            (directory / 'go').touch()
            poll(lambda: int((directory / 'done').read_text()) == expected, daemon, fixture)
            resume(paused)
            (directory / 'exit').touch()
            fixture.wait(timeout=5)
            assert fixture.returncode == 0
            lttng('stop', session)
            lttng('destroy', session)
            created = False
            notifications = quality(directory / 'trace')
            result = {'loss_unit': loss_unit, 'fixture_pid': fixture.pid, 'fixture_exit': fixture.returncode,
                      'fixture_invocations': expected, 'discard_notifications': notifications}
            (directory / 'quality.json').write_text(json.dumps(result, indent=2) + '\n')
            if mode == 'healthy':
                assert not notifications, result
                rows, counts = convert_trace(str(directory / 'trace'))
                assert counts['attributed'] == expected and counts['unattributed'] == 0, counts
                assert len(rows) == expected and all(row['node'] == '/trace_loss_fixture' for row in rows)
                run(cli('trace', 'convert', directory / 'trace', '--out', directory / 'callbacks.ndjson'),
                    directory, 'convert')
                assert load_duration_rows(str(directory / 'callbacks.ndjson')) == rows
                result.update(conversion_counts=counts, cli_exit=0)
            else:
                expected_type = ('_DiscardedPacketsMessageConst' if packets
                                 else '_DiscardedEventsMessageConst')
                assert notifications and all(n['type'] == expected_type for n in notifications), notifications
                assert any(n['count'] is not None and n['count'] > 0 for n in notifications), notifications
                try:
                    convert_trace(str(directory / 'trace'))
                except TraceDataLoss as exc:
                    assert f'discarded {loss_unit}' in str(exc), str(exc)
                    result['conversion_error'] = str(exc)
                else:
                    raise AssertionError('lossy CTF became normal callback statistics')
                # Even a pre-existing valid output must remain byte-identical.
                sentinel = b'keep previous capture exactly\n'
                destination = directory / 'callbacks.ndjson'
                destination.write_bytes(sentinel)
                run(cli('trace', 'convert', directory / 'trace', '--out', destination),
                    directory, 'convert', expected=2)
                assert destination.read_bytes() == sentinel
                run([sys.executable, '-m', 'ros_graph_debugger.agent', '--no-browser',
                     '--trace-file', str(directory / 'trace')], directory, 'agent', expected=2)
                for name in ('convert', 'agent'):
                    text = (directory / (name + '.stderr.log')).read_text()
                    assert f'discarded {loss_unit}' in text, text
                result.update(cli_exit=2, agent_exit=2, existing_output_preserved=True)
            results[mode] = result
            (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            print(mode + ': counted real callbacks and trace loss contract passed', flush=True)
    finally:
        resume(paused)
        try:
            if created:
                lttng('destroy', session)
        finally:
            processes.close()
            groups = {child.pid for _, child in processes.children}
            remaining = []
            for path in Path('/proc').glob('[0-9]*/stat'):
                try:
                    fields = path.read_text().rsplit(')', 1)[1].split()
                    if int(fields[2]) in groups and fields[0] != 'Z':
                        remaining.append(int(path.parent.name))
                except (OSError, ValueError):
                    continue
            cleanup = {'active_owned_group_members': remaining,
                       'direct_child_returns': {name: c.poll() for name, c in processes.children}}
            (output / 'cleanup.json').write_text(json.dumps(cleanup, indent=2) + '\n')
            assert not remaining and all(c.returncode is not None for _, c in processes.children), cleanup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--loss-unit', choices=('events', 'packets'), default='events',
                        help='discarded events or overwritten packets; each has a counted normal control')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ['LTTNG_HOME'] = str(output / 'lttng-home')
    Path(os.environ['LTTNG_HOME']).mkdir()
    os.environ['ROS_DOMAIN_ID'] = '94' if args.loss_unit == 'packets' else '95'

    def terminate(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    def timeout(_signum, _frame):
        raise TimeoutError('trace loss scenario exceeded 90 seconds')

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 90)
    try:
        scenario(output, args.fixture.resolve(), args.loss_unit)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
