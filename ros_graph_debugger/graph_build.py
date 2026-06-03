"""Build the runtime node/topic graph from a live rclpy node.

Nodes are reconstructed primarily from *topic endpoint info* rather than from
``get_node_names_and_namespaces()``. Endpoint discovery rides on topic
discovery, which propagates more reliably than the node-name graph (Autoware
and Nav2 routinely show topics whose nodes momentarily fail to list). Endpoints
also carry per-endpoint QoS, which is exactly what we need for mismatch checks.

This function is pure with respect to the passed rclpy node, so it can be
exercised in-process against real publishers/subscribers in a unit test.
"""

from __future__ import annotations

from .model import NodeInfo, TopicInfo
from .qos_utils import detect_qos_status, endpoint_to_qos_info

SKIP_NODE_NAMES = {'ros_graph_debugger', '_ros2cli_daemon'}
# rmw placeholders for endpoints whose participant hasn't fully matched yet.
_UNKNOWN_MARKERS = ('_NODE_NAME_UNKNOWN_', '_NODE_NAMESPACE_UNKNOWN_')


def fq_node(name: str, namespace: str) -> str:
    if not name or name in _UNKNOWN_MARKERS:
        return ''
    if namespace in ('', '/'):
        return '/' + name
    return namespace.rstrip('/') + '/' + name


def build_graph(ros_node) -> tuple[dict[str, NodeInfo], dict[str, TopicInfo]]:
    nodes: dict[str, NodeInfo] = {}
    topics: dict[str, TopicInfo] = {}

    def ensure_node(fq: str, name: str, ns: str) -> NodeInfo | None:
        if not fq or name in SKIP_NODE_NAMES:
            return None
        n = nodes.get(fq)
        if n is None:
            n = NodeInfo(id=fq, name=name, namespace=ns or '/')
            nodes[fq] = n
        return n

    # Seed from the node-name API when it happens to work (gives us nodes with
    # no pub/sub yet, plus services).
    try:
        for name, ns in ros_node.get_node_names_and_namespaces():
            ensure_node(fq_node(name, ns), name, ns)
    except Exception:
        pass

    for tname, ttypes in ros_node.get_topic_names_and_types():
        ttype = ttypes[0] if ttypes else ''
        t = TopicInfo(name=tname, type=ttype)
        endpoints = []

        for getter, role, bucket in (
                (ros_node.get_publishers_info_by_topic, 'publisher', t.publishers),
                (ros_node.get_subscriptions_info_by_topic, 'subscriber', t.subscribers)):
            try:
                infos = getter(tname)
            except Exception:
                infos = []
            for ep in infos:
                fq = fq_node(ep.node_name, ep.node_namespace)
                n = ensure_node(fq, ep.node_name, ep.node_namespace)
                if n is None:
                    continue
                bucket.append(fq)
                endpoints.append(endpoint_to_qos_info(fq, role, ep.qos_profile))
                if role == 'publisher':
                    if tname not in n.publishers:
                        n.publishers.append(tname)
                else:
                    if tname not in n.subscribers:
                        n.subscribers.append(tname)

        t.publisher_count = len(t.publishers)
        t.subscriber_count = len(t.subscribers)
        t.qos_endpoints = endpoints
        t.qos_status = detect_qos_status(endpoints)
        topics[tname] = t

    # Best-effort services for known nodes (cosmetic; skip on failure).
    for fq, n in nodes.items():
        try:
            n.services = [s for s, _ in
                          ros_node.get_service_names_and_types_by_node(
                              n.name, n.namespace)]
        except Exception:
            pass

    return nodes, topics
