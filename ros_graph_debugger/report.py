"""Aggregate a recording into a shareable report (HTML) and an AI briefing
(Markdown).

The whole module is pure data-in / string-out, so it is fully testable without
ROS or a running agent: feed it a list of snapshot dicts and assert on the
summary or rendered text.
"""

from __future__ import annotations

import html
import re

from .measurement import RATE_DELIVERY_NOTE, RATE_SCOPE_LABELS, rate_measurement_scope
from .model import callback_source, callback_source_label
from .pipeline import pipeline_timeline, spark
from .recording import header_age_clock_note

_SEV_RANK = {'critical': 3, 'warning': 2, 'info': 1}
_STATUS_RANK = {'critical': 3, 'warning': 2, 'ok': 1, 'unknown': 0}
_BOTTLENECK_KINDS = {'bottleneck', 'rate_drop', 'topic_stale', 'qos_mismatch',
                     'slow_callback'}


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def build_report(header: dict, snapshots: list[dict],
                 path_focus: str | None = None) -> dict:
    """Summarize a recording. ``path_focus`` picks the node/topic whose
    pipeline path is followed over time (default: the most-blamed node)."""
    profile = header.get('profile') or None
    compiled = _compile_stages(profile)

    n = len(snapshots)
    timestamps = [s.get('timestamp', 0.0) for s in snapshots if s.get('timestamp')]
    duration = (max(timestamps) - min(timestamps)) if len(timestamps) >= 2 else 0.0

    topics: dict[str, dict] = {}
    issue_catalog: dict[tuple, dict] = {}
    tf_stale: dict[tuple, dict] = {}
    callbacks: dict[tuple, dict] = {}
    timeline: list[dict] = []

    # Per-stage readiness tallies.
    stage_tally: dict[str, dict] = {k: {'ok': 0, 'warning': 0, 'critical': 0,
                                        'unknown': 0} for k in compiled}

    for idx, snap in enumerate(snapshots):
        sev_counts = {'critical': 0, 'warning': 0, 'info': 0}
        stage_worst = {k: 'unknown' for k in compiled}

        for t in snap.get('topics', []):
            name = t['name']
            rec = topics.setdefault(name, {
                'type': t.get('type', ''), 'rate_min': None, 'rate_max': None,
                'rate_sum': 0.0, 'rate_n': 0, 'stale_count': 0,
                'max_bandwidth_bps': 0.0, 'max_p95_bytes': 0.0, 'seen': 0,
                'rate_measurement_scope_counts': {k: 0 for k in RATE_SCOPE_LABELS}})
            rec['seen'] += 1
            r = t.get('rate_hz')
            if isinstance(r, (int, float)):
                rec['rate_min'] = r if rec['rate_min'] is None else min(rec['rate_min'], r)
                rec['rate_max'] = r if rec['rate_max'] is None else max(rec['rate_max'], r)
                rec['rate_sum'] += r
                rec['rate_n'] += 1
                rec['rate_measurement_scope_counts'][rate_measurement_scope(t)] += 1
            bw = t.get('bandwidth_bps')
            if isinstance(bw, (int, float)):
                rec['max_bandwidth_bps'] = max(rec['max_bandwidth_bps'], bw)
            p95 = t.get('p95_msg_size_bytes')
            if isinstance(p95, (int, float)):
                rec['max_p95_bytes'] = max(rec['max_p95_bytes'], p95)
            if t.get('status') == 'critical':
                rec['stale_count'] += 1
            st = _stage_of(name, compiled)
            if st:
                _bump(stage_worst, st, t.get('status', 'unknown'))

        for issue in snap.get('issues', []):
            sev = issue.get('severity', 'info')
            if sev in sev_counts:
                sev_counts[sev] += 1
            key = (issue.get('kind', ''), issue.get('title', ''))
            cat = issue_catalog.setdefault(key, {
                'kind': issue.get('kind', ''), 'title': issue.get('title', ''),
                'count': 0, 'max_severity': 'info', 'first_index': idx,
                'last_index': idx, 'evidence': issue.get('evidence', []),
                'suggested_actions': issue.get('suggested_actions', []),
                'related_nodes': issue.get('related_nodes', []),
                'related_diagnostics': issue.get('related_diagnostics'),
                'related_topics': issue.get('related_topics', []),
                'related_frames': issue.get('related_frames', [])})
            cat['count'] += 1
            cat['last_index'] = idx
            if _SEV_RANK.get(sev, 0) > _SEV_RANK.get(cat['max_severity'], 0):
                cat['max_severity'] = sev
            # readiness bump from issues
            stsev = 'ok' if sev == 'info' else sev
            for tn in issue.get('related_topics', []):
                st = _stage_of(tn, compiled)
                if st:
                    _bump(stage_worst, st, stsev)

        for c in snap.get('callbacks', []):
            p95 = c.get('p95_ms')
            if not isinstance(p95, (int, float)):
                continue
            source = callback_source(c.get('source'))
            key = (c.get('node', ''), c.get('callback', ''), c.get('topic', ''), source)
            rec = callbacks.setdefault(key, {
                'node': c.get('node', ''), 'callback': c.get('callback', ''),
                'topic': c.get('topic', ''), 'source': source,
                'max_p95': 0.0, 'max_mean': 0.0,
                'seen': 0})
            rec['seen'] += 1
            rec['max_p95'] = max(rec['max_p95'], p95)
            mean = c.get('mean_ms')
            if isinstance(mean, (int, float)):
                rec['max_mean'] = max(rec['max_mean'], mean)

        for e in snap.get('tf_edges', []):
            if e.get('status') == 'critical':
                k = (e.get('parent', ''), e.get('child', ''))
                rec = tf_stale.setdefault(k, {'count': 0, 'max_age_ms': 0.0})
                rec['count'] += 1
                age = e.get('age_ms') or 0.0
                rec['max_age_ms'] = max(rec['max_age_ms'], age)

        for st, worst in stage_worst.items():
            stage_tally[st][worst] += 1

        worst_sev = ('critical' if sev_counts['critical'] else
                     'warning' if sev_counts['warning'] else
                     'info' if sev_counts['info'] else 'ok')
        timeline.append({'index': idx, 'counts': sev_counts, 'worst': worst_sev})

    # Finalize topic stats.
    topic_list = []
    for name, rec in topics.items():
        avg = (rec['rate_sum'] / rec['rate_n']) if rec['rate_n'] else None
        topic_list.append({
            'name': name, 'type': rec['type'],
            'rate_min': rec['rate_min'], 'rate_avg': avg, 'rate_max': rec['rate_max'],
            'stale_count': rec['stale_count'],
            'stale_pct': round(100.0 * rec['stale_count'] / n, 1) if n else 0.0,
            'max_bandwidth_bps': rec['max_bandwidth_bps'],
            'max_p95_bytes': rec['max_p95_bytes'],
            'rate_measurement_scope_counts': rec['rate_measurement_scope_counts'],
            'rate_unmeasured_count': rec['seen'] - rec['rate_n']})

    bottlenecks = sorted(
        ({**v, 'pct': round(100.0 * v['count'] / n, 1) if n else 0.0}
         for v in issue_catalog.values() if v['kind'] in _BOTTLENECK_KINDS),
        key=lambda x: (-_SEV_RANK.get(x['max_severity'], 0), -x['count']))

    issue_catalog_list = sorted(
        ({**v, 'pct': round(100.0 * v['count'] / n, 1) if n else 0.0}
         for v in issue_catalog.values()),
        key=lambda x: (-_SEV_RANK.get(x['max_severity'], 0), -x['count']))

    bandwidth_top = sorted(
        (t for t in topic_list if t['max_bandwidth_bps'] > 0),
        key=lambda x: -x['max_bandwidth_bps'])[:10]

    tf_list = sorted(
        ({'parent': k[0], 'child': k[1], **v} for k, v in tf_stale.items()),
        key=lambda x: -x['max_age_ms'])

    callback_list = sorted(callbacks.values(), key=lambda x: -x['max_p95'])[:10]

    # Recording-level health rollup: a verdict per sample (critical / degraded /
    # ok), aggregated so CI can gate on "the stack was degraded N% of the run".
    verdicts = ['critical' if t['worst'] == 'critical' else
                'degraded' if t['worst'] == 'warning' else 'ok'
                for t in timeline]
    crit_n = verdicts.count('critical')
    degr_n = verdicts.count('degraded')
    ok_n = verdicts.count('ok')
    health = {
        'worst': ('critical' if crit_n else 'degraded' if degr_n else 'ok'),
        'final': verdicts[-1] if verdicts else 'ok',
        'critical_pct': round(100.0 * crit_n / n, 1) if n else 0.0,
        'degraded_pct': round(100.0 * degr_n / n, 1) if n else 0.0,
        'ok_pct': round(100.0 * ok_n / n, 1) if n else 0.0,
    }

    readiness = None
    if compiled:
        readiness = []
        for stage in compiled:
            tally = stage_tally[stage]
            worst = ('critical' if tally['critical'] else
                     'warning' if tally['warning'] else
                     'ok' if tally['ok'] else 'unknown')
            readiness.append({
                'stage': stage, 'worst': worst,
                'ok_pct': round(100.0 * tally['ok'] / n, 1) if n else 0.0,
                'warn_pct': round(100.0 * tally['warning'] / n, 1) if n else 0.0,
                'error_pct': round(100.0 * tally['critical'] / n, 1) if n else 0.0})

    return {
        'meta': {
            'samples': n, 'duration_s': round(duration, 1),
            'profile': (profile or {}).get('name') if profile else None,
            'header_age_clock': header.get('header_age_clock')},
        'topics': sorted(topic_list, key=lambda x: x['name']),
        'health': health,
        'bottlenecks': bottlenecks,
        'issue_catalog': issue_catalog_list,
        'timeline': timeline,
        'tf_stale': tf_list,
        'callbacks': callback_list,
        'bandwidth_top': bandwidth_top,
        'readiness': readiness,
        'pipeline': pipeline_timeline(snapshots, path_focus),
    }


