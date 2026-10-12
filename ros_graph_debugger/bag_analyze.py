"""Offline analysis of a rosbag2 recording (sqlite3 or MCAP).

A bag is the most common artifact a robotics team already has — from the robot,
from simulation, from CI. This turns one into the same snapshot recording
`rgd record` writes, so `rgd report` / `diff` / `assert` / `serve` work on it
unchanged, with no DDS and no live system.

The bag is cut into windows (``interval`` apart, each covering the trailing
``window`` seconds of bag time). Per window and topic: message rate, bandwidth,
size, and — for stamped types — header-stamp age (recorded ROS clock when
available, otherwise bag receive time, minus ``header.stamp``);
`/tf`, `/tf_static` and `/diagnostics` feed TF freshness and
diagnostics. Each window then runs through the live issue engine on the bag's
own clock, with the same profile expectations.

What a bag cannot tell: which nodes published or subscribed (it records topics,
not the graph), process CPU, or callback times — those sections stay empty.

The core is pure (`stamp_from_cdr`, `BagAnalyzer`) and tested without ROS;
`read_bag` is the rosbag2_py reader, imported lazily.
"""

from __future__ import annotations

import bisect
import math
import os
import struct
from dataclasses import dataclass, field

from .analysis import analyze
from .model import DiagnosticStatus, RuntimeGraphStore, TfEdge, TopicInfo
from .processing import ProcessingTimes, compile_patterns, matches, value_ms

TF_TOPICS = ('/tf', '/tf_static')
DIAGNOSTICS_TOPIC = '/diagnostics'
# Fewer messages than this over the whole bag = a one-shot / latched topic
# (a map, a static TF): never "stale" just because it was sent once.
_MIN_STREAMING = 3


def _time_from_cdr(raw: bytes, *, zero_valid: bool = False) -> float | None:
    if len(raw) < 12 or raw[0] != 0 or raw[1] not in (0, 1):
        return None
    fmt = '<iI' if raw[1] == 1 else '>iI'
    sec, nanosec = struct.unpack_from(fmt, raw, 4)
    if sec < 0 or nanosec >= 1_000_000_000 or (not zero_valid and sec == nanosec == 0):
        return None
    return sec + nanosec * 1e-9


def stamp_from_cdr(raw: bytes) -> float | None:
    """``header.stamp`` (epoch seconds) of a CDR-serialized message whose
    first field is a ``std_msgs/Header``, read without deserializing — so even
    multi-megabyte images cost nothing. None if unset or unreadable."""
    return _time_from_cdr(raw)


