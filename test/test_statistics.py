"""Latency Tier B — rclcpp topic statistics (no DDS: duck-typed messages)."""

from types import SimpleNamespace as NS

import pytest

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.markdown import snapshot_to_markdown
from ros_graph_debugger.model import NodeInfo, RuntimeGraphStore, TopicInfo
from ros_graph_debugger.statistics import (
    AGE,
    PERIOD,
    StatisticsBuffer,
    attribute,
    parse_metrics,
    parse_statistics_topics,
    topic_overlay,
)


def _msg(source, metric, avg, *, count=30, mn=None, mx=None, std=0.0):
    points = [NS(data_type=1, data=avg), NS(data_type=4, data=std),
              NS(data_type=5, data=float(count))]
    if mn is not None:
        points.append(NS(data_type=2, data=mn))
    if mx is not None:
        points.append(NS(data_type=3, data=mx))
    return NS(measurement_source_name=source, metrics_source=metric, unit='ms',
              window_stop=NS(sec=100, nanosec=500_000_000), statistics=points)


def _row(source, metric, avg, **kw):
    hint = kw.pop('hint', None)
    return parse_metrics(_msg(source, metric, avg, **kw), topic_hint=hint,
                         received=0.0)


def _graph():
    nodes = {
        '/camera': NodeInfo(id='/camera', name='camera',
                            publishers=['/image_raw']),
        '/perception/detector': NodeInfo(
            id='/perception/detector', name='detector',
            subscribers=['/image_raw', '/parameter_events'],
            publishers=['/objects']),
        '/fusion': NodeInfo(id='/fusion', name='fusion',
                            subscribers=['/image_raw', '/objects']),
    }
    topics = {
        '/image_raw': TopicInfo(name='/image_raw',
                                type='sensor_msgs/msg/Image',
                                publishers=['/camera'],
                                subscribers=['/perception/detector', '/fusion'],
                                publisher_count=1, subscriber_count=2),
        '/objects': TopicInfo(name='/objects', publishers=['/perception/detector'],
                              subscribers=['/fusion'],
                              publisher_count=1, subscriber_count=1),
    }
    return nodes, topics


# --------------------------------------------------------------- parsing #
def test_parse_metrics_flattens_data_points():
    r = parse_metrics(_msg('detector', PERIOD, 33.3, mn=30.0, mx=40.0, std=2.0),
                      '/statistics', received=5.0)
    assert r['source'] == 'detector' and r['metric'] == PERIOD
    assert (r['avg'], r['min'], r['max'], r['stddev'], r['count']) \
        == (33.3, 30.0, 40.0, 2.0, 30)
    assert r['window_stop'] == 100.5 and r['received'] == 5.0


def test_parse_metrics_skips_nan_and_foreign_metrics():
    nan = float('nan')
    r = parse_metrics(_msg('detector', AGE, nan, count=0))
    assert r['avg'] is None and r['count'] == 0
    assert parse_metrics(NS(metrics_source='cpu_usage')) is None


def test_buffer_keeps_latest_and_expires():
    buf = StatisticsBuffer(expiry_s=10)
    buf.ingest(_row('detector', PERIOD, 50.0))
    buf.ingest(_row('detector', PERIOD, 33.3))
    buf.ingest(None)
    assert [r['avg'] for r in buf.rows(now=5)] == [33.3]
    assert buf.rows(now=11) == []


def test_parse_statistics_topics():
    assert parse_statistics_topics(None) == {'/statistics': None}
    assert parse_statistics_topics(['/stats/objects=/objects', '/statistics']) \
        == {'/stats/objects': '/objects', '/statistics': None}
    assert parse_statistics_topics([]) == {}


# ------------------------------------------------------------ attribution #
def test_attribution_modes():
    nodes, topics = _graph()
    rows = [
        _row('detector', PERIOD, 33.3),            # one data sub (+ infra)
        _row('fusion', PERIOD, 50.0),              # two data subs
        _row('fusion', AGE, 20.0, hint='/objects'),  # mapped explicitly
        _row('ghost', PERIOD, 10.0),               # not in the graph
        _row('/fusion', PERIOD, 10.0, hint='/nope'),  # mapped to a missing topic
    ]
    got = [(r['node'], r['topic'], r['attribution'])
           for r in attribute(rows, nodes, topics)]
    assert got == [
        ('/perception/detector', '/image_raw', 'single_sub'),
        ('/fusion', None, 'ambiguous'),
        ('/fusion', '/objects', 'explicit'),
        (None, None, 'unknown_node'),
        ('/fusion', None, 'unknown_topic'),
    ]


def test_duplicate_short_names_are_not_guessed():
    nodes = {'/a/detector': NodeInfo(id='/a/detector', name='detector',
                                     subscribers=['/x']),
             '/b/detector': NodeInfo(id='/b/detector', name='detector',
                                     subscribers=['/y'])}
    topics = {'/x': TopicInfo(name='/x'), '/y': TopicInfo(name='/y')}
    [r] = attribute([_row('detector', PERIOD, 10.0)], nodes, topics)
    assert r['attribution'] == 'unknown_node'


def test_overlay_takes_slowest_rate_and_oldest_age():
    rows = [
        {**_row('a', PERIOD, 33.3), 'topic': '/t'},
        {**_row('b', PERIOD, 100.0), 'topic': '/t'},
        {**_row('a', AGE, 20.0, std=5.0, mx=60.0), 'topic': '/t'},
        {**_row('b', AGE, 40.0, std=0.0, mx=45.0), 'topic': '/t'},
        {**_row('c', AGE, 10.0, count=0), 'topic': '/u'},  # empty age window
    ]
    o = topic_overlay(rows)
    assert o['/t']['rate_hz'] == 10.0
    assert o['/t']['header_age_ms'] == 40.0
    # p95 ~ avg + 1.645*stddev, capped at the window max.
    assert o['/t']['header_age_p95_ms'] == 40.0
    assert '/u' not in o