def _duration_sources(hop) -> str:
    values = hop.get('cb_p95_ms', [])
    sources = hop.get('cb_source', [None] * len(values))
    labels = {callback_source_label(source) for value, source in zip(values, sources)
              if isinstance(value, (int, float))}
    return ', '.join(sorted(labels)) or '—'


def _range(values) -> str:
    known = [v for v in values if isinstance(v, (int, float))]
    if not known:
        return '—'
    lo, hi = min(known), max(known)
    return f'{lo:.0f}' if round(lo) == round(hi) else f'{lo:.0f}–{hi:.0f}'


def _compile_stages(profile: dict | None) -> dict[str, list]:
    if not profile or not profile.get('groups'):
        return {}
    out = {}
    for k, g in profile['groups'].items():
        pats = []
        for p in g.get('topic_patterns', []):
            try:
                pats.append(re.compile(p))
            except re.error:
                pass
        out[k] = pats
    return out


def _stage_of(name: str, compiled: dict[str, list]) -> str | None:
    for k, pats in compiled.items():
        for re_ in pats:
            if re_.search(name):
                return k
    return None


def _bump(worst: dict, stage: str, status: str) -> None:
    if _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(worst[stage], 0):
        worst[stage] = status


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #
def _fmt_bw(v):
    if not v:
        return '—'
    if v >= 1e6:
        return f'{v / 1e6:.1f} MB/s'
    if v >= 1e3:
        return f'{v / 1e3:.1f} KB/s'
    return f'{v:.0f} B/s'


