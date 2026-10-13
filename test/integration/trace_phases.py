"""Test-only audit of same-CTF-clock assertion-command envelopes.

These intervals do not align ROS/header clocks or establish current health,
message causality, end-to-end timing or capture/DDS completeness.
"""
import argparse
from collections import defaultdict
from dataclasses import asdict
import json
from pathlib import Path
import uuid

from ros_graph_debugger.tracing import aggregate_callback_durations, resolve_callback_owners

PHASES = ('healthy', 'missing-imu', 'recovered')
PROVIDER = 'lttng_ust_tracef:event'


def known_clock(clock):
    prefix = 'ctf_raw_1ghz:'
    if not isinstance(clock, str) or not clock.startswith(prefix):
        return False
    try:
        return str(uuid.UUID(clock[len(prefix):])) == clock[len(prefix):]
    except ValueError:
        return False


def phase_windows(events, run_id, marker_pid):
    assert type(marker_pid) is int and marker_pid > 0, 'unknown marker process'
    markers = []
    for event in events:
        if event['name'] != PROVIDER:
            continue
        text = event['fields'].get('msg', '')
        if not isinstance(text, str) or not text.startswith('RGD_PHASE '):
            continue
        marker = json.loads(text.removeprefix('RGD_PHASE '))
        if marker.get('run_id') != run_id:
            continue
        assert event['pid'] == marker_pid, 'marker process identity mismatch'
        assert type(event['t_ns']) is int and known_clock(event.get('clock')), 'unknown marker clock'
        markers.append(dict(marker, t_ns=event['t_ns'], clock=event['clock']))
    markers.sort(key=lambda m: m['t_ns'])
    expected = [(phase, boundary) for phase in PHASES for boundary in ('begin', 'end')]
    assert len(markers) == len(expected), 'missing or duplicate phase markers'
    assert [(m.get('phase'), m.get('boundary')) for m in markers] == expected, 'phase marker order'
    assert all(type(m.get('sequence')) is int for m in markers), 'invalid marker sequence type'
    assert [m.get('sequence') for m in markers] == list(range(1, 7)), 'marker sequence'
    clocks = {m['clock'] for m in markers}
    assert len(clocks) == 1, 'mixed marker clocks'
    assert all(a['t_ns'] < b['t_ns'] for a, b in zip(markers, markers[1:])), 'overlapping marker boundaries'
    return [dict(phase=phase, begin_ns=markers[2*i]['t_ns'], end_ns=markers[2*i+1]['t_ns'],
                 clock=markers[2*i]['clock']) for i, phase in enumerate(PHASES)]


def audit_phases(events, run_id, marker_pid):
    windows = phase_windows(events, run_id, marker_pid)
    owners = resolve_callback_owners(events)
    pending = defaultdict(list)
    rows = []
    for event in sorted(events, key=lambda e: e['t_ns']):
        if event['name'] not in ('ros2:callback_start', 'ros2:callback_end'):
            continue
        assert type(event['t_ns']) is int and known_clock(event.get('clock')), 'unknown callback clock'
        assert type(event['pid']) is int and event['pid'] > 0, 'unknown callback process'
        key = (event['pid'], event['fields'].get('callback'), event['clock'])
        if event['name'] == 'ros2:callback_start':
            pending[key].append(event['t_ns'])
        else:
            assert pending[key], 'unmatched callback end'
            start = pending[key].pop()
            assert event['t_ns'] >= start, 'reversed callback interval'
            owner = owners.get(key[:2])
            assert owner, 'unattributed callback invocation'
            rows.append(dict(owner, start_ns=start, end_ns=event['t_ns'], clock=event['clock'],
                             duration_ms=round((event['t_ns']-start)/1e6, 3), source='trace'))
    assert not any(pending.values()), 'unmatched callback start'
    grouped = []
    for window in windows:
        # A declared clock mismatch is an error, not a silent empty interval.
        assert all(r['clock'] == window['clock'] for r in rows), 'callback/marker clock mismatch'
        inside = [r for r in rows if window['begin_ns'] <= r['start_ns']
                  and r['end_ns'] <= window['end_ns']]
        crossing = [r for r in rows if not (window['begin_ns'] <= r['start_ns']
                    and r['end_ns'] <= window['end_ns']) and r['start_ns'] < window['end_ns']
                    and r['end_ns'] > window['begin_ns']]
        grouped.append(dict(window, invocations=len(inside), boundary_crossing=len(crossing),
                            callbacks=[asdict(c) for c in aggregate_callback_durations(inside)]))
    return dict(run_id=run_id, marker_pid=marker_pid, markers=6, total_paired=len(rows),
                phases=grouped, scope='Same-CTF-clock raw 1 GHz ticks (nanoseconds), without per-stream epoch offsets; '
                'assertion command envelopes, wholly contained invocations only. '
                'Not settled sample windows, ROS/header alignment, completeness, current health or causal timing.')


def read_events(path):
    from ros_graph_debugger.trace_convert import _import_bt2, _value
    bt2 = _import_bt2()
    for msg in bt2.TraceCollectionMessageIterator(str(path)):
        assert 'Discarded' not in type(msg).__name__, 'trace reports discarded events/packets'
        if type(msg) is not bt2._EventMessageConst:
            continue
        event = msg.event
        if not (event.name.startswith('ros2:') or event.name == PROVIDER):
            continue
        snapshot = msg.default_clock_snapshot
        clock_class = snapshot.clock_class
        assert clock_class.frequency == 10**9, 'phase audit requires a declared 1 GHz clock'
        clock_uuid = clock_class.uuid
        context = event.common_context_field
        payload = event.payload_field
        yield dict(name=event.name, pid=int(context['vpid']) if context is not None and 'vpid' in context else None,
                   # Different processes can declare slightly different epoch
                   # offsets for the same raw clock. Do not combine their
                   # ns_from_origin coordinates for boundary membership.
                   t_ns=snapshot.value,
                   clock=f'ctf_raw_1ghz:{clock_uuid}' if clock_uuid is not None else None,
                   clock_spec=dict(frequency_hz=clock_class.frequency,
                                   offset_seconds=clock_class.offset.seconds,
                                   offset_cycles=clock_class.offset.cycles),
                   fields={k: _value(v) for k, v in payload.items()} if payload is not None else {})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--marker-pid', type=int, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    assert not args.out.exists(), '--out must be fresh'
    result = audit_phases(list(read_events(args.trace)), args.run_id, args.marker_pid)
    args.out.write_text(json.dumps(result, indent=2)+'\n')
