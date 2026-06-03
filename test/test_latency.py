"""Header-stamp age (latency Tier A): the pure helper, probe stats, and the
freshness issue rule. No DDS — message age is computed from duck-typed stamps."""

from types import SimpleNamespace

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.model import NodeInfo, RuntimeGraphStore, TopicInfo
from ros_graph_debugger.msgutil import header_stamp_age_ms
from ros_graph_debugger.node import _TopicProbe, _percentile


def _msg(sec, nanosec=0):
    return SimpleNamespace(header=SimpleNamespace(
        stamp=SimpleNamespace(sec=sec, nanosec=nanosec)))


def test_header_stamp_age_basic():
    # stamp at t=100.0, now at t=100.5 -> 500 ms old
    assert header_stamp_age_ms(_msg(100, 0), 100.5) == 500.0


def test_header_stamp_age_clamps_negative():
    # stamp in the (slight) future -> clamped to 0
    assert header_stamp_age_ms(_msg(100, 0), 99.9) == 0.0


def test_header_stamp_age_none_cases():
    assert header_stamp_age_ms(SimpleNamespace(), 100.0) is None       # no header
    assert header_stamp_age_ms(_msg(0, 0), 100.0) is None              # unset stamp
    no_sec = SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(nanosec=5)))
    assert header_stamp_age_ms(no_sec, 100.0) is None                  # missing sec


def test_percentile():
    assert _percentile([], 50) is None
    assert _percentile([10, 20, 30, 40], 95) == 40.0
    assert _percentile([5], 50) == 5.0


def test_topicprobe_tracks_age():
    p = _TopicProbe(window=50)
    p.add(100, age_ms=40.0)
    p.add(200, age_ms=160.0)
    m = p.metrics()
    assert m['header_age_ms'] is not None
    assert m['header_age_p95_ms'] == 160.0
    # size metrics still work
    assert m['p95_msg_size_bytes'] == 200.0


def test_topicprobe_no_age_when_headerless():
    p = _TopicProbe(window=50)
    p.add(100)  # no age provided
    p.add(200)
    m = p.metrics()
    assert m['header_age_ms'] is None
    assert m['header_age_p95_ms'] is None


def test_analyzer_stale_data_rule_via_max_age():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/localization/kinematic_state', publisher_count=1,
                  subscriber_count=1, publishers=['/ndt'], subscribers=['/planner'])
    store.set_graph({'/ndt': NodeInfo(id='/ndt', name='ndt'),
                     '/planner': NodeInfo(id='/planner', name='planner')},
                    {t.name: t})
    store.update_topic_metrics(t.name, rate_hz=30.0, header_age_p95_ms=420.0)

    thr = Thresholds(expected_max_age_ms={'/localization/kinematic_state': 100.0})
    issues = analyze(store, thr)
    stale = [i for i in issues if i.kind == 'stale_data']
    assert stale, 'expected a stale_data issue when age p95 exceeds max_age'
    assert '/localization/kinematic_state' in stale[0].related_topics
    assert any('420' in e for e in stale[0].evidence)


def test_analyzer_stale_data_pattern_max_age():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/perception/foo', publisher_count=1, subscriber_count=1,
                  publishers=['/p'], subscribers=['/s'])
    store.set_graph({'/p': NodeInfo(id='/p', name='p'),
                     '/s': NodeInfo(id='/s', name='s')}, {t.name: t})
    store.update_topic_metrics(t.name, header_age_p95_ms=300.0)

    thr = Thresholds()
    thr.set_patterns(max_age=[('^/perception/.*', 150.0)])
    kinds = {i.kind for i in analyze(store, thr)}
    assert 'stale_data' in kinds