def _fmt_rate(v):
    return f'{v:.1f}' if isinstance(v, (int, float)) else '—'


def render_markdown(summary: dict) -> str:
    m = summary['meta']
    out = ['# ROS Graph Debugger — Recording Report']
    line = f'{m["samples"]} samples over {m["duration_s"]} s'
    if m['profile']:
        line += f'  ·  profile: {m["profile"]}'
    out += [line, '']

    h = summary.get('health')
    if h:
        out.append(f'**System: {h["worst"].upper()}** over the recording — '
                   f'critical {h["critical_pct"]}% · degraded {h["degraded_pct"]}% · '
                   f'ok {h["ok_pct"]}% (ended {h["final"].upper()})')
        out.append('')

    out += ['## Header age clock', '', header_age_clock_note(m), '']

    if summary['topics']:
        out += ['## Rate measurement sources', '', RATE_DELIVERY_NOTE, '',
                'Counts are snapshots in which the topic appears, not message counts. '
                'Unmeasured means no numeric rate; absent topics are not counted.', '',
                '| topic | probe receipts | subscriber statistics | bag recording | unknown | unmeasured |',
                '|---|---|---|---|---|---|']
        for t in sorted(summary['topics'], key=lambda t: t['name']):
            counts = t.get('rate_measurement_scope_counts', {})
            cells = [str(counts.get(k, '—')) for k in RATE_SCOPE_LABELS]
            cells.append(str(t.get('rate_unmeasured_count', '—')))
            out.append('| ' + t['name'] + ' | ' + ' | '.join(cells) + ' |')
        out.append('')

    if summary['readiness']:
        out.append('## Engage readiness (share of samples)')
        out.append('| stage | worst | ok% | warn% | error% |')
        out.append('|---|---|---|---|---|')
        for r in summary['readiness']:
            out.append(f'| {r["stage"]} | {r["worst"].upper()} | {r["ok_pct"]} | '
                       f'{r["warn_pct"]} | {r["error_pct"]} |')
        out.append('')

    out.append('## Top bottlenecks (by severity, then frequency)')
    if not summary['bottlenecks']:
        out.append('None observed.')
    else:
        for b in summary['bottlenecks'][:10]:
            out.append(f'- **[{b["max_severity"].upper()}]** {b["title"]} '
                       f'— seen in {b["pct"]}% of samples')
            if b['evidence']:
                out.append(f'  - e.g. {"; ".join(b["evidence"])}')
    out.append('')

    diagnostic_issues = [i for i in summary.get('issue_catalog', [])
                         if i['kind'] in ('diagnostics_error', 'diagnostics_warn')]
    if diagnostic_issues:
        out.append('## Diagnostic issues (representative samples)')
        for issue in diagnostic_issues:
            out.append(f'- **[{issue["max_severity"].upper()}]** {issue["title"]} '
                       f'— seen in {issue["pct"]}% of samples')
            refs = issue.get('related_diagnostics')
            out.append('  - Diagnostic tasks: ' + (', '.join(refs) if refs else 'unknown'))
            if issue['evidence']:
                out.append('  - e.g. ' + '; '.join(issue['evidence']))
        out.append('Diagnostic task and hardware labels do not establish ROS node '
                   'attribution. References and evidence are from a representative '
                   'sample; legacy missing references stay unknown.')
        out.append('')

    if summary['bandwidth_top']:
        out.append('## Highest bandwidth topics')
        out.append('| topic | max bandwidth | max p95 size |')
        out.append('|---|---|---|')
        for t in summary['bandwidth_top']:
            p95 = f'{t["max_p95_bytes"] / 1e6:.2f} MB' if t['max_p95_bytes'] else '—'
            out.append(f'| {t["name"]} | {_fmt_bw(t["max_bandwidth_bps"])} | {p95} |')
        out.append('')

    p = summary.get('pipeline')
    if p:
        out.append(f'## Pipeline path over time (focus: {p["target"]})')
        out.append(' → '.join(p['nodes']))
        out.append('')
        out.append('`!` marks samples where the focus was blamed; each sparkline '
                   'has its own scale.')
        out.append('')
        out.append('| hop | rate (Hz) | rate over time | consumer duration p95 (ms) '
                   '| duration p95 over time | sources |')
        out.append('|---|---|---|---|---|---|')
        width = 40
        out.append('| *blamed* | | `' + spark([1 if b else 0 for b in p['blamed']],
                                              width).replace('▁', '·')
                   .replace('█', '!') + '` | | | |')
        for h in p['hops']:
            out.append(f'| {h["topic"]} → {h["to"]} | {_range(h["rate_hz"])} | '
                       f'`{spark(h["rate_hz"], width)}` | {_range(h["cb_p95_ms"])} | '
                       f'`{spark(h["cb_p95_ms"], width)}` | {_duration_sources(h)} |')
        out.append('')

    if summary.get('callbacks'):
        out.append('## Slowest callbacks / processing durations (max p95 per source)')
        out.append('| node | callback | source | max p95 | max mean |')
        out.append('|---|---|---|---|---|')
        for c in summary['callbacks']:
            mean = f'{c["max_mean"]:.0f} ms' if c['max_mean'] else '—'
            out.append(f'| {c["node"]} | {c["callback"]} | '
                       f'{callback_source_label(c.get("source"))} | '
                       f'{c["max_p95"]:.0f} ms | {mean} |')
        out.append('')
        out.append('Application reports follow publisher semantics; only paired '
                   'callback traces measure callback start/end wall time. '
                   'Missing source stays unknown. Pipeline sources may change '
                   'over time; inspect the per-sample cb_source values.')
        out.append('')

    if summary['tf_stale']:
        out.append('## Stale transforms observed')
        for e in summary['tf_stale']:
            out.append(f'- {e["parent"]} → {e["child"]} — '
                       f'max age {e["max_age_ms"]:.0f} ms ({e["count"]} samples)')
        out.append('')

    out.append('## Topic rate summary (probed topics)')
    out.append('| topic | min | avg | max | stale% |')
    out.append('|---|---|---|---|---|')
    for t in summary['topics']:
        if t['rate_avg'] is None and t['stale_count'] == 0:
            continue
        out.append(f'| {t["name"]} | {_fmt_rate(t["rate_min"])} | '
                   f'{_fmt_rate(t["rate_avg"])} | {_fmt_rate(t["rate_max"])} | '
                   f'{t["stale_pct"]} |')
    return '\n'.join(out)


