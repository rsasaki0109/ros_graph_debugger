"""Validate overlapping real callbacks against raw per-thread CTF pairs."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess

import bt2
from ros_graph_debugger.trace_convert import (
    callbacks_from_trace_dir, convert_trace, read_ctf_events,
)
from ros_graph_debugger.tracing import aggregate_callback_durations, load_duration_rows
from run_scenario import Processes, cli, run
from run_trace_loss_scenario import quality, poll


def raw_pairs(capture, fixture_pid):
    marks, pending, rows = [], {}, []
    for message in bt2.TraceCollectionMessageIterator(str(capture)):
        if not isinstance(message, bt2._EventMessageConst):
            continue
        event = message.event
        if event.name not in ('ros2:callback_start', 'ros2:callback_end'):
            continue
        pid = int(event.common_context_field['vpid'])
        tid = int(event.common_context_field['vtid'])
        handle = int(event.payload_field['callback'])
        stamp = message.default_clock_snapshot.ns_from_origin
        assert pid == fixture_pid
        marks.append(dict(name=event.name, pid=pid, tid=tid, handle=handle, t_ns=stamp))
        key = (pid, tid, handle)
        if event.name == 'ros2:callback_start':
            assert key not in pending, 'unexpected same-thread overlap'
            pending[key] = stamp
        else:
            rows.append(dict(tid=tid, duration_ms=(stamp - pending.pop(key)) / 1e6))
    assert not pending and len(rows) == 2 and len({m['handle'] for m in marks}) == 1
    assert [m['name'] for m in marks] == ['ros2:callback_start'] * 2 + ['ros2:callback_end'] * 2
    assert marks[0]['tid'] == marks[2]['tid'] and marks[1]['tid'] == marks[3]['tid']
    return marks, rows


def scenario(root, executable):
    capture = root / 'capture'
    capture.mkdir()
    processes = Processes(root)
    created = False

    def lttng(*args):
        result = subprocess.run(['lttng', '--no-sessiond', *args],
                                capture_output=True, text=True, timeout=10)
        with (root / 'lttng.log').open('a') as log:
            log.write(json.dumps(dict(args=args, exit=result.returncode,
                                     stdout=result.stdout, stderr=result.stderr)) + '\n')
        assert result.returncode == 0, result.stderr

    try:
        daemon = processes.start('sessiond', ['lttng-sessiond', '--no-kernel'])
        poll(lambda: subprocess.run(['lttng', '--no-sessiond', 'list'],
             capture_output=True, timeout=2).returncode == 0, daemon)
        lttng('create', 'rgd-reentrant', '--output', str(capture))
        created = True
        lttng('enable-channel', '--userspace', '--buffers-pid',
              '--subbuf-size=1048576', '--num-subbuf=4', 'callbacks')
        lttng('enable-event', '--userspace', '--channel', 'callbacks', 'ros2:*')
        for context in ('vpid', 'vtid'):
            lttng('add-context', '--userspace', '--channel', 'callbacks', '--type', context)
        lttng('start')
        fixture = processes.start('fixture', [str(executable), str(root / 'fixture.json')])
        fixture.wait(timeout=25)
        assert fixture.returncode == 0
        lttng('stop')
        lttng('destroy')
        created = False
        assert not quality(capture)
        sample = json.loads((root / 'fixture.json').read_text())
        assert sample['status'] == 0 and len({s['tid'] for s in sample['samples']}) == 2
        marks, raw = raw_pairs(capture, fixture.pid)
        reference = {s['tid']: s['duration_ms'] for s in sample['samples']}
        assert all(abs(r['duration_ms'] - reference[r['tid']]) < 10 for r in raw), (raw, sample)
        rows, counts = convert_trace(str(capture))
        assert counts['attributed'] == 2 and counts['unattributed'] == 0
        errors = [abs(a - b) for a, b in zip(sorted(r['duration_ms'] for r in raw),
                                            sorted(r['duration_ms'] for r in rows))]
        assert max(errors) < 0.001, ('conversion differs from raw thread pairs', rows, raw)
        assert all(e['tid'] in reference for e in read_ctf_events(str(capture))
                   if e['name'] in ('ros2:callback_start', 'ros2:callback_end'))
        run(cli('trace', 'convert', capture, '--out', root / 'callbacks.ndjson'), root, 'convert')
        assert load_duration_rows(str(root / 'callbacks.ndjson')) == rows
        stats = aggregate_callback_durations(rows)
        assert callbacks_from_trace_dir(str(capture)) == stats
        assert len(stats) == 1 and stats[0].count == 2 and stats[0].source == 'trace'
        assert stats[0].node == '/trace_reentrant_fixture' and stats[0].topic == '/trace_reentrant/input'
        proof = dict(fixture=sample, fixture_pid=fixture.pid, raw_marks=marks,
                     raw_thread_paired=raw, current_rows=rows, counts=counts,
                     duration_errors_ms=errors, discard_notifications=[])
        (root / 'results.json').write_text(json.dumps(proof, indent=2) + '\n')
        print('real reentrant callbacks: raw thread pairs, CLI and agent loader matched', flush=True)
    finally:
        try:
            if created:
                lttng('destroy')
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
            cleanup = dict(direct_child_returns={n: c.poll() for n, c in processes.children},
                           active_owned_group_members=remaining)
            (root / 'cleanup.json').write_text(json.dumps(cleanup, indent=2) + '\n')
            assert not remaining and all(v == 0 for v in cleanup['direct_child_returns'].values()), cleanup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ['LTTNG_HOME'] = str(root / 'lttng-home')
    Path(os.environ['LTTNG_HOME']).mkdir()
    os.environ['ROS_DOMAIN_ID'] = '96'

    def terminate(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    def timeout(_signum, _frame):
        raise TimeoutError('reentrant trace scenario exceeded 60 seconds')

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 60)
    try:
        scenario(root, args.fixture.resolve())
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
