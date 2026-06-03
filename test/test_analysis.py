"""Issue engine / bottleneck analyzer tests — fully synthetic, no ROS needed."""

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.model import NodeInfo, QoSInfo, RuntimeGraphStore, TopicInfo


class Thr:
    """Minimal thresholds stand-in (avoids importing the rclpy node module)."""
    high_bandwidth_bps = 50_000_000
    large_msg_bytes = 1_000_000
    stale_topic_ms = 2000.0
    tf_stale_ms = 1000.0
    high_cpu_percent = 90.0
    high_rss_bytes = 2_000_000_000

    def __init__(self):
        self.expected_min_rate = {}
        self.expected_max_age_ms = {}


def _kinds(issues):
    return {i.kind for i in issues}


def test_no_publisher_and_no_subscriber():
    store = RuntimeGraphStore()
    nodes = {'/a': NodeInfo(id='/a', name='a', subscribers=['/lonely'])}
    topics = {
        '/lonely': TopicInfo(name='/lonely', subscriber_count=1,
                             subscribers=['/a']),
        '/orphan': TopicInfo(name='/orphan', publisher_count=1,
                             publishers=['/a']),
    }
    store.set_graph(nodes, topics)
    kinds = _kinds(analyze(store, Thr()))
    assert 'no_publisher' in kinds
    assert 'no_subscriber' in kinds


def test_qos_mismatch():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/t', publisher_count=1, subscriber_count=1,
                  publishers=['/p'], subscribers=['/s'])
    t.qos_status = 'mismatch'
    t.qos_endpoints = [
        QoSInfo(node='/p', endpoint_type='publisher', reliability='best_effort'),
        QoSInfo(node='/s', endpoint_type='subscriber', reliability='reliable'),
    ]
    store.set_graph({'/p': NodeInfo(id='/p', name='p'),
                     '/s': NodeInfo(id='/s', name='s')}, {'/t': t})
    issues = analyze(store, Thr())
    assert 'qos_mismatch' in _kinds(issues)
    mm = [i for i in issues if i.kind == 'qos_mismatch'][0]
    assert mm.severity == 'critical'
    assert mm.evidence  # carries the offending endpoints


def test_bottleneck_inference():
    store = RuntimeGraphStore()
    detector = NodeInfo(id='/detector', name='detector',
                        subscribers=['/image'], publishers=['/objects'])
    detector.cpu_percent = 95.0
    detector.process_mapping_confidence = 'medium'
    img = TopicInfo(name='/image', publisher_count=1, subscriber_count=1,
                    publishers=['/cam'], subscribers=['/detector'])
    obj = TopicInfo(name='/objects', publisher_count=1, subscriber_count=1,
                    publishers=['/detector'], subscribers=['/tracker'])
    store.set_graph(
        {'/detector': detector, '/cam': NodeInfo(id='/cam', name='cam'),
         '/tracker': NodeInfo(id='/tracker', name='tracker')},
        {'/image': img, '/objects': obj})
    # Mark probed metrics: healthy input, slow output.
    store.update_topic_metrics('/image', rate_hz=30.0, last_seen_time=None)
    store.update_topic_metrics('/objects', rate_hz=4.0, last_seen_time=None)

    thr = Thr()
    thr.expected_min_rate['/objects'] = 10.0
    issues = analyze(store, thr)
    kinds = _kinds(issues)
    assert 'rate_drop' in kinds
    assert 'high_cpu' in kinds
    assert 'bottleneck' in kinds
    bn = [i for i in issues if i.kind == 'bottleneck'][0]
    assert bn.severity == 'critical'  # CPU-hot -> critical
    assert '/detector' in bn.related_nodes
    # Severity ordering puts critical first.
    assert issues[0].severity == 'critical'


def test_clean_system_has_no_issues():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/t', publisher_count=1, subscriber_count=1,
                  publishers=['/p'], subscribers=['/s'])
    t.qos_status = 'ok'
    store.set_graph({'/p': NodeInfo(id='/p', name='p'),
                     '/s': NodeInfo(id='/s', name='s')}, {'/t': t})
    assert analyze(store, Thr()) == []
