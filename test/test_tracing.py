"""Synthetic Tier C trace source + real callback-duration aggregation."""

import json

from ros_graph_debugger.model import NodeInfo
from ros_graph_debugger.tracing import (
    aggregate_callback_durations,
    callbacks_from_trace_file,
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


def test_load_trace_file_tolerates_garbage(tmp_path):
    p = tmp_path / 'trace.ndjson'
    p.write_text(
        json.dumps({'node': '/n', 'topic': '/t', 'duration_ms': 12.0}) + '\n'
        + 'not json\n'
        + json.dumps({'node': '/n', 'topic': '/t', 'duration_ms': 18.0}) + '\n')
    cbs = callbacks_from_trace_file(str(p))
    assert len(cbs) == 1
    assert cbs[0].count == 2 and cbs[0].node == '/n'
