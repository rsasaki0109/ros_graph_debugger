"""Services and actions: who serves what, and which clients have no server.

Topics get all the attention in a graph view, but a Nav2-style stack also
hangs on request/response: `bt_navigator` calls `follow_path` on the
controller, `navigate_to_pose` is an action, and a missing server leaves the
client waiting forever with nothing on any topic to show it.

An action ``X`` is recognised by its ``X/_action/send_goal`` service (servers
serve it, clients are clients of it). Parameter services every node carries are
left out. Pure functions over ``NodeInfo``-like objects, so testable without
ROS; the live lists come from `graph_build`.
"""

from __future__ import annotations

ACTION_GOAL = '/_action/send_goal'
_ACTION_MARK = '/_action/'

# Services every rclcpp/rclpy node offers; clients of these are housekeeping.
_INFRA_SUFFIXES = (
    '/describe_parameters', '/get_parameters', '/get_parameter_types',
    '/list_parameters', '/set_parameters', '/set_parameters_atomically',
    '/get_type_description',
)


def _classify(name: str) -> tuple[str, str] | None:
    """(kind, name) for a service name: an action, a plain service, or None
    for housekeeping / action-internal services."""
    if name.endswith(ACTION_GOAL):
        return 'action', name[:-len(ACTION_GOAL)]
    if _ACTION_MARK in name or name.endswith(_INFRA_SUFFIXES):
        return None
    return 'service', name


def service_graph(nodes) -> dict:
    """``{services: [...], actions: [...]}``, each entry
    ``{name, servers, clients}`` with sorted node ids."""
    table: dict[tuple[str, str], dict] = {}
    nodes = nodes.values() if isinstance(nodes, dict) else nodes
    for n in nodes:
        nid = n['id'] if isinstance(n, dict) else n.id
        get = (lambda k: n.get(k) or []) if isinstance(n, dict) \
            else (lambda k: getattr(n, k, None) or [])
        for role, names in (('servers', get('services')),
                            ('clients', get('service_clients'))):
            for raw in names:
                key = _classify(raw)
                if key is None:
                    continue
                entry = table.setdefault(key, {'name': key[1], 'servers': set(),
                                               'clients': set()})
                entry[role].add(nid)
    out: dict[str, list] = {'services': [], 'actions': []}
    for (kind, _), e in sorted(table.items()):
        out[kind + 's'].append({'name': e['name'],
                                'servers': sorted(e['servers']),
                                'clients': sorted(e['clients'])})
    return out


def missing_servers(graph: dict, known_services=()) -> list[dict]:
    """Entries that have clients but no server among the known nodes — unless
    the service name is advertised anyway (``known_services``, the global
    service list: its server may just be a node we could not list)."""
    known = set(known_services)
    out = []
    for kind in ('service', 'action'):
        for e in graph.get(kind + 's', []):
            if e['servers'] or not e['clients']:
                continue
            advertised = e['name'] + ACTION_GOAL if kind == 'action' else e['name']
            if advertised in known:
                continue
            out.append({'kind': kind, **e})
    return out
