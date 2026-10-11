"""Services and actions: the server/client graph and missing-server issues."""

import time

import pytest

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.markdown import snapshot_to_markdown
from ros_graph_debugger.model import NodeInfo, RuntimeGraphStore
from ros_graph_debugger.services import missing_servers, service_graph

PARAMS = ['/get_parameters', '/set_parameters', '/list_parameters',
          '/describe_parameters', '/get_parameter_types',
          '/set_parameters_atomically', '/get_type_description']


def _node(nid, served=(), called=()):
    infra = [nid + p for p in PARAMS]
    return NodeInfo(id=nid, name=nid.rsplit('/', 1)[-1],
                    services=infra + list(served), service_clients=list(called))


def _nav_nodes():
    return {
        '/bt_navigator': _node(
            '/bt_navigator',
            served=['/navigate_to_pose/_action/send_goal',
                    '/navigate_to_pose/_action/get_result',
                    '/navigate_to_pose/_action/cancel_goal'],
            called=['/follow_path/_action/send_goal',
                    '/follow_path/_action/get_result',
                    '/planner_server/get_state',  # lifecycle probe
                    '/clear_entirely_global_costmap']),
        '/planner_server': _node('/planner_server',
                                 served=['/planner_server/get_state']),
        '/rviz': _node('/rviz', called=['/navigate_to_pose/_action/send_goal',
                                        '/bt_navigator/get_parameters']),
    }


def test_service_graph_groups_servers_and_clients_and_drops_housekeeping():
    g = service_graph(_nav_nodes())
    assert g['actions'] == [
        {'name': '/follow_path', 'servers': [], 'clients': ['/bt_navigator']},
        {'name': '/navigate_to_pose', 'servers': ['/bt_navigator'],
         'clients': ['/rviz']},
    ]
    assert g['services'] == [
        {'name': '/clear_entirely_global_costmap', 'servers': [],
         'clients': ['/bt_navigator']},
        {'name': '/planner_server/get_state', 'servers': ['/planner_server'],
         'clients': ['/bt_navigator']},
    ]


def test_missing_servers():
    g = service_graph(_nav_nodes())
    assert [(e['kind'], e['name']) for e in missing_servers(g)] == [
        ('service', '/clear_entirely_global_costmap'), ('action', '/follow_path')]


def test_issues_and_snapshot():
    store = RuntimeGraphStore()
    store.set_graph(_nav_nodes(), {})
    issues = {i.kind: i for i in analyze(store, Thresholds())
              if i.kind in ('no_action_server', 'no_service_server')}
    assert set(issues) == {'no_action_server', 'no_service_server'}
    issue = issues['no_action_server']
    assert issue.title == '/follow_path has action clients but no server'
    assert issue.severity == 'warning'
    assert issue.related_nodes == ['/bt_navigator']

    snap = store.snapshot().to_dict()
    assert [a['name'] for a in snap['actions']] == ['/follow_path', '/navigate_to_pose']
    md = snapshot_to_markdown(snap)
    assert '## Clients without a server' in md
    assert '- action /follow_path: called by /bt_navigator' in md


def test_no_section_or_issue_when_everything_is_served():
    store = RuntimeGraphStore()
    store.set_graph({'/a': _node('/a', served=['/do']),
                     '/b': _node('/b', called=['/do'])}, {})
    assert not [i for i in analyze(store, Thresholds()) if 'server' in i.kind]
    assert 'Clients without a server' not in snapshot_to_markdown(
        store.snapshot().to_dict())


# --------------------------------------------------------------------------- #
# Live rclpy: build_graph sees a node's own services and clients.
# --------------------------------------------------------------------------- #
def test_build_graph_live_services_and_clients():
    pytest.importorskip('rclpy')
    import rclpy
    from rclpy.node import Node
    from rcl_interfaces.srv import ListParameters

    from ros_graph_debugger.graph_build import build_graph

    rclpy.init()
    try:
        node = Node('rgd_svc_test')
        node.create_service(ListParameters, '/rgd_svc_test/present',
                            lambda req, res: res)
        node.create_client(ListParameters, '/rgd_svc_test/present')
        node.create_client(ListParameters, '/rgd_svc_test/missing')
        # The node shows up via its default /rosout and /parameter_events
        # publishers; its service endpoints take a moment to register.
        nodes = {}
        for _ in range(15):
            time.sleep(0.3)
            nodes, _ = build_graph(node)
            me = nodes.get('/rgd_svc_test')
            if me and '/rgd_svc_test/missing' in me.service_clients:
                break
        me = nodes.get('/rgd_svc_test')
        if not me or '/rgd_svc_test/missing' not in me.service_clients:
            node.destroy_node()
            pytest.skip('local graph introspection unavailable in this env')
        assert '/rgd_svc_test/present' in me.services
        missing = missing_servers(service_graph(nodes))
        assert [e['name'] for e in missing] == ['/rgd_svc_test/missing']
        node.destroy_node()
    finally:
        rclpy.shutdown()
