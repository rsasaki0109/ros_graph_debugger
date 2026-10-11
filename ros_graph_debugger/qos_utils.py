"""Helpers to turn rclpy QoS profiles into JSON-friendly ``QoSInfo``. The
compatibility rules (the classic "connected in the graph but no data flows"
trap) live in the ROS-free `qos_compat`."""

from __future__ import annotations

from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSLivelinessPolicy,
    QoSReliabilityPolicy,
)

from .model import QoSInfo
from .qos_compat import detect_qos_status  # noqa: F401  (re-exported)

_RELIABILITY = {
    QoSReliabilityPolicy.RELIABLE: 'reliable',
    QoSReliabilityPolicy.BEST_EFFORT: 'best_effort',
    QoSReliabilityPolicy.SYSTEM_DEFAULT: 'system_default',
}
_DURABILITY = {
    QoSDurabilityPolicy.TRANSIENT_LOCAL: 'transient_local',
    QoSDurabilityPolicy.VOLATILE: 'volatile',
    QoSDurabilityPolicy.SYSTEM_DEFAULT: 'system_default',
}
_HISTORY = {
    QoSHistoryPolicy.KEEP_LAST: 'keep_last',
    QoSHistoryPolicy.KEEP_ALL: 'keep_all',
    QoSHistoryPolicy.SYSTEM_DEFAULT: 'system_default',
}
_LIVELINESS = {
    QoSLivelinessPolicy.AUTOMATIC: 'automatic',
    QoSLivelinessPolicy.MANUAL_BY_TOPIC: 'manual_by_topic',
    QoSLivelinessPolicy.SYSTEM_DEFAULT: 'system_default',
}


# rmw implementations encode "infinite" differently (about 2^31 s in Fast DDS,
# about 2^63 ns in Cyclone); any period beyond ~3 years is treated as infinite.
_INFINITE_NS = 10 ** 17


def _duration_ms(duration) -> float | None:
    """An rclpy Duration as ms; None for unset (0) or infinite."""
    ns = getattr(duration, 'nanoseconds', None)
    if not isinstance(ns, int) or ns <= 0 or ns >= _INFINITE_NS:
        return None
    return ns / 1e6


def endpoint_to_qos_info(node: str, endpoint_type: str, qos) -> QoSInfo:
    return QoSInfo(
        node=node,
        endpoint_type=endpoint_type,
        reliability=_RELIABILITY.get(qos.reliability, 'unknown'),
        durability=_DURABILITY.get(qos.durability, 'unknown'),
        history=_HISTORY.get(qos.history, 'unknown'),
        depth=int(getattr(qos, 'depth', 0) or 0),
        liveliness=_LIVELINESS.get(qos.liveliness, 'unknown'),
        deadline_ms=_duration_ms(getattr(qos, 'deadline', None)),
        liveliness_lease_ms=_duration_ms(
            getattr(qos, 'liveliness_lease_duration', None)),
    )
