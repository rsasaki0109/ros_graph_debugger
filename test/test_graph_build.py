"""build_graph tests.

Two layers:
  - a fast mock-based test (deterministic, no DDS) of the reconstruction logic;
  - a live single-node rclpy test that exercises the real introspection APIs
    against the node's *own* endpoints (no cross-process discovery required).
"""

import pytest

from ros_graph_debugger.graph_build import build_graph, fq_node


# --------------------------------------------------------------------------- #
# Mock rclpy node returning canned endpoint info.
# --------------------------------------------------------------------------- #
class _QoS:
    def __init__(self, reliability, durability=0):
        from rclpy.qos import QoSDurabilityPolicy, QoSReliabilityPolicy
        self.reliability = (QoSReliabilityPolicy.BEST_EFFORT if reliability == 'be'
                            else QoSReliabilityPolicy.RELIABLE)
        self.durability = QoSDurabilityPolicy.VOLATILE
        from rclpy.qos import QoSHistoryPolicy, QoSLivelinessPolicy
        self.history = QoSHistoryPolicy.KEEP_LAST
        self.liveliness = QoSLivelinessPolicy.AUTOMATIC
        self.depth = 10


class _EP:
    def __init__(self, node_name, ns, etype, reliability):
        self.node_name = node_name
        self.node_namespace = ns
        self.endpoint_type = etype
        self.qos_profile = _QoS(reliability)


class _MockNode:
    def get_node_names_and_namespaces(self):
        return [('cam', '/'), ('detector', '/')]

    def get_topic_names_and_types(self):
        return [('/image', ['sensor_msgs/msg/Image']),
                ('/objects', ['std_msgs/msg/String'])]

    def get_publishers_info_by_topic(self, t):
        if t == '/image':
            return [_EP('cam', '/', 'publisher', 're')]
        if t == '/objects':
            return [_EP('detector', '/', 'publisher', 'be')]  # best_effort
        return []

    def get_subscriptions_info_by_topic(self, t):
        if t == '/image':
            return [_EP('detector', '/', 'subscriber', 're')]
        if t == '/objects':
            return [_EP('tracker', '/', 'subscriber', 're')]  # reliable -> mismatch
        return []

    def get_service_names_and_types_by_node(self, name, ns):
        return []


def test_fq_node():
    assert fq_node('cam', '/') == '/cam'
    assert fq_node('detector', '/perception') == '/perception/detector'
    assert fq_node('', '/') == ''
    assert fq_node('_NODE_NAME_UNKNOWN_', '/') == ''


def test_build_graph_from_mock():
    nodes, topics = build_graph(_MockNode())
    # nodes reconstructed from endpoints (incl. tracker which only subscribes)
    assert set(nodes) == {'/cam', '/detector', '/tracker'}
    assert topics['/image'].publishers == ['/cam']
    assert topics['/image'].subscribers == ['/detector']
    # best_effort pub + reliable sub -> mismatch
    assert topics['/objects'].qos_status == 'mismatch'
    # node back-references
    assert '/objects' in nodes['/detector'].publishers
    assert '/image' in nodes['/detector'].subscribers


# --------------------------------------------------------------------------- #
# Live rclpy: a node sees its own endpoints without network discovery.
# --------------------------------------------------------------------------- #
def test_build_graph_live_self_endpoints():
    rclpy = pytest.importorskip('rclpy')
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from std_msgs.msg import String
    import time

    rclpy.init()
    try:
        node = Node('rgd_selftest')
        node.create_publisher(String, '/rgd_test/be', QoSProfile(
            depth=5, reliability=ReliabilityPolicy.BEST_EFFORT))
        node.create_subscription(String, '/rgd_test/be', lambda m: None,
                                 QoSProfile(depth=5,
                                            reliability=ReliabilityPolicy.RELIABLE))
        # Give the local participant a moment to register its own endpoints.
        topics = {}
        for _ in range(10):
            time.sleep(0.3)
            _, topics = build_graph(node)
            if '/rgd_test/be' in topics and topics['/rgd_test/be'].qos_endpoints:
                break
        if '/rgd_test/be' not in topics:
            node.destroy_node()
            pytest.skip('local DDS endpoint discovery unavailable in this env')
        t = topics['/rgd_test/be']
        assert t.publisher_count >= 1
        assert t.subscriber_count >= 1
        assert t.qos_status == 'mismatch'  # be pub + reliable sub
        node.destroy_node()
    finally:
        rclpy.shutdown()
