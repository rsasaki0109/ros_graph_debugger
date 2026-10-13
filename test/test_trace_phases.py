"""Constructed CTF event controls for phase envelopes; no live-data claim."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

spec = importlib.util.spec_from_file_location('trace_phases', Path(__file__).parent / 'integration/trace_phases.py')
phases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phases)

CLOCK = 'ctf_raw_1ghz:4f3ccacf-8e09-45a6-8083-1761c14b6027'
OTHER = 'ctf_raw_1ghz:17928b61-b18e-4975-83f9-dc0c2648ad5f'
RUN = 'fixture-run'


def event(name, t, fields, pid=10, clock=CLOCK):
    return dict(name=name, t_ns=t, pid=pid, clock=clock, fields=fields)


def events():
    result = [event('ros2:rcl_node_init', 0, dict(node_handle=1, node_name='n', namespace='/')),
              event('ros2:rcl_subscription_init', 1, dict(subscription_handle=2, node_handle=1, topic_name='/imu')),
              event('ros2:rclcpp_subscription_init', 2, dict(subscription=3, subscription_handle=2)),
              event('ros2:rclcpp_subscription_callback_added', 3, dict(callback=4, subscription=3))]
    for i, (phase, boundary) in enumerate((p, b) for p in phases.PHASES for b in ('begin', 'end')):
        marker = dict(run_id=RUN, sequence=i+1, phase=phase, boundary=boundary)
        result.append(event(phases.PROVIDER, 10*(i+1), dict(msg='RGD_PHASE '+json.dumps(marker)), pid=99))
    for begin, end in ((9, 11), (12, 18), (32, 38), (52, 58), (65, 67)):
        result += [event('ros2:callback_start', begin, dict(callback=4)),
                   event('ros2:callback_end', end, dict(callback=4))]
    return result


def test_exact_phase_bounds_and_wholly_contained_invocations():
    result = phases.audit_phases(events(), RUN, 99)
    assert result['total_paired'] == 5 and result['markers'] == 6
    assert [(w['begin_ns'], w['end_ns']) for w in result['phases']] == [(10, 20), (30, 40), (50, 60)]
    assert [w['invocations'] for w in result['phases']] == [1, 1, 1]
    assert [w['boundary_crossing'] for w in result['phases']] == [1, 0, 0]
    assert all(w['clock'] == CLOCK and w['callbacks'][0]['count'] == 1 for w in result['phases'])
    assert 'Not settled sample windows' in result['scope']


@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'wrong_sequence', 'bool_sequence',
                                     'wrong_order', 'wrong_pid', 'mixed_clock', 'unknown_clock', 'equal_time'])
def test_phase_marker_failures_are_not_silently_accepted(failure):
    rows = events()
    marker = rows[4]
    data = json.loads(marker['fields']['msg'].removeprefix('RGD_PHASE '))
    if failure == 'missing': rows.remove(marker)
    elif failure == 'duplicate': rows.append(copy.deepcopy(marker))
    elif failure == 'wrong_sequence': data['sequence'] = 2
    elif failure == 'bool_sequence': data['sequence'] = True
    elif failure == 'wrong_order': data['boundary'] = 'end'
    elif failure == 'wrong_pid': marker['pid'] = 98
    elif failure == 'mixed_clock': marker['clock'] = OTHER
    elif failure == 'unknown_clock': marker['clock'] = None
    elif failure == 'equal_time': marker['t_ns'] = rows[5]['t_ns']
    marker['fields']['msg'] = 'RGD_PHASE '+json.dumps(data)
    with pytest.raises(AssertionError): phases.audit_phases(rows, RUN, 99)


@pytest.mark.parametrize('failure', ['unmatched_start', 'unmatched_end', 'unknown_owner',
                                     'unknown_clock', 'different_clock', 'float_timestamp', 'unknown_pid'])
def test_callback_evidence_failures_are_not_silently_accepted(failure):
    rows = events()
    start, end = rows[-2:]
    if failure == 'unmatched_start': rows.remove(end)
    elif failure == 'unmatched_end': rows.remove(start)
    elif failure == 'unknown_owner':
        start['fields']['callback'] = end['fields']['callback'] = 987
    elif failure == 'unknown_clock': start['clock'] = None
    elif failure == 'different_clock': start['clock'] = end['clock'] = OTHER
    elif failure == 'float_timestamp': start['t_ns'] = float(start['t_ns'])
    elif failure == 'unknown_pid': start['pid'] = None
    with pytest.raises(AssertionError): phases.audit_phases(rows, RUN, 99)


def test_other_run_markers_are_not_used_to_complete_missing_markers():
    rows = events()
    marker = rows[4]
    data = json.loads(marker['fields']['msg'].removeprefix('RGD_PHASE '))
    data['run_id'] = 'another-run'
    marker['fields']['msg'] = 'RGD_PHASE '+json.dumps(data)
    with pytest.raises(AssertionError, match='missing'): phases.audit_phases(rows, RUN, 99)


def test_zero_callback_interval_is_valid_and_not_missing():
    rows = events()
    rows[-1]['t_ns'] = rows[-2]['t_ns']
    result = phases.audit_phases(rows, RUN, 99)
    assert result['total_paired'] == 5


def test_epoch_offsets_do_not_change_raw_clock_boundary_membership():
    rows = events()
    for row in rows:
        row['clock_spec'] = dict(frequency_hz=10**9, offset_cycles=5 if row['pid']==99 else 9)
    result = phases.audit_phases(rows, RUN, 99)
    assert [w['invocations'] for w in result['phases']] == [1, 1, 1]
    assert [w['boundary_crossing'] for w in result['phases']] == [1, 0, 0]


@pytest.mark.parametrize('pid', [None, 0, True])
def test_missing_marker_process_identity_is_rejected(pid):
    with pytest.raises(AssertionError, match='unknown marker process'):
        phases.audit_phases(events(), RUN, pid)


def fake_reader(monkeypatch, frequency=10**9, discarded=False):
    import ros_graph_debugger.trace_convert as tc
    class Snapshot:
        value = 17
        clock_class = SimpleNamespace(uuid=uuid.UUID(CLOCK.split(':',1)[1]),
                                     frequency=frequency,
                                     offset=SimpleNamespace(seconds=2, cycles=3))
        @property
        def ns_from_origin(self):
            raise AssertionError('epoch coordinate must not be used')
    class EventMessage:
        default_clock_snapshot = Snapshot()
        event = SimpleNamespace(name=phases.PROVIDER, common_context_field={'vpid':99},
                                payload_field={'msg':'RGD_PHASE {}'})
    class DiscardedEventsMessage:
        pass
    messages = [DiscardedEventsMessage()] if discarded else [EventMessage()]
    bt2 = SimpleNamespace(_EventMessageConst=EventMessage,
                          TraceCollectionMessageIterator=lambda _:iter(messages))
    monkeypatch.setattr(tc,'_import_bt2',lambda:bt2)


def test_reader_uses_raw_ticks_and_retains_offset_metadata(monkeypatch):
    fake_reader(monkeypatch)
    [row] = phases.read_events('constructed-source')
    assert row['t_ns'] == 17 and row['clock'] == CLOCK
    assert row['clock_spec'] == dict(frequency_hz=10**9,offset_seconds=2,offset_cycles=3)


@pytest.mark.parametrize('frequency',[10**6,2*10**9])
def test_unsupported_clock_frequencies_are_rejected(monkeypatch,frequency):
    fake_reader(monkeypatch,frequency=frequency)
    with pytest.raises(AssertionError,match='1 GHz'):
        list(phases.read_events('constructed-source'))


def test_discard_notifications_fail_the_audit(monkeypatch):
    fake_reader(monkeypatch,discarded=True)
    with pytest.raises(AssertionError,match='discarded'):
        list(phases.read_events('constructed-source'))
