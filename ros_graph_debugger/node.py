"""The single rclpy node that drives every collector.

One node owns all timers and subscriptions so they share a single executor.
Collectors write into a RuntimeGraphStore; the web server reads from it. The
design is deliberately *passive by default*: the graph and QoS come from the
ROS graph API (no data subscriptions), and message-rate probing is opt-in and
refuses large sensor topics unless explicitly asked.
"""

from __future__ import annotations

import fnmatch
import re
import time
from collections import deque

import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)

from diagnostic_msgs.msg import DiagnosticArray
from tf2_msgs.msg import TFMessage

from .analysis import analyze
from .config import ProbeConfig, Thresholds
from .graph_build import build_graph
from .msgutil import header_stamp_age_ms
from .procmap import match_nodes_to_processes
from .model import (
    DiagnosticStatus,
    RuntimeGraphStore,
    TfEdge,
    TopicInfo,
)

try:
    import psutil
except Exception:  # pragma: no cover - psutil is an optional nicety
    psutil = None

# Message types whose bandwidth makes blind subscription dangerous. Probed
# only when the user explicitly opts in.
LARGE_TYPES = {
    'sensor_msgs/msg/Image',
    'sensor_msgs/msg/CompressedImage',
    'sensor_msgs/msg/PointCloud2',
    'sensor_msgs/msg/LaserScan',
}

# Internal / noisy topics we never auto-probe.
SKIP_TOPICS = {'/parameter_events', '/rosout'}


class _TopicProbe:
    """Tracks arrival times and serialized sizes for one probed topic."""

    def __init__(self, window: int) -> None:
        self.stamps: deque[float] = deque(maxlen=window)
        self.sizes: deque[int] = deque(maxlen=window)
        self.ages: deque[float] = deque(maxlen=window)  # header.stamp ages (ms)
        # None=unknown, True=has header, False=headerless (stop deserializing).
        self.has_header = None

    def add(self, size: int, age_ms: float | None = None) -> None:
        self.stamps.append(time.monotonic())
        self.sizes.append(size)
        if age_ms is not None:
            self.ages.append(age_ms)

    def metrics(self) -> dict:
        rate = None
        if len(self.stamps) >= 2:
            span = self.stamps[-1] - self.stamps[0]
            if span > 0:
                rate = (len(self.stamps) - 1) / span
        avg = sum(self.sizes) / len(self.sizes) if self.sizes else None
        bw = (avg * rate) if (avg is not None and rate is not None) else None
        return {
            'rate_hz': round(rate, 2) if rate is not None else None,
            'avg_msg_size_bytes': round(avg, 1) if avg is not None else None,
            'p95_msg_size_bytes': _percentile(self.sizes, 95),
            'bandwidth_bps': round(bw, 1) if bw is not None else None,
            'last_seen_time': time.time() if self.stamps else None,
            'header_age_ms': _percentile(self.ages, 50),
            'header_age_p95_ms': _percentile(self.ages, 95),
        }


def _percentile(values, pct: float):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int((pct / 100.0) * len(ordered)))
    return round(float(ordered[idx]), 1)


