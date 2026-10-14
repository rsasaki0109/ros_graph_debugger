"""Bad application durations must not corrupt statistics or JSON responses."""
import json
import math
import sys
from types import SimpleNamespace

import pytest

from ros_graph_debugger.processing import ProcessingTimes, value_ms
from ros_graph_debugger.bag_analyze import BagAnalyzer
from ros_graph_debugger.config import Thresholds

TOPIC = '/control/processor/debug/processing_time_ms'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), -1.0, True, '3', None, 10 ** 400])
def test_bad_values_do_not_enter_or_displace_valid_measurements(value):
    assert value_ms(SimpleNamespace(data=value)) is None
    samples = ProcessingTimes(window=2)
    for good in (2.0, 4.0):
        samples.add(TOPIC, good)
    samples.add(TOPIC, value)
    stat, = samples.stats()
    assert (stat.count, stat.mean_ms, stat.p95_ms, stat.max_ms) == (2, 3.0, 4.0, 4.0)


def test_bad_only_is_unmeasured_and_explicit_zero_is_measured():
    samples = ProcessingTimes()
    for bad in (float('nan'), float('inf'), -1):
        samples.add(TOPIC, bad)
    assert samples.stats() == []
    samples.add(TOPIC, 0)
    stat, = samples.stats()
    assert (stat.count, stat.mean_ms, stat.p95_ms, stat.max_ms) == (1, 0.0, 0.0, 0.0)


def test_finite_extreme_values_do_not_overflow_the_mean():
    samples = ProcessingTimes()
    for _ in range(2):
        samples.add(TOPIC, sys.float_info.max)
    stat, = samples.stats()
    assert math.isfinite(stat.mean_ms) and stat.mean_ms == sys.float_info.max
    json.dumps(dict(mean_ms=stat.mean_ms, p95_ms=stat.p95_ms, max_ms=stat.max_ms), allow_nan=False)


def test_bag_counts_only_valid_processing_measurements():
    analyzer = BagAnalyzer({TOPIC: 'std_msgs/msg/Float64'}, Thresholds())
    for i, value in enumerate((3.0, float('nan'), 5.0, float('inf'), -1.0)):
        stamp = 100.0 + i * .1
        analyzer.add_message(TOPIC, stamp, 8)
        analyzer.add_processing_time(stamp, TOPIC, value)
    frames = analyzer.snapshots(interval=1.0, window=1.0)
    first = frames[0]
    stat, = first['callbacks']
    assert (stat['count'], stat['mean_ms'], stat['p95_ms'], stat['max_ms']) == (2, 4.0, 5.0, 5.0)
    assert stat['source'] == 'application_report'
    json.dumps(frames, allow_nan=False)
