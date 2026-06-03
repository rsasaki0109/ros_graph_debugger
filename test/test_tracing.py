"""Synthetic Tier C trace source."""

from ros_graph_debugger.model import NodeInfo
from ros_graph_debugger.tracing import synthesize_callbacks


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
