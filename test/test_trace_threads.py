"""Reentrant callbacks need both process and execution-thread identity."""
from types import SimpleNamespace

import pytest

from ros_graph_debugger.tracing import pair_callback_events, trace_events_to_rows


def test_crossing_threads_pair_their_own_starts():
    marks = [
        {'kind': 'start', 'handle': 7, 'tid': 10, 't_ns': 0},
        {'kind': 'start', 'handle': 7, 'tid': 20, 't_ns': 100_000_000},
        {'kind': 'end', 'handle': 7, 'tid': 10, 't_ns': 200_000_000},
        {'kind': 'end', 'handle': 7, 'tid': 20, 't_ns': 300_000_000},
    ]
    assert [r['duration_ms'] for r in pair_callback_events(marks)] == [200, 200]


def test_nested_same_thread_and_unmatched_other_thread():
    marks = [
        {'kind': 'start', 'handle': 7, 'tid': 10, 't_ns': 0},
        {'kind': 'start', 'handle': 7, 'tid': 10, 't_ns': 1_000_000},
        {'kind': 'end', 'handle': 7, 'tid': 20, 't_ns': 2_000_000},
        {'kind': 'end', 'handle': 7, 'tid': 10, 't_ns': 3_000_000},
        {'kind': 'end', 'handle': 7, 'tid': 10, 't_ns': 5_000_000},
    ]
    assert [r['duration_ms'] for r in pair_callback_events(marks)] == [2, 5]


def test_thread_pairing_keeps_process_scoped_ownership():
    events = []
    def add(name, pid, stamp, fields, tid=None):
        events.append(dict(name='ros2:' + name, pid=pid, tid=tid, t_ns=stamp, fields=fields))
    for pid, node in ((1, 'first'), (2, 'second')):
        add('rcl_node_init', pid, 0, dict(node_handle=1, node_name=node, namespace='/'))
        add('rcl_timer_init', pid, 1, dict(timer_handle=2, period=1_000_000))
        add('rclcpp_timer_link_node', pid, 2, dict(timer_handle=2, node_handle=1))
        add('rclcpp_timer_callback_added', pid, 3, dict(timer_handle=2, callback=7))
    add('callback_start', 1, 10_000_000, dict(callback=7), 10)
    add('callback_start', 1, 20_000_000, dict(callback=7), 20)
    add('callback_start', 2, 25_000_000, dict(callback=7), 10)
    add('callback_end', 1, 30_000_000, dict(callback=7), 10)
    add('callback_end', 2, 35_000_000, dict(callback=7), 10)
    add('callback_end', 1, 40_000_000, dict(callback=7), 20)
    rows, counts = trace_events_to_rows(events)
    assert [(r['node'], r['duration_ms']) for r in rows] == [('/first', 20), ('/second', 10), ('/first', 20)]
    assert counts['attributed'] == 3 and counts['unattributed'] == 0


@pytest.mark.parametrize('context,tid', [({'vpid': 42, 'vtid': 123}, 123), ({'vpid': 42}, None), (None, None)])
def test_ctf_reader_preserves_thread_context_without_inventing_it(monkeypatch, context, tid):
    import ros_graph_debugger.trace_convert as converter
    class Event:
        event = SimpleNamespace(name='ros2:callback_start', common_context_field=context, payload_field={'callback': 7})
        default_clock_snapshot = SimpleNamespace(ns_from_origin=100)
    module = SimpleNamespace(_EventMessageConst=Event, _DiscardedEventsMessageConst=type('LostEvents', (), {}), _DiscardedPacketsMessageConst=type('LostPackets', (), {}), TraceCollectionMessageIterator=lambda path: iter([Event()]))
    monkeypatch.setattr(converter, '_import_bt2', lambda: module)
    row, = converter.read_ctf_events('capture')
    assert row['tid'] == tid
    assert row['pid'] == (42 if context else None)
