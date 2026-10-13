"""Re-read completed real CTF captures, including deliberately corrupted evidence."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

import bt2
from ros_graph_debugger.trace_convert import TraceDataLoss, convert_trace
from ros_graph_debugger.tracing import load_duration_rows
from run_scenario import cli, run


def read(path):
    return json.loads(path.read_text())


def hashes(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_file():
            with path.open('rb') as source:
                digest = hashlib.sha256()
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
                result[str(path.relative_to(directory))] = digest.hexdigest()
    return result


def scan(path, fixture_pid):
    notifications = []
    opened = defaultdict(list)
    pairs = Counter()
    starts = Counter()
    ends = Counter()
    unmatched_ends = 0
    before_loss = None
    for message in bt2.TraceCollectionMessageIterator(str(path)):
        if isinstance(message, (bt2._DiscardedEventsMessageConst,
                                bt2._DiscardedPacketsMessageConst)):
            notifications.append({'type': type(message).__name__, 'count': message.count})
            if before_loss is None:
                before_loss = pairs[fixture_pid]
        elif isinstance(message, bt2._EventMessageConst):
            event = message.event
            if event.name not in ('ros2:callback_start', 'ros2:callback_end'):
                continue
            context = event.common_context_field
            assert context is not None and 'vpid' in context, 'missing raw callback vpid'
            pid = int(context['vpid'])
            key = (pid, int(event.payload_field['callback']))
            stamp = message.default_clock_snapshot.ns_from_origin
            if event.name == 'ros2:callback_start':
                starts[pid] += 1
                opened[key].append(stamp)
            else:
                ends[pid] += 1
                if opened[key]:
                    assert stamp >= opened[key].pop(), 'negative raw callback duration'
                    pairs[pid] += 1
                else:
                    unmatched_ends += 1
    assert set(starts) == set(ends) == {fixture_pid}, 'raw callback process disagrees with fixture PID'
    return {'discard_notifications': notifications, 'raw_pairs': pairs[fixture_pid],
            'raw_starts': starts[fixture_pid], 'raw_ends': ends[fixture_pid],
            'unmatched_starts': sum(map(len, opened.values())), 'unmatched_ends': unmatched_ends,
            'pairs_before_first_loss': before_loss}


def audit(capture, output, loss_unit):
    proof = read(capture / 'results.json')
    assert set(proof) == {'healthy', 'loss'}, 'incomplete scenario results'
    cleanup = read(capture / 'cleanup.json')
    assert cleanup['active_owned_group_members'] == [], 'cleanup reports surviving children'
    returns = cleanup['direct_child_returns']
    assert set(returns) == {'sessiond', 'fixture-healthy', 'fixture-loss'}, 'incomplete child returns'
    assert all(value == 0 for value in returns.values()), 'child did not exit successfully'
    results = {}
    for mode in ('healthy', 'loss'):
        directory = capture / mode
        saved = proof[mode]
        expected = 5500 if loss_unit == 'packets' else (500 if mode == 'healthy' else 5000)
        assert saved['loss_unit'] == loss_unit, 'wrong loss unit'
        assert saved['fixture_invocations'] == int((directory / 'done').read_text()) == expected, 'fixture count mismatch'
        assert saved['fixture_exit'] == 0, 'fixture failed'
        raw = scan(directory / 'trace', saved['fixture_pid'])
        notifications = raw['discard_notifications']
        assert notifications == saved['discard_notifications'] == read(directory / 'quality.json')['discard_notifications'], 'discard evidence differs from raw CTF'
        if loss_unit == 'packets':
            warm = read(directory / 'warm-quality.json')
            assert int((directory / 'warm-done').read_text()) == 500, 'warm count mismatch'
            assert warm['discard_notifications'] == [], 'warm capture reported loss'
            assert warm['conversion_counts']['attributed'] == 500 and warm['conversion_counts']['unattributed'] == 0, 'warm conversion mismatch'
        destination = output / (mode + '.ndjson')
        if mode == 'healthy':
            assert not notifications, 'normal capture reported loss'
            assert raw['raw_pairs'] == raw['raw_starts'] == raw['raw_ends'] == expected, 'normal raw count mismatch'
            assert raw['unmatched_starts'] == raw['unmatched_ends'] == 0, 'normal capture has unmatched callbacks'
            rows, counts = convert_trace(str(directory / 'trace'))
            assert counts == saved['conversion_counts'], 'conversion counts changed'
            assert counts['attributed'] == expected and counts['unattributed'] == 0, 'normal attribution mismatch'
            assert len(rows) == expected and all(row['node'] == '/trace_loss_fixture' for row in rows), 'normal callback attribution mismatch'
            assert load_duration_rows(str(directory / 'callbacks.ndjson')) == rows, 'saved callbacks differ from real CTF'
            assert saved['cli_exit'] == 0, 'saved healthy CLI exit mismatch'
            run(cli('trace', 'convert', directory / 'trace', '--out', destination), output, mode + '-convert')
            assert load_duration_rows(str(destination)) == rows, 'repeated CLI export differs'
            raw['conversion_counts'] = counts
        else:
            kind = '_DiscardedPacketsMessageConst' if loss_unit == 'packets' else '_DiscardedEventsMessageConst'
            assert notifications and all(n['type'] == kind for n in notifications), 'expected real discard type absent'
            assert any(n['count'] is not None and n['count'] > 0 for n in notifications), 'no positive discard count'
            assert 0 < raw['raw_pairs'] < expected, 'loss control did not lose counted callbacks'
            if loss_unit == 'packets':
                assert raw['pairs_before_first_loss'] == 500, 'warm raw callbacks before packet gap mismatch'
            try:
                convert_trace(str(directory / 'trace'))
            except TraceDataLoss as exc:
                assert str(exc) == saved['conversion_error'], 'conversion error changed'
            else:
                raise AssertionError('lossy raw capture accepted')
            assert saved['cli_exit'] == saved['agent_exit'] == 2, 'saved rejection exit mismatch'
            sentinel = b'keep previous capture exactly\n'
            assert saved['existing_output_preserved'] is True and (directory / 'callbacks.ndjson').read_bytes() == sentinel, 'saved output was overwritten'
            destination.write_bytes(sentinel)
            run(cli('trace', 'convert', directory / 'trace', '--out', destination), output, mode + '-convert', expected=2)
            assert destination.read_bytes() == sentinel, 'repeated CLI overwrote existing output'
            run([sys.executable, '-m', 'ros_graph_debugger.agent', '--no-browser', '--trace-file', str(directory / 'trace')], output, 'loss-agent', expected=2)
            for name in ('convert', 'agent'):
                assert f'discarded {loss_unit}' in (directory / (name + '.stderr.log')).read_text(), 'saved rejection log lacks loss evidence'
                assert f'discarded {loss_unit}' in (output / ('loss-' + name + '.stderr.log')).read_text(), 'repeated rejection log lacks loss evidence'
        results[mode] = raw
    return {'loss_unit': loss_unit, 'results': results, 'cleanup': cleanup}


def negative_controls(capture, output, loss_unit):
    def edit_json(path, mutate):
        value = read(path)
        mutate(value)
        path.write_text(json.dumps(value))

    controls = [
        ('hidden-loss', 'discard evidence differs from raw CTF', lambda p: edit_json(p / 'results.json', lambda v: v['loss'].update(discard_notifications=[]))),
        ('false-count', 'fixture count mismatch', lambda p: edit_json(p / 'results.json', lambda v: v['healthy'].update(fixture_invocations=1))),
        ('false-success', 'saved rejection exit mismatch', lambda p: edit_json(p / 'results.json', lambda v: v['loss'].update(cli_exit=0))),
        ('overwritten-output', 'saved output was overwritten', lambda p: (p / 'loss/callbacks.ndjson').write_bytes(b'corrupt\n')),
        ('truncated-export', 'saved callbacks differ from real CTF', lambda p: (p / 'healthy/callbacks.ndjson').write_bytes(b'')),
        ('surviving-child', 'cleanup reports surviving children', lambda p: edit_json(p / 'cleanup.json', lambda v: v.update(active_owned_group_members=[1]))),
    ]
    results = []
    for name, reason, mutate in controls:
        with tempfile.TemporaryDirectory(prefix=name + '-', dir=output) as temporary:
            temporary = Path(temporary)
            copied = temporary / 'capture'
            shutil.copytree(capture, copied)
            mutate(copied)
            target = output / 'negative-controls' / name
            target.mkdir(parents=True)
            try:
                audit(copied, target, loss_unit)
            except AssertionError as exc:
                assert str(exc) == reason, (name, str(exc))
                results.append({'name': name, 'rejected': True, 'reason': str(exc)})
            else:
                raise AssertionError('corrupted evidence accepted: ' + name)
            (target / 'result.json').write_text(json.dumps(results[-1], indent=2) + '\n')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--loss-unit', choices=('events', 'packets'), required=True)
    args = parser.parse_args()
    capture, output = args.capture.resolve(), args.output.resolve()
    assert capture.is_dir() and output != capture and capture not in output.parents, 'audit output must be outside capture'
    output.mkdir(parents=True, exist_ok=False)
    before = hashes(capture)
    try:
        result = audit(capture, output, args.loss_unit)
        result['negative_controls'] = negative_controls(capture, output, args.loss_unit)
    finally:
        assert hashes(capture) == before, 'audit changed original evidence'
        (output / 'capture-sha256.json').write_text(json.dumps(before, indent=2) + '\n')
    result.update(original_evidence_unchanged=True, original_files=len(before), python=sys.version)
    (output / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f'{args.loss_unit}: independent raw CTF audit and {len(result["negative_controls"])} corruption controls passed')


if __name__ == '__main__':
    main()
