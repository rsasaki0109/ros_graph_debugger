"""Helpers to turn rclpy QoS enums into JSON-friendly strings and to detect
publisher/subscriber QoS incompatibilities (the classic "topic is connected in
the graph but no data flows" trap)."""

from __future__ import annotations

from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSLivelinessPolicy,
    QoSReliabilityPolicy,
)

from .model import QoSInfo

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


def endpoint_to_qos_info(node: str, endpoint_type: str, qos) -> QoSInfo:
    return QoSInfo(
        node=node,
        endpoint_type=endpoint_type,
        reliability=_RELIABILITY.get(qos.reliability, 'unknown'),
        durability=_DURABILITY.get(qos.durability, 'unknown'),
        history=_HISTORY.get(qos.history, 'unknown'),
        depth=int(getattr(qos, 'depth', 0) or 0),
        liveliness=_LIVELINESS.get(qos.liveliness, 'unknown'),
    )


def detect_qos_status(endpoints: list[QoSInfo]) -> str:
    """Return 'ok' | 'mismatch' | 'risk' | 'unknown' for a topic's endpoints.

    A request (subscriber) is incompatible with an offer (publisher) when the
    subscriber demands a stronger guarantee than the publisher provides.
    """
    pubs = [e for e in endpoints if e.endpoint_type == 'publisher']
    subs = [e for e in endpoints if e.endpoint_type == 'subscriber']
    if not pubs or not subs:
        return 'unknown'

    for pub in pubs:
        for sub in subs:
            # Reliability: BEST_EFFORT publisher cannot satisfy RELIABLE sub.
            if pub.reliability == 'best_effort' and sub.reliability == 'reliable':
                return 'mismatch'
            # Durability: VOLATILE publisher cannot satisfy TRANSIENT_LOCAL sub.
            if pub.durability == 'volatile' and sub.durability == 'transient_local':
                return 'mismatch'
    return 'ok'
