"""Runtime data model for ros_graph_debugger.

Plain dataclasses describe the live ROS graph plus overlaid metrics, and a
thread-safe store assembles them into a JSON-serializable snapshot. The store
is written from the rclpy spin thread (collectors) and read from the asyncio
web server thread, so every public accessor takes the lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# Severity / status vocabulary (kept as plain strings for JSON friendliness).
# --------------------------------------------------------------------------- #
CRITICAL = 'critical'
WARNING = 'warning'
INFO = 'info'
OK = 'ok'
UNKNOWN = 'unknown'


@dataclass
class QoSInfo:
    """QoS of a single publisher/subscriber endpoint on a topic."""

    node: str = ''
    endpoint_type: str = ''  # 'publisher' | 'subscriber'
    reliability: str = UNKNOWN
    durability: str = UNKNOWN
    history: str = UNKNOWN
    depth: int = 0
    liveliness: str = UNKNOWN


@dataclass
class TopicInfo:
    name: str
    type: str = ''
    publisher_count: int = 0
    subscriber_count: int = 0
    publishers: list[str] = field(default_factory=list)
    subscribers: list[str] = field(default_factory=list)
    qos_endpoints: list[QoSInfo] = field(default_factory=list)

    # Overlaid runtime metrics (None == not probed / unknown).
    probed: bool = False
    rate_hz: Optional[float] = None
    bandwidth_bps: Optional[float] = None
    avg_msg_size_bytes: Optional[float] = None
    p95_msg_size_bytes: Optional[float] = None
    last_seen_time: Optional[float] = None
    age_ms: Optional[float] = None

    qos_status: str = UNKNOWN  # ok | mismatch | risk | unknown
    status: str = UNKNOWN  # ok | warning | critical | unknown


@dataclass
class NodeInfo:
    id: str  # fully-qualified name, e.g. /perception/detector
    name: str = ''
    namespace: str = '/'
    publishers: list[str] = field(default_factory=list)
    subscribers: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)

    pid: Optional[int] = None
    process_name: Optional[str] = None
    cpu_percent: Optional[float] = None
    rss_bytes: Optional[int] = None
    # none | low | medium | high — be honest about node->process mapping.
    process_mapping_confidence: str = 'none'

    status: str = OK


@dataclass
class EdgeInfo:
    from_node: str
    to_node: str
    topic: str
    type: str = ''
    rate_hz: Optional[float] = None
    bandwidth_bps: Optional[float] = None
    status: str = UNKNOWN


@dataclass
class TfEdge:
    parent: str
    child: str
    last_update_time: Optional[float] = None
    age_ms: Optional[float] = None
    static: bool = False
    status: str = OK  # ok | warning(stale) | critical


@dataclass
class DiagnosticStatus:
    name: str
    level: int = 0  # 0 OK, 1 WARN, 2 ERROR, 3 STALE
    message: str = ''
    hardware_id: str = ''


@dataclass
class Issue:
    id: str
    severity: str
    kind: str
    title: str
    explanation: str = ''
    evidence: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)
    related_nodes: list[str] = field(default_factory=list)
    related_topics: list[str] = field(default_factory=list)
    related_frames: list[str] = field(default_factory=list)


@dataclass
class GraphSnapshot:
    timestamp: float
    profile: Optional[str] = None
    nodes: list[dict] = field(default_factory=list)
    topics: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    tf_edges: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class RuntimeGraphStore:
    """Thread-safe holder of the latest collected state.

    Collectors push their findings here; the web server pulls assembled
    snapshots. Each field is replaced wholesale under the lock, so readers
    always observe a consistent view of a given category.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: dict[str, NodeInfo] = {}
        self._topics: dict[str, TopicInfo] = {}
        self._tf: dict[tuple[str, str], TfEdge] = {}
        self._diagnostics: dict[str, DiagnosticStatus] = {}
        self._issues: list[Issue] = []
        self._profile: Optional[str] = None

    # -- writers (called from the rclpy spin thread) ------------------------ #
    def set_graph(self, nodes: dict[str, NodeInfo],
                  topics: dict[str, TopicInfo]) -> None:
        with self._lock:
            # Preserve already-collected metrics across graph re-polls.
            for name, new in topics.items():
                old = self._topics.get(name)
                if old is not None and old.probed:
                    new.probed = old.probed
                    new.rate_hz = old.rate_hz
                    new.bandwidth_bps = old.bandwidth_bps
                    new.avg_msg_size_bytes = old.avg_msg_size_bytes
                    new.p95_msg_size_bytes = old.p95_msg_size_bytes
                    new.last_seen_time = old.last_seen_time
                    new.age_ms = old.age_ms
            for nid, new in nodes.items():
                old = self._nodes.get(nid)
                if old is not None:
                    new.pid = old.pid
                    new.process_name = old.process_name
                    new.cpu_percent = old.cpu_percent
                    new.rss_bytes = old.rss_bytes
                    new.process_mapping_confidence = old.process_mapping_confidence
            self._nodes = nodes
            self._topics = topics

    def update_topic_metrics(self, name: str, **metrics) -> None:
        with self._lock:
            topic = self._topics.get(name)
            if topic is None:
                return
            topic.probed = True
            for key, value in metrics.items():
                setattr(topic, key, value)

    def update_node_process(self, node_id: str, **metrics) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                return
            for key, value in metrics.items():
                setattr(node, key, value)

    def set_tf(self, edges: dict[tuple[str, str], TfEdge]) -> None:
        with self._lock:
            self._tf = edges

    def set_diagnostics(self, diags: dict[str, DiagnosticStatus]) -> None:
        with self._lock:
            self._diagnostics = diags

    def set_issues(self, issues: list[Issue]) -> None:
        with self._lock:
            self._issues = issues

    def set_profile(self, profile: Optional[str]) -> None:
        with self._lock:
            self._profile = profile

    # -- readers (called from collectors and the web server) ---------------- #
    def working_copy(self) -> tuple[dict[str, NodeInfo], dict[str, TopicInfo],
                                    dict[tuple[str, str], TfEdge],
                                    dict[str, DiagnosticStatus]]:
        """Return references for read-only analysis within the spin thread."""
        with self._lock:
            return (dict(self._nodes), dict(self._topics),
                    dict(self._tf), dict(self._diagnostics))

    def probe_candidates(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(t.name, t.type) for t in self._topics.values()]

    def snapshot(self) -> GraphSnapshot:
        with self._lock:
            edges = _build_edges(self._nodes, self._topics)
            return GraphSnapshot(
                timestamp=time.time(),
                profile=self._profile,
                nodes=[asdict(n) for n in self._nodes.values()],
                topics=[asdict(t) for t in self._topics.values()],
                edges=[asdict(e) for e in edges],
                tf_edges=[asdict(e) for e in self._tf.values()],
                diagnostics=[asdict(d) for d in self._diagnostics.values()],
                issues=[asdict(i) for i in self._issues],
            )


def _build_edges(nodes: dict[str, NodeInfo],
                 topics: dict[str, TopicInfo]) -> list[EdgeInfo]:
    """Expand publisher/subscriber relations into node->node edges per topic."""
    edges: list[EdgeInfo] = []
    for topic in topics.values():
        for pub in topic.publishers:
            for sub in topic.subscribers:
                edges.append(EdgeInfo(
                    from_node=pub,
                    to_node=sub,
                    topic=topic.name,
                    type=topic.type,
                    rate_hz=topic.rate_hz,
                    bandwidth_bps=topic.bandwidth_bps,
                    status=topic.status,
                ))
    return edges
