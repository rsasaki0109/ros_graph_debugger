"""Graph polls clear stale process evidence and preserve shared-PID samples."""
from types import SimpleNamespace

import pytest
import rclpy

from ros_graph_debugger.config import ProbeConfig, Thresholds
from ros_graph_debugger.model import NodeInfo, RuntimeGraphStore
from ros_graph_debugger.node import DebuggerNode
from test_process_metrics import Process


@pytest.fixture
def collector(monkeypatch):
    process = Process(cpu=(0, 42, 73, 99))
    monkeypatch.setattr('ros_graph_debugger.node.psutil',
                        SimpleNamespace(Process=lambda _: process))
    table = [{'pid': 17, 'cmdline': ['container', '--ros-args', '-r',
                                   '__node:=first', '-r', '__node:=second']}]
    monkeypatch.setattr('ros_graph_debugger.node._collect_processes', lambda: table)
    store = RuntimeGraphStore()
    store.set_graph({'/first': NodeInfo('/first'), '/second': NodeInfo('/second')}, {})
    rclpy.init(args=[])
    node = DebuggerNode(store, ProbeConfig(), Thresholds(), None, lifecycle=False)
    try:
        yield node, store, process, table
    finally:
        node.destroy_node()
        rclpy.shutdown()


def nodes(store):
    return store.working_copy()[0]


def test_shared_process_cpu_is_not_sampled_once_per_node(collector):
    node, store, process, _ = collector
    node._poll_processes()
    assert all(n.cpu_percent is None and n.pid == 17 for n in nodes(store).values())
    node._poll_processes()
    assert all(n.cpu_percent == 42 and n.process_mapping_confidence == 'low'
               for n in nodes(store).values())
    assert process.cpu_calls == 2


def test_removed_mapping_clears_previous_measurements(collector):
    node, store, _, table = collector
    node._poll_processes()
    node._poll_processes()
    store.update_node_process('/first', lifecycle_state='active')
    table.clear()
    node._poll_processes()
    for n in nodes(store).values():
        assert (n.pid, n.cpu_percent, n.rss_bytes, n.process_name) == (None,) * 4
        assert n.process_mapping_confidence == 'none'
    assert nodes(store)['/first'].lifecycle_state == 'active'


def test_inspection_failure_clears_stale_cpu_and_rss(collector):
    node, store, process, _ = collector
    node._poll_processes()
    node._poll_processes()
    process.denied = True
    node._poll_processes()
    assert all(n.cpu_percent is None and n.rss_bytes is None and n.pid is None
               for n in nodes(store).values())


def test_table_failure_invalidates_sampling_baseline(collector, monkeypatch):
    node, store, _, table = collector
    node._poll_processes()
    node._poll_processes()
    def fail():
        raise PermissionError('process table unavailable')
    monkeypatch.setattr('ros_graph_debugger.node._collect_processes', fail)
    node._poll_processes()
    assert all(n.cpu_percent is None for n in nodes(store).values())
    monkeypatch.setattr('ros_graph_debugger.node._collect_processes', lambda: table)
    node._poll_processes()
    assert all(n.cpu_percent is None for n in nodes(store).values())


def test_optional_psutil_unavailable_clears_previous_metrics(collector, monkeypatch):
    node, store, _, _ = collector
    node._poll_processes()
    node._poll_processes()
    monkeypatch.setattr('ros_graph_debugger.node.psutil', None)
    node._poll_processes()
    assert all(n.pid is None and n.cpu_percent is None for n in nodes(store).values())
