"""Issue engine / bottleneck analyzer tests — fully synthetic, no ROS needed."""

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds as Thr
from ros_graph_debugger.model import (
    CallbackStat,
    NodeInfo,
    QoSInfo,
    RuntimeGraphStore,
    TopicInfo,
)


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


def test_slow_callback_issue():
    store = RuntimeGraphStore()
    store.set_graph({'/detector': NodeInfo(id='/detector', name='detector')}, {})
    store.set_callbacks([
        CallbackStat(node='/detector', callback='sub /image', topic='/image',
                     count=100, mean_ms=120.0, p95_ms=210.0, max_ms=260.0),
        CallbackStat(node='/detector', callback='sub /fast', topic='/fast',
                     count=100, mean_ms=8.0, p95_ms=18.0, max_ms=25.0),
    ])
    issues = analyze(store, Thr())  # default slow_callback_ms = 100
    slow = [i for i in issues if i.kind == 'slow_callback']
    assert len(slow) == 1  # only the 210 ms callback trips it
    assert slow[0].severity == 'critical'  # > 2x the limit
    assert '/detector' in slow[0].related_nodes
    assert '/image' in slow[0].related_topics


def test_slow_callback_budget_is_stage_aware():
    # The same 60 ms callback is fine for planning but a violation for control.
    store = RuntimeGraphStore()
    store.set_graph({'/p': NodeInfo(id='/p', name='p'),
                     '/c': NodeInfo(id='/c', name='c')}, {})
    store.set_callbacks([
        CallbackStat(node='/p', callback='sub /planning/plan',
                     topic='/planning/plan', p95_ms=60.0),
        CallbackStat(node='/c', callback='sub /control/cmd',
                     topic='/control/cmd', p95_ms=60.0),
    ])
    thr = Thr(callback_ms_patterns=[('^/control/.*', 15.0), ('^/planning/.*', 200.0)])
    slow = [i for i in analyze(store, thr) if i.kind == 'slow_callback']
    assert len(slow) == 1
    assert slow[0].related_topics == ['/control/cmd']  # only control trips


def test_callbacks_under_budget_are_silent():
    store = RuntimeGraphStore()
    store.set_graph({'/n': NodeInfo(id='/n', name='n')}, {})
    store.set_callbacks([CallbackStat(node='/n', callback='sub /t', topic='/t',
                                      p95_ms=40.0)])
    assert 'slow_callback' not in _kinds(analyze(store, Thr()))


def test_clean_system_has_no_issues():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/t', publisher_count=1, subscriber_count=1,
                  publishers=['/p'], subscribers=['/s'])
    t.qos_status = 'ok'
    store.set_graph({'/p': NodeInfo(id='/p', name='p'),
                     '/s': NodeInfo(id='/s', name='s')}, {'/t': t})
    assert analyze(store, Thr()) == []
