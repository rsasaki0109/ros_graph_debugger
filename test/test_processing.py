"""App-reported processing time (Tier B): ~/debug/processing_time_ms as a
node's execution time, through budgets, the pipeline path and bags."""

import time
from types import SimpleNamespace as NS

import pytest

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.bag_analyze import BagAnalyzer
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.model import CallbackStat, RuntimeGraphStore
from ros_graph_debugger.paths import find_profile
from ros_graph_debugger.pipeline import trace_pipeline_path
from ros_graph_debugger.processing import ProcessingTimes, node_for, value_ms
from ros_graph_debugger.profile import apply_profile, load_profile

CTRL = '/control/trajectory_follower/controller_node_exe'
CTRL_TOPIC = CTRL + '/debug/processing_time_ms'


def test_value_and_node_resolution():
    assert value_ms(NS(data=12.5)) == 12.5
    assert value_ms(NS(data=3)) == 3.0
    assert value_ms(NS(data='x')) is None and value_ms(NS(data=True)) is None
    assert value_ms(NS()) is None
    assert node_for(CTRL_TOPIC, [CTRL]) == CTRL                  # graph knows it
    assert node_for(CTRL_TOPIC, []) == CTRL                      # ~/debug/ convention
    assert node_for(CTRL_TOPIC, ['/a', '/b']) == CTRL            # ambiguous pubs
    assert node_for('/timing', []) is None


def test_stats_window():
    p = ProcessingTimes(window=4)
    for v in (10, 20, 30, 40, 50, None):
        p.add(CTRL_TOPIC, v)
    [s] = p.stats()
    assert (s.node, s.callback, s.topic) == (CTRL, 'processing_time', CTRL_TOPIC)
    assert (s.count, s.mean_ms, s.max_ms) == (4, 35.0, 50.0)
    assert s.source == 'application_report'


def test_autoware_profile_budget_flags_a_slow_controller():
    data, _ = load_profile(find_profile('autoware'))
    assert '/debug/processing_time_ms$' in data['processing_time_patterns']
    th = Thresholds()
    apply_profile(th, data)
    limit = th.callback_budget_for(CTRL_TOPIC)
    assert limit and limit <= 20  # control stage: tight budget

    store = RuntimeGraphStore()
    store.set_callbacks([CallbackStat(node='/perception/detector', callback='sub /x',
                                      topic='/x', count=5, p95_ms=5.0)])
    p = ProcessingTimes()
    for _ in range(20):
        p.add(CTRL_TOPIC, 3 * limit)
    store.set_processing_times(p.stats())
    snap = store.snapshot().to_dict()
    assert {c['callback'] for c in snap['callbacks']} == {'sub /x', 'processing_time'}
    [issue] = [i for i in analyze(store, th) if i.kind == 'slow_callback']
    assert issue.related_nodes == [CTRL] and issue.severity == 'critical'
    assert 'processing_time' in issue.explanation


def test_pipeline_path_falls_back_to_processing_time():
    d = {'nodes': [{'id': '/a', 'publishers': ['/t'], 'subscribers': []},
                   {'id': '/b', 'publishers': [], 'subscribers': ['/t']}],
         'topics': [{'name': '/t', 'rate_hz': 10.0,
                     'publishers': ['/a'], 'subscribers': ['/b']}],
         'callbacks': [{'node': '/b', 'callback': 'processing_time',
                        'topic': '/b/debug/processing_time_ms', 'p95_ms': 42.0}]}
    path = trace_pipeline_path(d, '/a')
    assert path['hops'][0]['cb_p95_ms'] == 42.0
    # A per-subscription callback, when known, wins over processing time.
    d['callbacks'].append({'node': '/b', 'callback': 'sub /t', 'topic': '/t',
                           'p95_ms': 7.0})
    assert trace_pipeline_path(d, '/a')['hops'][0]['cb_p95_ms'] == 7.0


def test_bag_windows_carry_processing_time():
    an = BagAnalyzer({CTRL_TOPIC: 'std_msgs/msg/Float64'}, Thresholds())
    for i in range(50):
        t = 100.0 + i * 0.1
        an.add_message(CTRL_TOPIC, t, 16)
        an.add_processing_time(t, CTRL_TOPIC, 5.0 if t < 102.5 else 50.0)
    snaps = an.snapshots(interval=1.0, window=1.0)
    first = next(c for c in snaps[0]['callbacks'] if c['node'] == CTRL)
    last = next(c for c in snaps[-1]['callbacks'] if c['node'] == CTRL)
    assert first['p95_ms'] == 5.0 and last['p95_ms'] == 50.0
    assert first['source'] == last['source'] == 'application_report'


def test_agent_subscribes_and_reports_live():
    pytest.importorskip('rclpy')
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from std_msgs.msg import Float64

    from ros_graph_debugger.config import ProbeConfig
    from ros_graph_debugger.node import DebuggerNode

    rclpy.init()
    try:
        store = RuntimeGraphStore()
        agent = DebuggerNode(store, ProbeConfig(enabled=False), Thresholds(), None,
                             statistics_topics={}, lifecycle=False,
                             processing_time_patterns=['/debug/processing_time_ms$'])
        worker = Node('rgd_proc')
        pub = worker.create_publisher(Float64, '/rgd_proc/debug/processing_time_ms', 10)
        executor = SingleThreadedExecutor()
        executor.add_node(agent)
        executor.add_node(worker)
        stat = None
        for _ in range(30):
            agent._collect_graph()
            pub.publish(Float64(data=12.0))
            for _ in range(5):
                executor.spin_once(timeout_sec=0.05)
            agent._refresh_metrics_and_analyze()
            stat = next((c for c in store.snapshot().to_dict()['callbacks']
                         if c['callback'] == 'processing_time'), None)
            if stat:
                break
            time.sleep(0.05)
        if stat is None:
            pytest.skip('local DDS delivery unavailable in this env')
        assert stat['node'] == '/rgd_proc'
        assert stat['p95_ms'] == 12.0
        executor.shutdown()
        worker.destroy_node()
        agent.destroy_node()
    finally:
        rclpy.shutdown()
