"""Compare two recordings and surface what regressed.

Answers the question a ROS developer actually asks after a change: *did this
make the pipeline worse?* It diffs two `build_report` summaries — topic rates,
issues, callback durations, and the overall health verdict — into a structured
regression report and a Markdown briefing, so it can gate CI or back an
AI-assisted before/after review.

Pure data-in / dict-out, like `report.py`, so it's fully testable.
"""

from __future__ import annotations

_HEALTH_RANK = {'ok': 0, 'degraded': 1, 'critical': 2}


def diff_reports(base: dict, cur: dict, *, rate_tol: float = 0.2,
                 cb_tol: float = 0.2) -> dict:
    """Diff two `build_report` summaries. ``rate_tol`` / ``cb_tol`` are the
    fractional changes (default 20%) that count as a regression/improvement."""
    bt = {t['name']: t for t in base.get('topics', [])}
    ct = {t['name']: t for t in cur.get('topics', [])}
    topic_regressions, topic_improvements = [], []
    for name in sorted(set(bt) | set(ct)):
        br = (bt.get(name) or {}).get('rate_avg')
        cr = (ct.get(name) or {}).get('rate_avg')
        if not (isinstance(br, (int, float)) and isinstance(cr, (int, float)) and br > 0):
            continue
        pct = round(100.0 * (cr - br) / br, 1)
        row = {'name': name, 'base': round(br, 1), 'cur': round(cr, 1), 'pct': pct}
        if cr < br * (1 - rate_tol):
            topic_regressions.append(row)
        elif cr > br * (1 + rate_tol):
            topic_improvements.append(row)

    b_issues = {(i['kind'], i['title']) for i in base.get('issue_catalog', [])}
    c_issues = {(i['kind'], i['title']) for i in cur.get('issue_catalog', [])}
    new_issues = [{'kind': k, 'title': t} for k, t in sorted(c_issues - b_issues)]
    resolved_issues = [{'kind': k, 'title': t} for k, t in sorted(b_issues - c_issues)]

    bc = {(c['node'], c['callback']): c for c in base.get('callbacks', [])}
    cc = {(c['node'], c['callback']): c for c in cur.get('callbacks', [])}
    callback_regressions = []
    for k in sorted(set(bc) & set(cc)):
        bp, cp = bc[k]['max_p95'], cc[k]['max_p95']
        if bp > 0 and cp > bp * (1 + cb_tol):
            callback_regressions.append({'node': k[0], 'callback': k[1],
                                         'base': round(bp, 1), 'cur': round(cp, 1)})

    bh, ch = base.get('health', {}), cur.get('health', {})
    health = {'base': bh.get('worst', 'ok'), 'cur': ch.get('worst', 'ok'),
              'base_critical_pct': bh.get('critical_pct', 0.0),
              'cur_critical_pct': ch.get('critical_pct', 0.0)}
    health_dir = (_HEALTH_RANK.get(health['cur'], 0)
                  - _HEALTH_RANK.get(health['base'], 0))

    regressed = bool(topic_regressions or new_issues or callback_regressions
                     or health_dir > 0)
    improved = (not regressed and bool(topic_improvements or resolved_issues
                                       or health_dir < 0))
    verdict = 'regressed' if regressed else 'improved' if improved else 'stable'

    return {
        'verdict': verdict, 'health': health,
        'topic_regressions': topic_regressions,
        'topic_improvements': topic_improvements,
        'new_issues': new_issues, 'resolved_issues': resolved_issues,
        'callback_regressions': callback_regressions,
    }


def render_diff_markdown(d: dict) -> str:
    out = ['# ROS Graph Debugger — Regression Diff']
    h = d['health']
    verdict_line = f'**Verdict: {d["verdict"].upper()}**'
    if h['base'] != h['cur']:
        verdict_line += f' — system health {h["base"]} → {h["cur"]}'
    out += [verdict_line, '']

    regs = []
    for t in d['topic_regressions']:
        regs.append(f'- Rate drop: `{t["name"]}` {t["base"]} → {t["cur"]} Hz ({t["pct"]}%)')
    for c in d['callback_regressions']:
        regs.append(f'- Slower callback: `{c["node"]}` {c["callback"]} '
                    f'{c["base"]} → {c["cur"]} ms')
    for i in d['new_issues']:
        regs.append(f'- New issue: [{i["kind"]}] {i["title"]}')
    out.append('## Regressions')
    out += regs or ['None. 🎉']
    out.append('')

    imps = []
    for t in d['topic_improvements']:
        imps.append(f'- Rate up: `{t["name"]}` {t["base"]} → {t["cur"]} Hz (+{t["pct"]}%)')
    for i in d['resolved_issues']:
        imps.append(f'- Resolved: [{i["kind"]}] {i["title"]}')
    if imps:
        out.append('## Improvements')
        out += imps
        out.append('')

    return '\n'.join(out)
