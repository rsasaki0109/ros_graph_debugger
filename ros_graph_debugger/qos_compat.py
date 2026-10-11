"""QoS request/offer compatibility — which pub/sub pairs DDS refuses to match.

A subscription *requests* QoS and a publisher *offers* it; for the policies
below DDS only matches the pair when the offer is at least as strong as the
request. An incompatible pair shows up connected in the graph, yet no data
ever flows — what rmw reports as a requested/offered-incompatible-QoS event,
here detected statically from the endpoint info:

- reliability: a best-effort offer cannot serve a reliable request;
- durability:  a volatile offer cannot serve a transient-local request;
- deadline:    the offered period must be <= the requested one;
- liveliness:  an automatic offer cannot serve a manual-by-topic request;
- lease:       the offered lease duration must be <= the requested one.

Durations are milliseconds, ``None`` meaning infinite / unset. Pure, so it is
tested without ROS; `qos_utils` converts rclpy profiles into this shape.
"""

from __future__ import annotations

_LIVELINESS_RANK = {'automatic': 0, 'manual_by_topic': 1}


def _longer(offered: float | None, requested: float | None) -> bool:
    """True when the offered period is weaker (longer) than requested."""
    if requested is None:
        return False
    return offered is None or offered > requested


def _fmt(ms: float | None) -> str:
    return 'infinite' if ms is None else f'{ms:g} ms'


def incompatibilities(pub, sub) -> list[str]:
    """Why ``pub`` (offer) cannot serve ``sub`` (request); empty if it can.
    Takes ``QoSInfo``-like objects."""
    out = []
    if pub.reliability == 'best_effort' and sub.reliability == 'reliable':
        out.append('reliability: publisher best_effort, subscriber requests reliable')
    if pub.durability == 'volatile' and sub.durability == 'transient_local':
        out.append('durability: publisher volatile, subscriber requests transient_local')
    p_dl, s_dl = getattr(pub, 'deadline_ms', None), getattr(sub, 'deadline_ms', None)
    if _longer(p_dl, s_dl):
        out.append(f'deadline: publisher offers {_fmt(p_dl)}, subscriber '
                   f'requests {_fmt(s_dl)}')
    p_lk, s_lk = (_LIVELINESS_RANK.get(getattr(e, 'liveliness', ''))
                  for e in (pub, sub))
    if p_lk is not None and s_lk is not None and p_lk < s_lk:
        out.append(f'liveliness: publisher {pub.liveliness}, subscriber requests '
                   f'{sub.liveliness}')
    p_ls = getattr(pub, 'liveliness_lease_ms', None)
    s_ls = getattr(sub, 'liveliness_lease_ms', None)
    if _longer(p_ls, s_ls):
        out.append(f'liveliness lease: publisher offers {_fmt(p_ls)}, subscriber '
                   f'requests {_fmt(s_ls)}')
    return out


def topic_incompatibilities(endpoints) -> list[str]:
    """Every incompatible pub/sub pair on a topic, as evidence lines."""
    pubs = [e for e in endpoints if e.endpoint_type == 'publisher']
    subs = [e for e in endpoints if e.endpoint_type == 'subscriber']
    return [f'{p.node} → {s.node}: {why}'
            for p in pubs for s in subs for why in incompatibilities(p, s)]


def detect_qos_status(endpoints) -> str:
    """'ok' | 'mismatch' | 'unknown' for a topic's endpoints."""
    if not any(e.endpoint_type == 'publisher' for e in endpoints) or \
            not any(e.endpoint_type == 'subscriber' for e in endpoints):
        return 'unknown'
    return 'mismatch' if topic_incompatibilities(endpoints) else 'ok'
