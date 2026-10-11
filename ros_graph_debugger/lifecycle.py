"""Managed (lifecycle) node states.

A lifecycle node only processes and publishes data while it is ``active`` — so
in Nav2 and other managed stacks, "nothing comes out of /cmd_vel" is very often
a node stuck in ``inactive`` or ``unconfigured``, invisible in a plain graph view
because its publishers and subscriptions exist either way.

Detection is passive (a node that serves ``<node>/get_state`` is a lifecycle
node — that comes from the service list the graph poll already collects); the
state itself is read with the standard ``lifecycle_msgs/srv/GetState`` query in
`node.py`. This module holds the pure, ROS-free part.
"""

from __future__ import annotations

ACTIVE = 'active'
# Primary states in which the node does not process data.
NOT_ACTIVE = ('unconfigured', 'inactive', 'finalized')
# Transition the node lands in when a transition callback raised.
ERROR = 'errorprocessing'
UNKNOWN = 'unknown'  # a lifecycle node that has not answered get_state yet


def is_lifecycle_node(node) -> bool:
    """True when the node serves the standard ``<node>/get_state`` service."""
    services = getattr(node, 'services', None) or []
    return f'{node.id}/get_state' in services


def suggested_transition(node_id: str, state: str) -> str | None:
    """The `ros2 lifecycle set` command that moves ``state`` toward active."""
    step = {'unconfigured': 'configure', 'inactive': 'activate'}.get(state)
    return f'ros2 lifecycle set {node_id} {step}' if step else None


def waiting_outputs(node, topics: dict) -> list[str]:
    """Topics this node publishes that somebody else subscribes to — the data
    those subscribers are not getting while the node is not active."""
    out = []
    for name in getattr(node, 'publishers', []) or []:
        t = topics.get(name)
        if t is not None and any(s != node.id for s in t.subscribers):
            out.append(name)
    return sorted(out)
