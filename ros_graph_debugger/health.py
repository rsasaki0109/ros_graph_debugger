"""One-line system health verdict derived from the current issues.

A developer (or an AI) glancing at the tool should get the bottom line first:
is the stack OK, degraded, or in trouble, and what is the single most important
thing to look at. This rolls the issue list up into that verdict. Pure function
over the snapshot dict, so the briefing, REST API, and UI all agree.
"""

from __future__ import annotations

_SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}


def summarize_health(d: dict) -> dict:
    """Return ``{verdict, counts, headline, issue_count}`` for a snapshot dict.

    ``verdict`` is ``critical`` if any critical issue exists, ``degraded`` if any
    warning, else ``ok``. ``headline`` is the title of the most severe issue
    (None when clean)."""
    issues = d.get('issues', []) or []
    counts = {'critical': 0, 'warning': 0, 'info': 0}
    for i in issues:
        sev = i.get('severity')
        if sev in counts:
            counts[sev] += 1

    if counts['critical']:
        verdict = 'critical'
    elif counts['warning']:
        verdict = 'degraded'
    else:
        verdict = 'ok'

    top = min(issues, key=lambda i: _SEVERITY_ORDER.get(i.get('severity'), 9),
              default=None) if issues else None
    return {'verdict': verdict, 'counts': counts,
            'headline': top['title'] if top else None,
            'issue_count': len(issues)}


def health_line(d: dict) -> str:
    """A compact Markdown one-liner, e.g.
    ``**System: CRITICAL** — 3 critical · top: Likely bottleneck: detector``."""
    s = summarize_health(d)
    c = s['counts']
    parts = [f'{c[k]} {k}' for k in ('critical', 'warning', 'info') if c[k]]
    tally = ', '.join(parts) if parts else 'no issues'
    line = f'**System: {s["verdict"].upper()}** — {tally}'
    if s['headline']:
        line += f' · top: {s["headline"]}'
    return line
