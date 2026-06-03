"""Synthetic Tier C trace source + real callback-duration aggregation."""

import json

from ros_graph_debugger.model import NodeInfo
from ros_graph_debugger.tracing import (
    aggregate_callback_durations,
    callbacks_from_trace_file,
    pair_callback_events,
    rows_with_owners,
    synthesize_callbacks,
)


def test_one_callback_per_subscription():
    nodes = [
        NodeInfo(id='/detector', name='detector', subscribers=['/image']),
        NodeInfo(id='/tracker', name='tracker', subscribers=['/objects']),
        NodeInfo(id='/cam', name='cam', subscribers=[]),  # publisher only
    ]
    cbs = synthesize_callbacks(nodes)
    assert {c.node for c in cbs} == {'/detector', '/tracker'}  # cam has no sub callback
    assert all(c.topic and c.p95_ms is not None for c in cbs)


def test_slow_node_callback_spikes():
    nodes = [NodeInfo(id='/detector', name='detector', subscribers=['/image']),
             NodeInfo(id='/tracker', name='tracker', subscribers=['/objects'])]
    cbs = synthesize_callbacks(nodes, slow_node='/detector', slow_p95_ms=210.0)
    det = next(c for c in cbs if c.node == '/detector')
    trk = next(c for c in cbs if c.node == '/tracker')
    assert det.p95_ms > 200 and trk.p95_ms < 50


def test_accepts_plain_dicts():
    nodes = [{'id': '/a', 'subscribers': ['/t']}]
    cbs = synthesize_callbacks(nodes)
    assert cbs[0].node == '/a' and cbs[0].topic == '/t'


def test_aggregate_durations_computes_stats():
    rows = [{'node': '/detector', 'topic': '/image', 'duration_ms': d}
            for d in (10, 20, 30, 40, 200)]
    cbs = aggregate_callback_durations(rows)
    assert len(cbs) == 1
    c = cbs[0]
    assert c.node == '/detector' and c.topic == '/image'
    assert c.callback == 'sub /image'  # derived from topic
    assert c.count == 5
    assert c.max_ms == 200.0
    assert c.mean_ms == 60.0
    assert c.p95_ms == 200.0  # nearest-rank top sample


def test_aggregate_groups_and_skips_bad_rows():
    rows = [
        {'node': '/a', 'topic': '/x', 'duration_ms': 5.0},
        {'node': '/a', 'topic': '/x', 'duration_ms': 15.0},
        {'node': '/b', 'callback': 'timer', 'duration_ms': 1.0},
        {'node': '/a', 'topic': '/x'},          # no duration -> skipped
        {'duration_ms': 9.0},                    # no node -> skipped
    ]
    cbs = {(c.node, c.callback): c for c in aggregate_callback_durations(rows)}
    assert set(cbs) == {('/a', 'sub /x'), ('/b', 'timer')}
    assert cbs[('/a', 'sub /x')].count == 2


def test_pair_callback_events_computes_durations():
    events = [
        {'kind': 'start', 'handle': 0xA, 't_ns': 1_000_000},
        {'kind': 'end',   'handle': 0xA, 't_ns': 6_000_000},   # 5 ms
        {'kind': 'start', 'handle': 0xB, 't_ns': 2_000_000},
        {'kind': 'end',   'handle': 0xB, 't_ns': 2_500_000},   # 0.5 ms
    ]
    rows = pair_callback_events(events)
    durs = {r['handle']: r['duration_ms'] for r in rows}
    assert durs == {0xA: 5.0, 0xB: 0.5}


def test_pair_handles_reentrancy_and_unmatched():
    events = [
        {'kind': 'start', 'handle': 1, 't_ns': 0},
        {'kind': 'start', 'handle': 1, 't_ns': 1_000_000},   # nested
        {'kind': 'end',   'handle': 1, 't_ns': 3_000_000},   # pairs inner (2 ms)
        {'kind': 'end',   'handle': 1, 't_ns': 5_000_000},   # pairs outer (5 ms)
        {'kind': 'end',   'handle': 9, 't_ns': 9},           # unmatched end -> ignored
        {'kind': 'start', 'handle': 7, 't_ns': 0},           # unmatched start -> dropped
    ]
    durs = sorted(r['duration_ms'] for r in pair_callback_events(events))
    assert durs == [2.0, 5.0]


def test_rows_with_owners_resolves_and_drops_unknown():
    paired = [{'handle': 0xA, 'duration_ms': 5.0},
              {'handle': 0xFF, 'duration_ms': 9.0}]  # no owner
    owners = {0xA: {'node': '/detector', 'callback': 'sub /image', 'topic': '/image'}}
    rows = rows_with_owners(paired, owners)
    assert rows == [{'node': '/detector', 'callback': 'sub /image',
                     'topic': '/image', 'duration_ms': 5.0}]
    # End-to-end: paired durations -> owners -> aggregated stats.
    owners2 = {1: {'node': '/n', 'topic': '/t'}}
    paired2 = [{'handle': 1, 'duration_ms': 10.0}, {'handle': 1, 'duration_ms': 20.0}]
    cbs = aggregate_callback_durations(rows_with_owners(paired2, owners2))
    assert cbs[0].node == '/n' and cbs[0].count == 2 and cbs[0].mean_ms == 15.0


def test_load_trace_file_tolerates_garbage(tmp_path):
    p = tmp_path / 'trace.ndjson'
    p.write_text(
        json.dumps({'node': '/n', 'topic': '/t', 'duration_ms': 12.0}) + '\n'
        + 'not json\n'
        + json.dumps({'node': '/n', 'topic': '/t', 'duration_ms': 18.0}) + '\n')
    cbs = callbacks_from_trace_file(str(p))
    assert len(cbs) == 1
    assert cbs[0].count == 2 and cbs[0].node == '/n'
