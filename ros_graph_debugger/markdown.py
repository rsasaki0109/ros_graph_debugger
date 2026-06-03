"""Render a snapshot as compact Markdown for LLM consumption.

The goal is an AI-friendly briefing a developer can paste into a chat (or an
agent can fetch via /api/v1/snapshot.md): issues first with evidence, then a
terse graph/metrics table. Kept small on purpose so it fits a prompt."""

from __future__ import annotations


def _fmt_rate(v):
    return f'{v:.1f} Hz' if isinstance(v, (int, float)) else '—'


def _fmt_bw(v):
    if not isinstance(v, (int, float)):
        return '—'
    if v >= 1e6:
        return f'{v/1e6:.1f} MB/s'
    if v >= 1e3:
        return f'{v/1e3:.1f} KB/s'
    return f'{v:.0f} B/s'


def snapshot_to_markdown(snap) -> str:
    # Accept a GraphSnapshot, a replay _Frame, or a plain dict.
    d = snap.to_dict() if hasattr(snap, 'to_dict') else snap
    lines: list[str] = []
    lines.append('# ROS Graph Debugger — Runtime Snapshot')
    if d.get('profile'):
        lines.append(f'Profile: **{d["profile"]}**')
    lines.append(f'Nodes: {len(d["nodes"])}  ·  Topics: {len(d["topics"])}  ·  '
                 f'Issues: {len(d["issues"])}')
    lines.append('')

    # --- Issues first: this is what an AI should act on. ---
    issues = d['issues']
    lines.append('## Issues (what to look at next)')
    if not issues:
        lines.append('No issues detected.')
    else:
        for i in issues:
            lines.append(f'### [{i["severity"].upper()}] {i["title"]}')
            if i.get('explanation'):
                lines.append(i['explanation'])
            if i.get('evidence'):
                lines.append('- Evidence: ' + '; '.join(i['evidence']))
            if i.get('suggested_actions'):
                lines.append('- Suggested: ' + '; '.join(i['suggested_actions']))
            related = i.get('related_nodes', []) + i.get('related_topics', []) \
                + i.get('related_frames', [])
            if related:
                lines.append('- Related: ' + ', '.join(related))
            lines.append('')

    # --- Topic table. ---
    lines.append('## Topics')
    lines.append('| topic | type | pub | sub | rate | age p95 | bandwidth | qos | status |')
    lines.append('|---|---|---|---|---|---|---|---|---|')
    for t in sorted(d['topics'], key=lambda x: x['name']):
        age = t.get('header_age_p95_ms')
        lines.append('| {name} | {type} | {p} | {s} | {rate} | {age} | {bw} | {qos} | '
                     '{status} |'.format(
                         name=t['name'],
                         type=t['type'].split('/')[-1] if t['type'] else '—',
                         p=t['publisher_count'], s=t['subscriber_count'],
                         rate=_fmt_rate(t.get('rate_hz')),
                         age=f'{age:.0f} ms' if isinstance(age, (int, float)) else '—',
                         bw=_fmt_bw(t.get('bandwidth_bps')),
                         qos=t.get('qos_status', 'unknown'),
                         status=t.get('status', 'unknown')))
    lines.append('')

    # --- Nodes with process metrics, when known. ---
    metric_nodes = [n for n in d['nodes'] if n.get('cpu_percent') is not None]
    if metric_nodes:
        lines.append('## Nodes (process metrics)')
        lines.append('| node | cpu | rss | confidence |')
        lines.append('|---|---|---|---|')
        for n in sorted(metric_nodes, key=lambda x: -(x.get('cpu_percent') or 0)):
            rss = n.get('rss_bytes')
            rss_s = f'{rss/1e6:.0f} MB' if rss else '—'
            lines.append(f'| {n["id"]} | {n["cpu_percent"]:.0f}% | {rss_s} | '
                         f'{n["process_mapping_confidence"]} |')
        lines.append('')

    # --- TF staleness, if any. ---
    stale_tf = [e for e in d['tf_edges'] if e.get('status') != 'ok']
    if stale_tf:
        lines.append('## TF (stale transforms)')
        for e in stale_tf:
            lines.append(f'- {e["parent"]} -> {e["child"]}: '
                         f'{e.get("age_ms", "?")} ms')
        lines.append('')

    return '\n'.join(lines)
