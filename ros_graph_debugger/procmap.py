"""Map ROS node ids to OS processes from their command lines.

Node→process attribution in ROS 2 is genuinely hard: anonymous nodes, console
entry points, and **component containers** (many nodes in one process) all blur
the line. Rather than pretend, we attach a *confidence* to every match:

- ``high``   — the process declares this node via a ``__node:=`` remap.
- ``medium`` — the executable / script basename equals the node's base name
  (the ``ros2 run pkg <exe>`` case).
- ``low``    — the node's base name appears as a bare token in the command line,
  or several nodes share one pid (a composition container, where per-node CPU
  cannot be separated).

Kept free of rclpy/psutil so the matching logic is unit-tested with synthetic
process dicts; the live agent feeds it real ``/proc`` data via psutil.
"""

from __future__ import annotations

import os
import re

from .graph_build import fq_node

_NODE_RE = re.compile(r'__node:=(\S+)')
_NS_RE = re.compile(r'__ns:=(\S+)')

_CONF_RANK = {'none': 0, 'low': 1, 'medium': 2, 'high': 3}
_RANK_CONF = {v: k for k, v in _CONF_RANK.items()}


def _exe_basename(cmdline: list[str]) -> str:
    """The executable/script name driving a process, minus a ``.py`` suffix.
    Skips a leading ``pythonX`` interpreter to reach the actual script."""
    args = [a for a in cmdline if a and not a.startswith('-')]
    if not args:
        return ''
    first = os.path.basename(args[0])
    if first.startswith('python') and len(args) > 1:
        first = os.path.basename(args[1])
    if first.endswith('.py'):
        first = first[:-3]
    return first


def _proc_view(p: dict) -> dict:
    cmd = p.get('cmdline') or []
    joined = ' '.join(cmd)
    nsm = _NS_RE.search(joined)
    ns = nsm.group(1) if nsm else '/'
    fqs = {fq_node(n, ns) for n in _NODE_RE.findall(joined)}
    return {'pid': p['pid'], 'fqs': fqs, 'exe': _exe_basename(cmd),
            'tokens': set(cmd)}


def match_nodes_to_processes(node_ids, processes) -> dict[str, dict]:
    """Return ``{node_id: {'pid', 'confidence'}}`` for the nodes we can place.

    ``processes`` is an iterable of dicts with ``pid`` and ``cmdline`` (a list of
    argv strings). Highest-confidence evidence wins per node; a pid shared by
    several matched nodes is a container, so all its matches are capped at
    ``low`` (we can't split a shared process's CPU per node)."""
    views = [_proc_view(p) for p in processes]
    chosen: dict[str, tuple] = {}  # node_id -> (pid, rank)

    def consider(node_id, pid, conf):
        rank = _CONF_RANK[conf]
        cur = chosen.get(node_id)
        if cur is None or rank > cur[1]:
            chosen[node_id] = (pid, rank)

    for nid in node_ids:
        base = nid.rsplit('/', 1)[-1]
        for v in views:
            if nid in v['fqs']:
                consider(nid, v['pid'], 'high')
            elif v['exe'] and v['exe'] == base:
                consider(nid, v['pid'], 'medium')
            elif base and base in v['tokens']:
                consider(nid, v['pid'], 'low')

    # Cap container-shared pids: more than one node on a pid => low confidence.
    share: dict[int, int] = {}
    for pid, _ in chosen.values():
        share[pid] = share.get(pid, 0) + 1

    out: dict[str, dict] = {}
    for nid, (pid, rank) in chosen.items():
        if share[pid] > 1:
            rank = min(rank, _CONF_RANK['low'])
        out[nid] = {'pid': pid, 'confidence': _RANK_CONF[rank]}
    return out