def test_empty_period_window_reads_as_zero_hz():
    nan = float('nan')
    rows = [{**_row('a', PERIOD, nan, count=0), 'topic': '/t'},
            {**_row('b', PERIOD, 33.3), 'topic': '/t'}]
    assert topic_overlay(rows)['/t']['rate_hz'] == 0.0


def test_dead_topic_trips_rate_expectation():
    store = RuntimeGraphStore()
    store.set_graph(*_graph())
    store.set_statistics([_row('detector', PERIOD, float('nan'), count=0)])
    th = Thresholds()
    th.expected_min_rate['/image_raw'] = 10.0
    [issue] = [i for i in analyze(store, th) if i.kind == 'rate_drop']
    assert issue.evidence[0] == 'observed: 0.0 Hz (via topic statistics)'


# ------------------------------------------------------------------ store #
def test_store_overlays_unprobed_topics_and_keeps_probe_authoritative():
    store = RuntimeGraphStore()
    store.set_graph(*_graph())
    store.update_topic_metrics('/objects', rate_hz=9.0,
                               rate_measurement_scope='probe_receipt')
    store.set_statistics([_row('detector', PERIOD, 33.3),
                          _row('fusion', PERIOD, 50.0, hint='/objects')])
    snap = store.snapshot().to_dict()
    t = {x['name']: x for x in snap['topics']}
    assert t['/image_raw']['rate_hz'] == 30.03
    assert t['/image_raw']['rate_source'] == 'statistics'
    assert t['/image_raw']['rate_measurement_scope'] == 'subscriber_statistics'
    assert t['/objects']['rate_hz'] == 9.0
    assert t['/objects']['rate_source'] == 'probe'
    assert t['/objects']['rate_measurement_scope'] == 'probe_receipt'
    assert {s['attribution'] for s in snap['statistics']} == {'single_sub', 'explicit'}

    # The overlay survives a graph re-poll and is cleared when stats stop.
    store.set_graph(*_graph())
    image = {x['name']: x for x in store.snapshot().to_dict()['topics']}['/image_raw']
    assert image['rate_hz'] == 30.03
    assert image['rate_measurement_scope'] == 'subscriber_statistics'
    objects = next(t for t in store.snapshot().to_dict()['topics'] if t['name'] == '/objects')
    assert objects['rate_measurement_scope'] == 'probe_receipt'
    store.set_statistics([])
    image = {x['name']: x for x in store.snapshot().to_dict()['topics']}['/image_raw']
    assert image['rate_hz'] is None and image['rate_source'] is None
    assert image['rate_measurement_scope'] is None


def test_rate_drop_fires_from_statistics_with_honest_evidence():
    store = RuntimeGraphStore()
    store.set_graph(*_graph())
    store.set_statistics([_row('detector', PERIOD, 250.0)])  # 4 Hz
    th = Thresholds()
    th.expected_min_rate['/image_raw'] = 10.0
    issues = [i for i in analyze(store, th) if i.kind == 'rate_drop']
    assert len(issues) == 1
    assert issues[0].evidence[0] == 'observed: 4.0 Hz (via topic statistics)'


def test_briefing_marks_stats_rates_and_lists_unattributed():
    store = RuntimeGraphStore()
    store.set_graph(*_graph())
    store.set_statistics([_row('detector', PERIOD, 33.3),
                          _row('fusion', PERIOD, 50.0)])
    md = snapshot_to_markdown(store.snapshot().to_dict())
    assert '30.0 Hz (stats)' in md
    assert '## Topic statistics (not attributed to a topic)' in md
    assert 'node subscribes to several topics' in md
    assert '--statistics-topic /statistics=TOPIC' in md


# --------------------------------------------------------------------------- #
# Live rclpy: the agent node's MetricsMessage subscription, in one process.
# --------------------------------------------------------------------------- #
def test_debugger_node_ingests_metrics_messages_live():
    rclpy = pytest.importorskip('rclpy')
    import time

    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from statistics_msgs.msg import MetricsMessage, StatisticDataPoint

    from ros_graph_debugger.config import ProbeConfig
    from ros_graph_debugger.node import DebuggerNode

    rclpy.init()
    try:
        store = RuntimeGraphStore()
        agent = DebuggerNode(store, ProbeConfig(enabled=False), Thresholds(), None,
                             statistics_topics={'/rgd_test/statistics': '/rgd_test/data'})
        pub_node = Node('rgd_stats_pub')
        pub = pub_node.create_publisher(MetricsMessage, '/rgd_test/statistics', 10)
        msg = MetricsMessage(measurement_source_name='rgd_stats_pub',
                             metrics_source=PERIOD, unit='ms')
        msg.statistics = [StatisticDataPoint(data_type=1, data=20.0),
                          StatisticDataPoint(data_type=5, data=50.0)]
        executor = SingleThreadedExecutor()
        executor.add_node(agent)
        executor.add_node(pub_node)
        rows = []
        for _ in range(20):
            pub.publish(msg)
            executor.spin_once(timeout_sec=0.2)
            rows = agent._statistics.rows(now=time.time())
            if rows:
                break
        if not rows:
            pytest.skip('local DDS delivery unavailable in this env')
        [row] = rows
        assert (row['source'], row['metric'], row['avg'], row['count']) \
            == ('rgd_stats_pub', PERIOD, 20.0, 50)
        assert row['stats_topic'] == '/rgd_test/statistics'
        assert row['topic_hint'] == '/rgd_test/data'
        executor.shutdown()
        pub_node.destroy_node()
        agent.destroy_node()
    finally:
        rclpy.shutdown()
