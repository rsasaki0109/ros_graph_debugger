"""A retained rate must expose its sampled interval independently of silence."""

import pytest

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.model import RuntimeGraphStore, TopicInfo
from ros_graph_debugger.node import _TopicProbe


def test_silence_keeps_interval_but_advances_monotonic_sample_age(monkeypatch):
    mono, wall = [10.0], [1000.0]
    monkeypatch.setattr('ros_graph_debugger.node.time.monotonic', lambda: mono[0])
    monkeypatch.setattr('ros_graph_debugger.node.time.time', lambda: wall[0])
    probe = _TopicProbe(window=3)
    for i in range(3):
        mono[0] = 10 + i * 0.2
        probe.add(10, received_timestamp_ns=1_000_000_000 + i * 100_000_000)
    before = probe.metrics()
    assert before['rate_hz'] == 10
    assert before['probe_rate_window_samples'] == 3
    assert before['probe_rate_window_duration_ms'] == 200
    assert before['probe_rate_window_clock'] == 'rmw_received_timestamp'
    assert before['probe_last_sample_age_ms'] == 0
    mono[0] = 20.4
    wall[0] = 5  # System/ROS clock jumps must not erase processing silence.
    after = probe.metrics()
    assert after['probe_last_sample_age_ms'] == 10000
    assert {k: v for k, v in after.items() if k != 'probe_last_sample_age_ms'} == {
        k: v for k, v in before.items() if k != 'probe_last_sample_age_ms'}
    store = RuntimeGraphStore()
    store.set_graph({}, {'/scan': TopicInfo(name='/scan', publisher_count=1)})
    store.update_topic_metrics('/scan', **after)
    assert any(i.kind == 'topic_stale' for i in analyze(store, Thresholds(), now=1010))
    # A new processed sample resets age; old rate samples leave on replacement.
    for i in range(3):
        mono[0] = 21 + i * 0.1
        probe.add(10, received_timestamp_ns=2_000_000_000 + i * 100_000_000)
    recovered = probe.metrics()
    assert recovered['probe_last_sample_age_ms'] == 0
    assert recovered['rate_hz'] == 10
    assert recovered['probe_rate_window_duration_ms'] == 200


@pytest.mark.parametrize('received', [
    [0, 0, 0], [0, 1_000_000_000, 1_100_000_000],
    [1_000_000_000, 0, 1_100_000_000],
    [1_100_000_000, 1_000_000_000, 1_200_000_000],
    [1_000_000_000] * 3,
])
def test_fallback_duration_and_clock_describe_the_same_rate(monkeypatch, received):
    mono = [10.0]
    monkeypatch.setattr('ros_graph_debugger.node.time.monotonic', lambda: mono[0])
    probe = _TopicProbe(window=3)
    for i, rx in enumerate(received):
        mono[0] = 10 + i * 0.2
        probe.add(10, received_timestamp_ns=rx)
    metrics = probe.metrics()
    assert metrics['rate_hz'] == 5
    assert metrics['probe_rate_window_duration_ms'] == 400
    assert metrics['probe_rate_window_clock'] == 'callback_monotonic'
    assert metrics['probe_rate_window_samples'] == 3


def test_empty_single_and_unmeasurable_windows_are_not_zero_rates(monkeypatch):
    monkeypatch.setattr('ros_graph_debugger.node.time.monotonic', lambda: 10)
    probe = _TopicProbe(window=3)
    assert probe.metrics()['probe_rate_window_samples'] == 0
    assert probe.metrics()['probe_last_sample_age_ms'] is None
    for count in range(1, 4):
        probe.add(10)
        metrics = probe.metrics()
        assert metrics['probe_rate_window_samples'] == count
        assert metrics['rate_hz'] is None
        assert metrics['probe_rate_window_duration_ms'] is None
        assert metrics['probe_rate_window_clock'] is None
        assert metrics['probe_last_sample_age_ms'] == 0


def test_receipt_clock_recovery_changes_interval_and_basis_together(monkeypatch):
    mono = [10.0]
    monkeypatch.setattr('ros_graph_debugger.node.time.monotonic', lambda: mono[0])
    probe = _TopicProbe(window=3)
    for i, rx in enumerate([0, 1_000_000_000, 1_100_000_000, 1_200_000_000]):
        mono[0] = 10 + i * 0.2
        probe.add(10, received_timestamp_ns=rx)
        if i == 2:
            assert probe.metrics()['probe_rate_window_clock'] == 'callback_monotonic'
    metrics = probe.metrics()
    assert metrics['probe_rate_window_clock'] == 'rmw_received_timestamp'
    assert metrics['probe_rate_window_duration_ms'] == 200
    assert metrics['rate_hz'] == 10


def test_poll_and_statistics_preserve_probe_window_without_inventing_other_windows():
    fields = {'probe_rate_window_samples': 50, 'probe_rate_window_duration_ms': 1633.333,
              'probe_rate_window_clock': 'rmw_received_timestamp',
              'probe_last_sample_age_ms': 9000}
    store = RuntimeGraphStore()
    def graph():
        return {}, {name: TopicInfo(name=name) for name in ('/scan', '/other')}
    store.set_graph(*graph())
    store.update_topic_metrics('/scan', rate_hz=30, **fields)
    store.set_graph(*graph())
    store.set_statistics([])
    topics = {t['name']: t for t in store.snapshot().to_dict()['topics']}
    assert all(topics['/scan'][k] == v for k, v in fields.items())
    assert topics['/scan']['rate_hz'] == 30
    assert all(topics['/other'][k] is None for k in fields)
