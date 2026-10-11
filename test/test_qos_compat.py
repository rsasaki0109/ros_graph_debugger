"""QoS request/offer compatibility across reliability, durability, deadline,
liveliness kind and lease — the pairs DDS refuses to match."""

import time

import pytest

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.model import QoSInfo, RuntimeGraphStore, TopicInfo
from ros_graph_debugger.qos_compat import (
    detect_qos_status,
    incompatibilities,
    topic_incompatibilities,
)


def _ep(kind, node='/n', **kw):
    base = dict(reliability='reliable', durability='volatile',
                liveliness='automatic')
    base.update(kw)
    return QoSInfo(node=node, endpoint_type=kind, **base)


@pytest.mark.parametrize('pub, sub, policy', [
    (dict(reliability='best_effort'), dict(reliability='reliable'), 'reliability'),
    (dict(durability='volatile'), dict(durability='transient_local'), 'durability'),
    (dict(deadline_ms=200.0), dict(deadline_ms=100.0), 'deadline'),
    (dict(), dict(deadline_ms=100.0), 'deadline'),            # pub: no deadline
    (dict(liveliness='automatic'), dict(liveliness='manual_by_topic'), 'liveliness'),
    (dict(liveliness_lease_ms=2000.0), dict(liveliness_lease_ms=1000.0),
     'liveliness lease'),
])
def test_each_policy_is_detected(pub, sub, policy):
    reasons = incompatibilities(_ep('publisher', **pub), _ep('subscriber', **sub))
    assert len(reasons) == 1 and reasons[0].startswith(policy + ':')


@pytest.mark.parametrize('pub, sub', [
    (dict(reliability='reliable'), dict(reliability='best_effort')),
    (dict(durability='transient_local'), dict(durability='volatile')),
    (dict(deadline_ms=50.0), dict(deadline_ms=100.0)),
    (dict(deadline_ms=50.0), dict()),                        # sub requests none
    (dict(liveliness='manual_by_topic'), dict(liveliness='automatic')),
    (dict(liveliness_lease_ms=500.0), dict(liveliness_lease_ms=1000.0)),
    (dict(liveliness='system_default'), dict(liveliness='manual_by_topic')),
])
def test_stronger_or_equal_offers_are_compatible(pub, sub):
    assert incompatibilities(_ep('publisher', **pub), _ep('subscriber', **sub)) == []


def test_topic_status_and_evidence_names_the_pair():
    eps = [_ep('publisher', '/lidar', deadline_ms=200.0),
           _ep('subscriber', '/fusion', deadline_ms=100.0),
           _ep('subscriber', '/logger')]
    assert detect_qos_status(eps) == 'mismatch'
    assert topic_incompatibilities(eps) == [
        '/lidar → /fusion: deadline: publisher offers 200 ms, subscriber '
        'requests 100 ms']
    assert detect_qos_status(eps[:1]) == 'unknown'
    assert detect_qos_status([eps[0], eps[2]]) == 'ok'


def test_issue_evidence_says_which_policy():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/scan', publishers=['/lidar'], subscribers=['/fusion'],
                  publisher_count=1, subscriber_count=1, qos_status='mismatch',
                  qos_endpoints=[_ep('publisher', '/lidar', deadline_ms=200.0),
                                 _ep('subscriber', '/fusion', deadline_ms=100.0)])
    store.set_graph({}, {'/scan': t})
    [issue] = [i for i in analyze(store, Thresholds()) if i.kind == 'qos_mismatch']
    assert issue.evidence == ['/lidar → /fusion: deadline: publisher offers '
                              '200 ms, subscriber requests 100 ms']


def test_live_deadline_mismatch_is_seen_in_endpoint_info():
    pytest.importorskip('rclpy')
    import rclpy
    from rclpy.duration import Duration
    from rclpy.node import Node
    from rclpy.qos import QoSProfile
    from std_msgs.msg import String

    from ros_graph_debugger.graph_build import build_graph

    rclpy.init()
    try:
        node = Node('rgd_qos_test')
        node.create_publisher(String, '/rgd_qos/dl', QoSProfile(
            depth=5, deadline=Duration(seconds=0.2)))
        node.create_subscription(String, '/rgd_qos/dl', lambda m: None, QoSProfile(
            depth=5, deadline=Duration(seconds=0.1)))
        topics = {}
        for _ in range(10):
            time.sleep(0.3)
            _, topics = build_graph(node)
            t = topics.get('/rgd_qos/dl')
            if t and len(t.qos_endpoints) >= 2:
                break
        t = topics.get('/rgd_qos/dl')
        if not t or len(t.qos_endpoints) < 2:
            node.destroy_node()
            pytest.skip('local DDS endpoint discovery unavailable in this env')
        pub = next(e for e in t.qos_endpoints if e.endpoint_type == 'publisher')
        assert pub.deadline_ms == pytest.approx(200.0)
        assert t.qos_status == 'mismatch'
        assert 'deadline' in topic_incompatibilities(t.qos_endpoints)[0]
        node.destroy_node()
    finally:
        rclpy.shutdown()