_COLORS = {'critical': '#f85149', 'warning': '#d29922', 'info': '#58a6ff',
           'ok': '#3fb950', 'unknown': '#6e7681'}


def _timeline_svg(timeline: list[dict]) -> str:
    if not timeline:
        return ''
    w_each = max(1, min(8, 900 // max(1, len(timeline))))
    width = w_each * len(timeline)
    height = 60
    max_count = max((sum(t['counts'].values()) for t in timeline), default=1) or 1
    bars = []
    for i, t in enumerate(timeline):
        total = sum(t['counts'].values())
        h = int((total / max_count) * (height - 6)) if max_count else 0
        color = _COLORS.get(t['worst'], '#6e7681')
        x = i * w_each
        bars.append(f'<rect x="{x}" y="{height - h}" width="{max(1, w_each - 1)}" '
                    f'height="{h}" fill="{color}"><title>sample {i}: {total} issues</title></rect>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'preserveAspectRatio="none">{"".join(bars)}</svg>')


def _series_svg(values: list, color: str, blamed: list, width=400, height=34) -> str:
    """One hop's series as a polyline (own scale; a flat series sits mid-height
    so it does not read as zero), blamed samples shaded. Scales to its cell."""
    n = len(values)
    if n < 2:
        return ''
    step = width / (n - 1)
    shade = ''.join(
        f'<rect x="{max(0.0, i * step - step / 2):.1f}" y="0" width="{step:.1f}" '
        f'height="{height}" fill="#f85149" opacity="0.12"/>'
        for i, b in enumerate(blamed) if b)
    known = [v for v in values if isinstance(v, (int, float))]
    if not known:
        return _svg_frame(width, height, shade)
    lo, hi = min(known), max(known)
    segs, cur = [], []
    for i, v in enumerate(values):
        if isinstance(v, (int, float)):
            y = (height / 2 if hi <= lo
                 else height - 3 - (v - lo) / (hi - lo) * (height - 6))
            cur.append(f'{i * step:.1f},{y:.1f}')
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    lines = ''.join(f'<polyline points="{" ".join(sg)}" fill="none" '
                    f'stroke="{color}" stroke-width="1.5" '
                    'vector-effect="non-scaling-stroke"/>' for sg in segs)
    return _svg_frame(width, height, shade + lines)


def _svg_frame(width, height, body: str) -> str:
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'preserveAspectRatio="none">{body}</svg>')


def render_html(summary: dict) -> str:
    m = summary['meta']
    e = html.escape

    def readiness_html():
        if not summary['readiness']:
            return ''
        cells = []
        for r in summary['readiness']:
            c = _COLORS.get(r['worst'], '#6e7681')
            cells.append(
                f'<div class="stage" style="border-bottom:3px solid {c}">'
                f'<div class="sname">{e(r["stage"])}</div>'
                f'<div class="sverdict" style="color:{c}">{r["worst"].upper()}</div>'
                f'<div class="spct">ok {r["ok_pct"]}% · err {r["error_pct"]}%</div></div>')
        return f'<div class="readiness">{"".join(cells)}</div>'

    def rate_sources_section():
        if not summary['topics']:
            return ''
        rows = []
        for t in sorted(summary['topics'], key=lambda t: t['name']):
            counts = t.get('rate_measurement_scope_counts', {})
            cells = [str(counts.get(k, '—')) for k in RATE_SCOPE_LABELS]
            cells.append(str(t.get('rate_unmeasured_count', '—')))
            rows.append('<tr><td>' + e(t['name']) + '</td>'
                        + ''.join('<td>' + e(c) + '</td>' for c in cells) + '</tr>')
        return ('<h2>Rate measurement sources</h2><p>' + e(RATE_DELIVERY_NOTE) + '</p>'
                '<p class="muted">Counts are snapshots in which the topic appears, '
                'not message counts. Unmeasured means no numeric rate; absent topics '
                'are not counted.</p><table><tr><th>topic</th><th>probe receipts</th>'
                '<th>subscriber statistics</th><th>bag recording</th><th>unknown</th>'
                '<th>unmeasured</th></tr>' + ''.join(rows) + '</table>')

    def bottleneck_rows():
        if not summary['bottlenecks']:
            return '<p class="muted">None observed. 🎉</p>'
        rows = []
        for b in summary['bottlenecks'][:15]:
            c = _COLORS.get(b['max_severity'], '#6e7681')
            ev = e('; '.join(b['evidence']))
            rows.append(
                f'<div class="issue" style="border-left-color:{c}">'
                f'<b style="color:{c}">[{b["max_severity"].upper()}]</b> {e(b["title"])}'
                f'<span class="pct">{b["pct"]}% of samples</span>'
                f'<div class="ev">{ev}</div></div>')
        return ''.join(rows)

    def diagnostic_section():
        issues = [i for i in summary.get('issue_catalog', [])
                  if i['kind'] in ('diagnostics_error', 'diagnostics_warn')]
        if not issues:
            return ''
        rows = ['<h2>Diagnostic issues (representative samples)</h2>']
        for issue in issues:
            refs = issue.get('related_diagnostics')
            tasks = ', '.join(refs) if refs else 'unknown'
            rows.append('<div class="issue"><b>' + e(issue['title']) + '</b>'
                        + f'<span class="pct">{issue["pct"]}% of samples</span>'
                        + '<div class="ev">Diagnostic tasks: ' + e(tasks) + '</div>'
                        + '<div class="ev">' + e('; '.join(issue['evidence'])) + '</div></div>')
        rows.append('<p class="muted">Diagnostic task and hardware labels do not '
                    'establish ROS node attribution. References and evidence are '
                    'from a representative sample; legacy missing references stay unknown.</p>')
        return ''.join(rows)

    def bandwidth_rows():
        rows = []
        for t in summary['bandwidth_top']:
            p95 = f'{t["max_p95_bytes"] / 1e6:.2f} MB' if t['max_p95_bytes'] else '—'
            rows.append(f'<tr><td>{e(t["name"])}</td><td>{_fmt_bw(t["max_bandwidth_bps"])}</td>'
                        f'<td>{p95}</td></tr>')
        return ''.join(rows) or '<tr><td colspan="3" class="muted">no probed bandwidth</td></tr>'

    def health_banner():
        h = summary.get('health')
        if not h:
            return ''
        c = {'critical': '#f85149', 'degraded': '#d29922', 'ok': '#3fb950'}.get(
            h['worst'], '#6e7681')
        return (f'<div class="health" style="border-left:4px solid {c}">'
                f'<b style="color:{c}">System: {h["worst"].upper()}</b> '
                f'<span class="muted">over the recording — critical {h["critical_pct"]}% '
                f'· degraded {h["degraded_pct"]}% · ok {h["ok_pct"]}% '
                f'(ended {e(h["final"].upper())})</span></div>')

    def pipeline_section():
        p = summary.get('pipeline')
        if not p:
            return ''
        rows = ''.join(
            f'<tr><td>{e(h["topic"])} → {e(h["to"])}</td>'
            f'<td>{_range(h["rate_hz"])} Hz<br>'
            f'{_series_svg(h["rate_hz"], "#58a6ff", p["blamed"])}</td>'
            f'<td>{_range(h["cb_p95_ms"])} ms<br>'
            f'{_series_svg(h["cb_p95_ms"], "#d29922", p["blamed"])}</td>'
            f'<td>{e(_duration_sources(h))}</td></tr>'
            for h in p['hops'])
        return (f'<h2>Pipeline path over time <span class="muted">— focus: '
                f'{e(p["target"])}</span></h2>'
                f'<p class="muted">{" → ".join(e(n) for n in p["nodes"])}. '
                'Red bands: samples where the focus was blamed; each series has '
                'its own scale.</p>'
                '<table><tr><th>hop</th><th>rate</th><th>consumer duration p95</th><th>sources</th>'
                f'</tr>{rows}</table>')

    def callback_section():
        if not summary.get('callbacks'):
            return ''
        rows = ''.join(
            f'<tr><td>{e(c["node"])}</td><td>{e(c["callback"])}</td>'
            f'<td>{e(callback_source_label(c.get("source")))}</td>'
            f'<td>{c["max_p95"]:.0f} ms</td>'
            f'<td>{(str(round(c["max_mean"])) + " ms") if c["max_mean"] else "—"}</td></tr>'
            for c in summary['callbacks'])
        return ('<h2>Slowest callbacks / processing durations (per source)</h2>'
                '<p>Application reports follow publisher semantics; only paired '
                'callback traces measure callback start/end wall time. '
                'Missing source stays unknown.</p>'
                '<table><tr><th>node</th><th>callback</th><th>source</th>'
                f'<th>max p95</th><th>max mean</th></tr>{rows}</table>')

    def tf_rows():
        if not summary['tf_stale']:
            return ''
        rows = ''.join(
            f'<tr><td>{e(x["parent"])} → {e(x["child"])}</td>'
            f'<td>{x["max_age_ms"]:.0f} ms</td><td>{x["count"]}</td></tr>'
            for x in summary['tf_stale'])
        return ('<h2>Stale transforms</h2><table><tr><th>transform</th>'
                f'<th>max age</th><th>samples</th></tr>{rows}</table>')

    def topic_rows():
        rows = []
        for t in summary['topics']:
            if t['rate_avg'] is None and t['stale_count'] == 0:
                continue
            rows.append(
                f'<tr><td>{e(t["name"])}</td><td>{_fmt_rate(t["rate_min"])}</td>'
                f'<td>{_fmt_rate(t["rate_avg"])}</td><td>{_fmt_rate(t["rate_max"])}</td>'
                f'<td>{t["stale_pct"]}%</td></tr>')
        return ''.join(rows) or '<tr><td colspan="5" class="muted">no probed topics</td></tr>'

    profile_line = f' · profile: {e(m["profile"])}' if m['profile'] else ''
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>ROS Graph Debugger — Report</title>
<style>
body{{background:#0d1117;color:#c9d1d9;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px;max-width:1000px;margin:0 auto}}
h1{{font-size:20px}}h2{{font-size:15px;border-bottom:1px solid #2d333b;padding-bottom:4px;margin-top:28px}}
.muted{{color:#8b949e}}
.health{{background:#161b22;border:1px solid #2d333b;border-radius:6px;padding:8px 12px;margin:12px 0;font-size:14px}}
.readiness{{display:flex;gap:1px;background:#2d333b;border-radius:6px;overflow:hidden;margin:12px 0}}
.stage{{flex:1;background:#161b22;padding:8px;text-align:center}}
.sname{{text-transform:capitalize}}.sverdict{{font-weight:700;font-size:13px}}.spct{{color:#8b949e;font-size:11px}}
.tl{{background:#161b22;border:1px solid #2d333b;border-radius:6px;padding:8px;margin:12px 0}}
.issue{{background:#161b22;border:1px solid #2d333b;border-left-width:3px;border-radius:6px;padding:8px 11px;margin:8px 0}}
.issue .pct{{float:right;color:#8b949e;font-size:12px}}.issue .ev{{color:#8b949e;font-size:12px;margin-top:3px}}
table{{width:100%;border-collapse:collapse;margin:10px 0}}
td,th{{text-align:left;padding:5px 8px;border-bottom:1px solid #21262d;font-size:13px}}
th{{color:#8b949e;font-weight:600}}
</style></head><body>
<h1>◇ ROS Graph Debugger — Recording Report</h1>
<p class="muted">{m['samples']} samples over {m['duration_s']} s{profile_line}</p>
{health_banner()}
<h2>Header age clock</h2>
<p>{e(header_age_clock_note(m))}</p>
{readiness_html()}
{rate_sources_section()}
<h2>Issue timeline</h2>
<div class="tl">{_timeline_svg(summary['timeline'])}</div>
<h2>Top bottlenecks</h2>
{bottleneck_rows()}
{diagnostic_section()}
<h2>Highest bandwidth topics</h2>
<table><tr><th>topic</th><th>max bandwidth</th><th>max p95 size</th></tr>{bandwidth_rows()}</table>
{pipeline_section()}
{callback_section()}
{tf_rows()}
<h2>Topic rate summary</h2>
<table><tr><th>topic</th><th>min</th><th>avg</th><th>max</th><th>stale%</th></tr>{topic_rows()}</table>
</body></html>"""