class DebuggerNode(Node):
    def __init__(self, store: RuntimeGraphStore, probe: ProbeConfig,
                 thresholds: Thresholds, profile_name: str | None) -> None:
        super().__init__('ros_graph_debugger')
        self.store = store
        self.probe_cfg = probe
        self.thresholds = thresholds
        store.set_profile(profile_name)

        self._probes: dict[str, _TopicProbe] = {}
        self._subs: dict[str, object] = {}
        self._probe_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
        )

        # TF / diagnostics live state.
        self._tf_edges: dict[tuple[str, str], TfEdge] = {}
        self._diagnostics: dict[str, DiagnosticStatus] = {}

        # Passive infrastructure subscriptions (small, safe).
        tf_qos = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT,
                            durability=DurabilityPolicy.VOLATILE,
                            history=HistoryPolicy.KEEP_LAST, depth=50)
        self.create_subscription(TFMessage, '/tf', self._on_tf, tf_qos)
        static_qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE,
                                durability=DurabilityPolicy.TRANSIENT_LOCAL,
                                history=HistoryPolicy.KEEP_LAST, depth=10)
        self.create_subscription(TFMessage, '/tf_static',
                                 self._on_tf_static, static_qos)
        self.create_subscription(DiagnosticArray, '/diagnostics',
                                 self._on_diagnostics, 10)

        # Periodic work.
        self.create_timer(1.0, self._poll_graph)
        self.create_timer(2.0, self._poll_processes)
        self.create_timer(1.0, self._refresh_metrics_and_analyze)

        self.get_logger().info('ros_graph_debugger node started')

    # ---------------------------------------------------------------- graph #
    def _poll_graph(self) -> None:
        try:
            self._collect_graph()
        except Exception as exc:  # never let a poll kill the node
            self.get_logger().warn(f'graph poll failed: {exc}')

    def _collect_graph(self) -> None:
        nodes, topics = build_graph(self)
        self.store.set_graph(nodes, topics)
        self._sync_probes(topics)

    # --------------------------------------------------------------- probes #
    def _wants_probe(self, name: str, ttype: str) -> bool:
        if not self.probe_cfg.enabled:
            return False
        if name in SKIP_TOPICS:
            return False
        # Explicit allowlist / regex always win (even for large types).
        explicit = any(fnmatch.fnmatch(name, p)
                       for p in self.probe_cfg.include_patterns)
        if self.probe_cfg.regex and re.search(self.probe_cfg.regex, name):
            explicit = True
        if explicit:
            return True
        if self.probe_cfg.include_patterns or self.probe_cfg.regex:
            # User narrowed the scope; don't auto-probe everything else.
            return False
        if ttype in LARGE_TYPES and not self.probe_cfg.allow_large:
            return False
        return True

    def _sync_probes(self, topics: dict[str, TopicInfo]) -> None:
        for name, t in topics.items():
            if name in self._subs:
                continue
            if len(self._subs) >= self.probe_cfg.max_topics:
                break
            if not self._wants_probe(name, t.type):
                continue
            self._start_probe(name, t.type)

    def _start_probe(self, name: str, ttype: str) -> None:
        try:
            from rosidl_runtime_py.utilities import get_message
            msg_type = get_message(ttype)
        except Exception as exc:
            self.get_logger().debug(f'cannot resolve type for {name}: {exc}')
            return
        probe = _TopicProbe(self.probe_cfg.window)
        self._probes[name] = probe

        def _cb(raw: bytes, _name=name, _probe=probe, _mt=msg_type) -> None:
            try:
                size = len(raw) if isinstance(raw, (bytes, bytearray)) \
                    else len(bytes(raw))
            except Exception:
                size = 0
            age = None
            # Best-effort latency Tier A: read header.stamp if the type has one.
            # Once we learn a type is headerless we stop paying for deserialize.
            if _probe.has_header is not False:
                try:
                    from rclpy.serialization import deserialize_message
                    msg = deserialize_message(bytes(raw), _mt)
                    age = header_stamp_age_ms(msg, time.time())
                    _probe.has_header = hasattr(msg, 'header')
                except Exception:
                    _probe.has_header = False
            _probe.add(size, age)

        try:
            sub = self.create_subscription(
                msg_type, name, _cb, self._probe_qos, raw=True)
            self._subs[name] = sub
            self.get_logger().info(f'probing {name} ({ttype})')
        except Exception as exc:
            self.get_logger().debug(f'probe subscription failed for {name}: {exc}')
            self._probes.pop(name, None)

    # ------------------------------------------------------------------- tf #
    def _on_tf(self, msg: TFMessage) -> None:
        self._ingest_tf(msg, static=False)

    def _on_tf_static(self, msg: TFMessage) -> None:
        self._ingest_tf(msg, static=True)

    def _ingest_tf(self, msg: TFMessage, static: bool) -> None:
        now = time.time()
        for tr in msg.transforms:
            parent = tr.header.frame_id
            child = tr.child_frame_id
            self._tf_edges[(parent, child)] = TfEdge(
                parent=parent, child=child,
                last_update_time=now, age_ms=0.0, static=static)

    def _tf_snapshot(self) -> dict[tuple[str, str], TfEdge]:
        now = time.time()
        out: dict[tuple[str, str], TfEdge] = {}
        for key, edge in self._tf_edges.items():
            age = (now - edge.last_update_time) * 1000.0 \
                if edge.last_update_time else None
            status = 'ok'
            if not edge.static and age is not None \
                    and age > self.thresholds.tf_stale_ms:
                status = 'critical'
            out[key] = TfEdge(parent=edge.parent, child=edge.child,
                              last_update_time=edge.last_update_time,
                              age_ms=round(age, 1) if age is not None else None,
                              static=edge.static, status=status)
        return out

    # ---------------------------------------------------------- diagnostics #
    def _on_diagnostics(self, msg: DiagnosticArray) -> None:
        for st in msg.status:
            self._diagnostics[st.name] = DiagnosticStatus(
                name=st.name, level=int.from_bytes(st.level, 'big')
                if isinstance(st.level, bytes) else int(st.level),
                message=st.message, hardware_id=st.hardware_id)

    # ------------------------------------------------------------ processes #
    def _poll_processes(self) -> None:
        if psutil is None:
            return
        try:
            procs = _collect_processes()
        except Exception:
            return
        nodes, _, _, _, _ = self.store.working_copy()
        matches = match_nodes_to_processes(list(nodes), procs)
        for node_id, m in matches.items():
            try:
                proc = psutil.Process(m['pid'])
                cpu = proc.cpu_percent(interval=None)
                rss = proc.memory_info().rss
                pname = proc.name()
            except Exception:
                continue
            self.store.update_node_process(
                node_id, pid=m['pid'], process_name=pname, cpu_percent=cpu,
                rss_bytes=rss, process_mapping_confidence=m['confidence'])

    # --------------------------------------------------- metrics + analyze #
    def _refresh_metrics_and_analyze(self) -> None:
        for name, probe in self._probes.items():
            self.store.update_topic_metrics(name, **probe.metrics())
        self.store.set_tf(self._tf_snapshot())
        self.store.set_diagnostics(dict(self._diagnostics))
        try:
            issues = analyze(self.store, self.thresholds)
            self.store.set_issues(issues)
        except Exception as exc:
            self.get_logger().warn(f'analysis failed: {exc}')


def _collect_processes() -> list[dict]:
    """Snapshot live processes as plain dicts for ``match_nodes_to_processes``."""
    procs: list[dict] = []
    if psutil is None:
        return procs
    for proc in psutil.process_iter(['pid', 'cmdline']):
        try:
            procs.append({'pid': proc.info['pid'],
                          'cmdline': proc.info.get('cmdline') or []})
        except Exception:
            continue
    return procs
