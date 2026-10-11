"""Lifecycle (managed) node states: detection, the issue rule, and a live query."""

import time

import pytest

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.lifecycle import (
    is_lifecycle_node,
    suggested_transition,
    waiting_outputs,
)
from ros_graph_debugger.markdown import snapshot_to_markdown
from ros_graph_debugger.model import NodeInfo, RuntimeGraphStore, TopicInfo


def _graph(state='inactive', planner_state='active'):
    nodes = {
        '/controller_server': NodeInfo(
            id='/controller_server', name='controller_server',
            publishers=['/cmd_vel'], subscribers=['/plan'],
            services=['/controller_server/get_state',
                      '/controller_server/change_state']),
        '/planner_server': NodeInfo(
            id='/planner_server', name='planner_server', publishers=['/plan'],
            services=['/planner_server/get_state']),
        '/base': NodeInfo(id='/base', name='base', subscribers=['/cmd_vel']),
    }
    topics = {
        '/cmd_vel': TopicInfo(name='/cmd_vel', publishers=['/controller_server'],
                              subscribers=['/base'], publisher_count=1,
                              subscriber_count=1),
        '/plan': TopicInfo(name='/plan', publishers=['/planner_server'],
                           subscribers=['/controller_server'], publisher_count=1,
                           subscriber_count=1),
    }
    store = RuntimeGraphStore()
    store.set_graph(nodes, topics)
    store.update_node_process('/controller_server', lifecycle_state=state)
    store.update_node_process('/planner_server', lifecycle_state=planner_state)
    return store


def _lifecycle_issues(store):
    return [i for i in analyze(store, Thresholds()) if i.kind == 'lifecycle_not_active']


def test_detection_and_helpers():
    store = _graph()
    nodes, topics, *_ = store.working_copy()
    assert is_lifecycle_node(nodes['/controller_server'])
    assert not is_lifecycle_node(nodes['/base'])
    assert waiting_outputs(nodes['/controller_server'], topics) == ['/cmd_vel']
    assert suggested_transition('/n', 'unconfigured') == 'ros2 lifecycle set /n configure'
    assert suggested_transition('/n', 'inactive') == 'ros2 lifecycle set /n activate'
    assert suggested_transition('/n', 'finalized') is None


def test_inactive_node_with_waiting_subscribers_is_critical():
    [issue] = _lifecycle_issues(_graph('inactive'))
    assert issue.severity == 'critical'
    assert issue.title == 'controller_server is inactive (lifecycle)'
    assert issue.evidence == ['lifecycle state: inactive',
                              '/cmd_vel: 1 subscriber(s) waiting']
    assert issue.suggested_actions[0] == \
        'Transition it: ros2 lifecycle set /controller_server activate'
    assert issue.related_topics == ['/cmd_vel']
    snap = _graph('inactive').snapshot().to_dict()
    assert {n['id']: n['lifecycle_state'] for n in snap['nodes']}['/base'] is None


def test_idle_node_nobody_waits_on_is_a_warning():
    [issue] = _lifecycle_issues(_graph_no_consumer())
    assert issue.severity == 'warning'
    assert 'subscribers waiting' not in issue.explanation


def _graph_no_consumer():
    nodes = {'/map_server': NodeInfo(id='/map_server', name='map_server',
                                     publishers=['/map'],
                                     services=['/map_server/get_state'])}
    topics = {'/map': TopicInfo(name='/map', publishers=['/map_server'],
                                publisher_count=1)}
    store = RuntimeGraphStore()
    store.set_graph(nodes, topics)
    store.update_node_process('/map_server', lifecycle_state='unconfigured')
    return store


def test_active_unknown_and_transitional_states_are_quiet():
    for state in ('active', 'unknown', 'activating', None):
        assert _lifecycle_issues(_graph(state)) == []


def test_errorprocessing_is_critical_and_explained():
    [issue] = _lifecycle_issues(_graph('errorprocessing'))
    assert issue.severity == 'critical'
    assert 'A transition callback failed.' in issue.explanation


def test_state_survives_graph_repoll():
    store = _graph('inactive')
    nodes, topics, *_ = store.working_copy()
    store.set_graph({k: NodeInfo(id=v.id, name=v.name, publishers=v.publishers,
                                 subscribers=v.subscribers, services=v.services)
                     for k, v in nodes.items()}, topics)
    states = {n['id']: n['lifecycle_state'] for n in store.snapshot().to_dict()['nodes']}
    assert states['/controller_server'] == 'inactive'


def test_briefing_lists_idle_lifecycle_nodes():
    md = snapshot_to_markdown(_graph('inactive').snapshot().to_dict())
    assert '## Lifecycle nodes (1 of 2 active)' in md
    assert '- /controller_server: **inactive**' in md


# --------------------------------------------------------------------------- #
# Live rclpy: the agent queries a real lifecycle node in the same process.
# --------------------------------------------------------------------------- #
def test_agent_reads_live_lifecycle_state():
    pytest.importorskip('rclpy')
    try:
        from rclpy.lifecycle import LifecycleNode
    except ImportError:
        pytest.skip('rclpy.lifecycle not available')
    import rclpy
    from rclpy.executors import SingleThreadedExecutor

    from ros_graph_debugger.config import ProbeConfig
    from ros_graph_debugger.node import DebuggerNode

    rclpy.init()
    try:
        store = RuntimeGraphStore()
        agent = DebuggerNode(store, ProbeConfig(enabled=False), Thresholds(), None,
                             statistics_topics={})
        managed = LifecycleNode('rgd_lc_test')
        executor = SingleThreadedExecutor()
        executor.add_node(agent)
        executor.add_node(managed)

        def wait_for(expected, budget=40):
            """Poll like the agent's timers would until the state reads
            ``expected``; return the last state seen."""
            st = None
            for _ in range(budget):
                agent._collect_graph()
                agent._query_lifecycle(time.time())
                for _ in range(5):
                    executor.spin_once(timeout_sec=0.05)
                nodes = {n['id']: n for n in store.snapshot().to_dict()['nodes']}
                st = (nodes.get('/rgd_lc_test') or {}).get('lifecycle_state')
                if st == expected:
                    break
            return st

        first = wait_for('unconfigured')
        if first in (None, 'unknown'):
            pytest.skip('local service discovery unavailable in this env')
        assert first == 'unconfigured'
        managed.trigger_configure()
        assert wait_for('inactive') == 'inactive'
        managed.trigger_activate()
        assert wait_for('active') == 'active'
        executor.shutdown()
        managed.destroy_node()
        agent.destroy_node()
    finally:
        rclpy.shutdown()