def _p(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    return round(float(ordered[min(len(ordered) - 1, int(pct / 100 * len(ordered)))]), 1)


@dataclass
class _Series:
    """Per-topic arrivals in bag time, kept sorted for window slicing."""
    t: list = field(default_factory=list)
    size: list = field(default_factory=list)
    age_ms: list = field(default_factory=list)  # None where unstamped


class BagAnalyzer:
    """Accumulate a bag's messages, then emit one snapshot dict per window."""

    def __init__(self, topic_types: dict[str, str], thresholds,
                 profile_name: str | None = None, *, age_clock: str = 'bag') -> None:
        if age_clock not in ('bag', 'ros'):
            raise ValueError('age_clock must be bag or ros')
        self.topic_types = dict(topic_types)
        self.thresholds = thresholds
        self.profile_name = profile_name
        self.age_clock = age_clock
        self.clock_messages = 0
        self.unreadable_clock_messages = 0
        self._ros_clock = None
        self._series: dict[str, _Series] = {name: _Series() for name in topic_types}
        self._tf: list[tuple[float, str, str, bool]] = []
        self._diag: list[tuple[float, DiagnosticStatus]] = []
        self._proc: list[tuple[float, str, float]] = []

    # -- input -------------------------------------------------------------- #
    def update_ros_clock(self, value: float | None) -> None:
        self.clock_messages += 1
        if value is None or not math.isfinite(value) or value < 0:
            self.unreadable_clock_messages += 1
            value = None
        self._ros_clock = value

    def add_message(self, topic: str, t: float, size: int,
                    stamp: float | None = None) -> None:
        s = self._series.setdefault(topic, _Series())
        s.t.append(t)
        s.size.append(size)
        now = t if self.age_clock == 'bag' else self._ros_clock
        # Match live ROS-clock startup: a zero/unavailable clock cannot measure
        # header age. Arrival windows and receipt freshness still use bag time.
        known = now is not None and (self.age_clock == 'bag' or now > 0)
        s.age_ms.append(max(0.0, (now - stamp) * 1000.0) if stamp and known else None)

    def add_tf(self, t: float, parent: str, child: str, static: bool) -> None:
        self._tf.append((t, parent, child, static))

    def add_diagnostic(self, t: float, status: DiagnosticStatus) -> None:
        self._diag.append((t, status))

    def add_processing_time(self, t: float, topic: str, value_ms: float) -> None:
        self._proc.append((t, topic, value_ms))

    # -- output ------------------------------------------------------------- #
    def time_range(self) -> tuple[float, float] | None:
        times = [s.t[0] for s in self._series.values() if s.t] + \
                [s.t[-1] for s in self._series.values() if s.t]
        return (min(times), max(times)) if times else None

    def snapshots(self, interval: float = 1.0, window: float = 2.0) -> list[dict]:
        rng = self.time_range()
        if rng is None:
            return []
        start, end = rng
        for s in self._series.values():  # bags are time-ordered; be safe
            order = sorted(range(len(s.t)), key=s.t.__getitem__)
            s.t, s.size, s.age_ms = ([s.t[i] for i in order], [s.size[i] for i in order],
                                     [s.age_ms[i] for i in order])
        self._tf.sort(key=lambda x: x[0])
        self._diag.sort(key=lambda x: x[0])
        self._proc.sort(key=lambda x: x[0])

        out = []
        # The first window is a full one, so early rates are not underestimated.
        e = min(start + window, end)
        while True:
            out.append(self._snapshot_at(e, window))
            if e >= end:
                break
            e = min(e + interval, end)
        return out

    def _snapshot_at(self, e: float, window: float) -> dict:
        topics = {}
        metrics = {}
        for name, s in self._series.items():
            topics[name] = TopicInfo(name=name, type=self.topic_types.get(name, ''))
            hi = bisect.bisect_right(s.t, e)
            if hi == 0:
                continue  # nothing yet on this topic
            lo = bisect.bisect_right(s.t, e - window)
            sizes = s.size[lo:hi]
            ages = [a for a in s.age_ms[lo:hi] if a is not None]
            streaming = len(s.t) >= _MIN_STREAMING and name != '/tf_static'
            metrics[name] = {
                'rate_hz': round(len(sizes) / window, 2),
                'bandwidth_bps': round(sum(sizes) / window, 1),
                'avg_msg_size_bytes': round(sum(sizes) / len(sizes), 1) if sizes else None,
                'p95_msg_size_bytes': _p(sizes, 95),
                'last_seen_time': s.t[hi - 1] if streaming else None,
                'header_age_ms': _p(ages, 50),
                'header_age_p95_ms': _p(ages, 95),
            }

        store = RuntimeGraphStore()
        store.set_profile(self.profile_name)
        store.set_graph({}, topics)
        for name, m in metrics.items():
            store.update_topic_metrics(name, **m)
        store.set_tf(self._tf_at(e))
        store.set_diagnostics({st.name: st for t, st in self._diag if t <= e})
        if self._proc:
            window_proc = ProcessingTimes(window=10 ** 6)
            for t, topic, v in self._proc:
                if e - window < t <= e:
                    window_proc.add(topic, v)
            # No graph in a bag: the node comes from the ~/debug/ convention.
            store.set_processing_times(window_proc.stats())
        store.set_issues(analyze(store, self.thresholds, now=e))
        snap = store.snapshot().to_dict()
        snap['timestamp'] = e
        return snap

    def _tf_at(self, e: float) -> dict:
        latest: dict[tuple[str, str], tuple[float, bool]] = {}
        for t, parent, child, static in self._tf:
            if t > e:
                break
            latest[(parent, child)] = (t, static)
        edges = {}
        for (parent, child), (t, static) in latest.items():
            age = (e - t) * 1000.0
            stale = not static and age > self.thresholds.tf_stale_ms
            edges[(parent, child)] = TfEdge(
                parent=parent, child=child, last_update_time=t,
                age_ms=round(age, 1), static=static,
                status='critical' if stale else 'ok')
        return edges


# --------------------------------------------------------------------------- #
# rosbag2_py reader
# --------------------------------------------------------------------------- #
class BagReaderUnavailable(RuntimeError):
    """rosbag2_py (a ROS 2 install) is not importable."""


def _storage_id(path: str) -> str:
    """Pick the storage plugin: from metadata.yaml, else from the extension."""
    meta = os.path.join(path, 'metadata.yaml') if os.path.isdir(path) else None
    if meta and os.path.isfile(meta):
        import yaml
        with open(meta) as f:
            info = (yaml.safe_load(f) or {}).get('rosbag2_bagfile_information', {})
        if info.get('storage_identifier'):
            return info['storage_identifier']
        files = info.get('relative_file_paths') or []
        path = files[0] if files else path
    return 'mcap' if str(path).endswith('.mcap') else 'sqlite3'


def _first_field_is_header(msg_type) -> bool:
    try:
        fields = msg_type.get_fields_and_field_types()
    except Exception:
        return False
    first = next(iter(fields.items()), None)
    return bool(first) and first[0] == 'header' and first[1] == 'std_msgs/Header'


def analyze_bag(path: str, thresholds, profile_name: str | None = None,
                processing_time_patterns=None, *, age_clock: str = 'auto') -> BagAnalyzer:
    """Read every message of the bag at ``path`` into a ``BagAnalyzer``.
    Topics matching ``processing_time_patterns`` also feed processing time."""
    if age_clock not in ('auto', 'bag', 'ros'):
        raise ValueError('age_clock must be auto, bag or ros')
    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message
    except ImportError as exc:
        raise BagReaderUnavailable(
            'reading bags needs a sourced ROS 2 install (rosbag2_py); for MCAP '
            'on Humble also ros-humble-rosbag2-storage-mcap') from exc

    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=path, storage_id=_storage_id(path)),
                rosbag2_py.ConverterOptions(input_serialization_format='cdr',
                                            output_serialization_format='cdr'))
    types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    recorded_clock = types.get('/clock') == 'rosgraph_msgs/msg/Clock'
    if age_clock == 'ros' and not recorded_clock:
        raise ValueError('--age-clock ros requires a recorded /clock (rosgraph_msgs/msg/Clock)')
    selected_clock = 'ros' if age_clock == 'ros' or (age_clock == 'auto' and recorded_clock) else 'bag'
    analyzer = BagAnalyzer(types, thresholds, profile_name, age_clock=selected_clock)

    proc = compile_patterns(processing_time_patterns)
    msg_types, stamped = {}, {}
    for name, ttype in types.items():
        try:
            msg_types[name] = get_message(ttype)
        except Exception:
            msg_types[name] = None  # type not installed: still count it
        stamped[name] = msg_types[name] is not None and \
            _first_field_is_header(msg_types[name])

    while reader.has_next():
        topic, raw, t_ns = reader.read_next()
        t = t_ns * 1e-9
        if topic == '/clock' and recorded_clock and selected_clock == 'ros':
            # Clock starts with builtin_interfaces/Time, not std_msgs/Header;
            # decode even if its Python binding is not installed.
            analyzer.update_ros_clock(_time_from_cdr(raw, zero_valid=True))
        stamp = stamp_from_cdr(raw) if stamped.get(topic) else None
        analyzer.add_message(topic, t, len(raw), stamp)
        mt = msg_types.get(topic)
        if mt is None:
            continue
        if proc and matches(topic, proc):
            try:
                v = value_ms(deserialize_message(raw, mt))
            except Exception:
                v = None
            if v is not None:
                analyzer.add_processing_time(t, topic, v)
        elif topic in TF_TOPICS:
            try:
                msg = deserialize_message(raw, mt)
            except Exception:
                continue
            for tr in msg.transforms:
                analyzer.add_tf(t, tr.header.frame_id, tr.child_frame_id,
                                static=topic == '/tf_static')
        elif topic == DIAGNOSTICS_TOPIC:
            try:
                msg = deserialize_message(raw, mt)
            except Exception:
                continue
            for st in msg.status:
                level = int.from_bytes(st.level, 'big') \
                    if isinstance(st.level, bytes) else int(st.level)
                analyzer.add_diagnostic(t, DiagnosticStatus(
                    name=st.name, level=level, message=st.message,
                    hardware_id=st.hardware_id))
    return analyzer
