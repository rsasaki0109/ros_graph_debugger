"""Observer work must not appear as a publisher rate regression."""

from types import SimpleNamespace
import threading
from unittest.mock import MagicMock

import pytest
import rclpy
from rclpy.context import Context
from rclpy.executors import Executor
from rclpy.node import Node

from ros_graph_debugger.node import _TopicProbe
from ros_graph_debugger import probe_executor


def test_delayed_callback_uses_middleware_receipt_for_rate(monkeypatch):
    # The last callback is delayed 55 ms by work in the observer. DDS delivery
    # remains exactly 10 Hz, but the old callback-clock rate would be 9.89 Hz.
    callbacks = iter([100 + i * 0.1 + (0.055 if i == 49 else 0) for i in range(50)])
    monkeypatch.setattr('ros_graph_debugger.node.time.monotonic', lambda: next(callbacks))
    probe = _TopicProbe(window=50)
    for i in range(50):
        probe.add(100, age_ms=12, received_timestamp_ns=1_700_000_000_000_000_000 + i * 100_000_000)
    metrics = probe.metrics()
    assert metrics['rate_hz'] == 10
    assert metrics['bandwidth_bps'] == 1000
    assert metrics['avg_msg_size_bytes'] == 100
    assert metrics['header_age_p95_ms'] == 12


@pytest.mark.parametrize('timestamps', [
    [0, 0, 0],                         # RMW does not supply timestamps
    [0, 1_000_000_000, 1_100_000_000],  # metadata becomes available mid-window
    [1_000_000_000, 0, 1_100_000_000],  # one message lacks metadata
    [1_100_000_000, 1_000_000_000, 1_200_000_000],  # receive clock moves backward
    [1_000_000_000] * 3,               # no measurable receive-time span
])
def test_invalid_receipt_window_uses_only_monotonic_clock(monkeypatch, timestamps):
    callbacks = iter([10, 10.2, 10.4])
    monkeypatch.setattr('ros_graph_debugger.node.time.monotonic', lambda: next(callbacks))
    probe = _TopicProbe(window=3)
    for timestamp in timestamps:
        probe.add(1, received_timestamp_ns=timestamp)
    assert probe.metrics()['rate_hz'] == 5


def test_receipt_clock_recovers_when_invalid_samples_leave_window(monkeypatch):
    callbacks = iter([10, 10.2, 10.4, 10.6])
    monkeypatch.setattr('ros_graph_debugger.node.time.monotonic', lambda: next(callbacks))
    probe = _TopicProbe(window=3)
    for timestamp in [0, 1_000_000_000, 1_100_000_000]:
        probe.add(1, received_timestamp_ns=timestamp)
    assert probe.metrics()['rate_hz'] == 5
    probe.add(1, received_timestamp_ns=1_200_000_000)
    assert probe.metrics()['rate_hz'] == 10


def test_native_callback_dispatch_remains_unwrapped(monkeypatch):
    monkeypatch.setattr(probe_executor, '_NATIVE_MESSAGE_INFO', True)
    callback = MagicMock()
    assert probe_executor.with_message_info(callback) is callback


@pytest.fixture
def executor():
    context = Context()
    rclpy.init(context=context)
    executor = probe_executor.ProbeExecutor(context=context)
    try:
        yield executor
    finally:
        executor.shutdown()
        context.shutdown()


def test_humble_probe_preserves_raw_message_and_receive_info(monkeypatch, executor):
    monkeypatch.setattr(probe_executor, '_NATIVE_MESSAGE_INFO', False)
    callback = MagicMock()
    wrapped = probe_executor.with_message_info(callback)
    info = {'received_timestamp': 1_000_000_000}
    handle = MagicMock()
    handle.take_message.return_value = (b'raw message', info)
    subscription = SimpleNamespace(handle=handle, msg_type=object, raw=True, callback=wrapped)
    packet = executor._take_subscription(subscription)
    wrapped(packet)
    callback.assert_called_once_with(b'raw message', info)
    handle.take_message.assert_called_once_with(object, True)
    handle.take_message.return_value = None
    assert executor._take_subscription(subscription) is None
    # A normal executor supplies raw bytes without the envelope; fallback works.
    wrapped(b'ordinary executor')
    callback.assert_called_with(b'ordinary executor', None)


@pytest.mark.parametrize('native', [False, True])
def test_executor_leaves_ordinary_subscriptions_on_base_path(monkeypatch, executor, native):
    monkeypatch.setattr(probe_executor, '_NATIVE_MESSAGE_INFO', native)
    monkeypatch.setattr(Executor, '_take_subscription', lambda self, sub: 'base path')
    callback = lambda raw: None
    subscription = SimpleNamespace(callback=callback)
    assert executor._take_subscription(subscription) == 'base path'


def test_collector_callback_completes_on_the_spin_thread(executor):
    # A pool schedules this callback on another thread and can return before
    # it completes. Collectors must finish directly on the spinning thread.
    node = Node('collector_dispatch_test', context=executor.context)
    calls = []
    guard = node.create_guard_condition(lambda: calls.append(threading.get_ident()))
    executor.add_node(node)
    try:
        guard.trigger()
        executor.spin_once(timeout_sec=1)
        assert calls == [threading.get_ident()]
    finally:
        executor.remove_node(node)
        node.destroy_node()
