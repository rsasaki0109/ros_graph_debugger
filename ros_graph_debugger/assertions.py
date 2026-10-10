"""Health assertions over a window of snapshots — a CI gate without a baseline.

`rgd diff` answers "did this change make things worse?" but needs a known-good
recording to compare against. This answers the simpler question a launch test or
a sim CI job asks: *over these N seconds, did the system meet its contract?* —
no critical issues, these topics at or above these rates, these nodes up, none
of these issue kinds.

Every check is evaluated per snapshot. A sample that violates it is a *breach*,
and a check passes when its breach fraction is at most ``max_breach`` (default
0: never). Pure data-in / dict-out, like `diff.py`, so it is fully testable and
the CLI only has to collect snapshots.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from .health import summarize_health

_VERDICT_RANK = {'ok': 0, 'degraded': 1, 'critical': 2}
FAIL_ON_CHOICES = ('critical', 'degraded', 'never')


def _fmt_hz(v) -> str:
    return 'n/a' if v is None else f'{v:.1f} Hz'


def _health_check(fail_on: str, ignore_kinds: set):
    threshold = _VERDICT_RANK[fail_on]

    def check(snap: dict):
        issues = [i for i in snap.get('issues', []) or []
                  if i.get('kind') not in ignore_kinds]
        h = summarize_health({'issues': issues})
        if _VERDICT_RANK[h['verdict']] >= threshold:
            return False, f'{h["verdict"]}: {h["headline"]}'
        return True, None

    return check


def _rate_check(topic: str, min_hz: float):
    def check(snap: dict):
        t = next((t for t in snap.get('topics', []) or []
                  if t.get('name') == topic), None)
        if t is None:
            return False, 'topic not in graph'
        rate = t.get('rate_hz')
        if rate is None:
            return False, 'rate not measured (is the topic probed?)'
        if rate < min_hz:
            return False, f'{_fmt_hz(rate)} < {min_hz:g} Hz'
        return True, None

    return check


def _forbid_check(kind: str):
    def check(snap: dict):
        hit = next((i for i in snap.get('issues', []) or []
                    if i.get('kind') == kind), None)
        if hit:
            return False, hit.get('title', kind)
        return True, None

    return check


def _presence_check(field: str, key: str, name: str):
    def check(snap: dict):
        if any(x.get(key) == name for x in snap.get(field, []) or []):
            return True, None
        return False, f'{name} missing'

    return check


def _settled(snapshots: list[dict], settle: float) -> list[dict]:
    """Drop samples from the first ``settle`` seconds (startup warm-up: rates
    are not measured yet and nodes are still coming up)."""
    if settle <= 0 or not snapshots:
        return list(snapshots)
    t0 = snapshots[0].get('timestamp')
    if not isinstance(t0, (int, float)):
        return list(snapshots)
    return [s for s in snapshots
            if not isinstance(s.get('timestamp'), (int, float))
            or s['timestamp'] - t0 >= settle]


def evaluate(snapshots: list[dict], *, fail_on: str = 'critical',
             expect_rates: dict[str, float] | None = None,
             forbid_kinds=(), ignore_kinds=(), require_nodes=(),
             require_topics=(), settle: float = 0.0,
             max_breach: float = 0.0) -> dict:
    """Evaluate assertions over ``snapshots`` and return a result dict:

    ``{passed, samples, max_breach, checks: [{name, passed, breaches, samples,
    first_breach}]}``. ``first_breach`` is the note from the first violating
    sample (what an engineer wants to read first)."""
    if fail_on not in FAIL_ON_CHOICES:
        raise ValueError(f'fail_on must be one of {FAIL_ON_CHOICES}')
    samples = _settled(snapshots, settle)
    ignore = set(ignore_kinds)

    checks = []
    if fail_on != 'never':
        label = ('health never critical' if fail_on == 'critical'
                 else 'health never degraded or critical')
        checks.append((label, _health_check(fail_on, ignore)))
    for topic, hz in sorted((expect_rates or {}).items()):
        checks.append((f'{topic} >= {hz:g} Hz', _rate_check(topic, hz)))
    for kind in forbid_kinds:
        checks.append((f'no {kind} issues', _forbid_check(kind)))
    for node in require_nodes:
        checks.append((f'node {node} present', _presence_check('nodes', 'id', node)))
    for topic in require_topics:
        checks.append((f'topic {topic} present',
                       _presence_check('topics', 'name', topic)))

    results = []
    for name, fn in checks:
        breaches, first = 0, None
        for snap in samples:
            ok, note = fn(snap)
            if not ok:
                breaches += 1
                if first is None:
                    first = note
        n = len(samples)
        passed = n > 0 and breaches <= max_breach * n
        results.append({'name': name, 'passed': passed, 'breaches': breaches,
                        'samples': n, 'first_breach': first})

    return {
        'passed': bool(samples) and all(r['passed'] for r in results),
        'samples': len(samples),
        'max_breach': max_breach,
        'checks': results,
    }


def render_markdown(result: dict) -> str:
    """A short pass/fail table — readable in a CI log and a PR comment."""
    verdict = 'PASS' if result['passed'] else 'FAIL'
    out = [f'## rgd assert: {verdict}', '',
           f'{result["samples"]} samples evaluated · allowed breach '
           f'{result["max_breach"]:.0%}', '']
    if not result['samples']:
        out.append('No samples to evaluate.')
        return '\n'.join(out) + '\n'
    if not result['checks']:
        out.append('No checks configured.')
        return '\n'.join(out) + '\n'
    out += ['| | Check | Breaches | First breach |', '|---|---|---|---|']
    for c in result['checks']:
        mark = '✅' if c['passed'] else '❌'
        pct = c['breaches'] / c['samples'] if c['samples'] else 0
        out.append(f'| {mark} | {c["name"]} | {c["breaches"]}/{c["samples"]} '
                   f'({pct:.0%}) | {c["first_breach"] or ""} |')
    return '\n'.join(out) + '\n'


def render_junit(result: dict) -> str:
    """JUnit XML so CI systems (GitHub test reporters, colcon test-result)
    show each assertion as a test case."""
    suite = ET.Element('testsuite', name='rgd_assert',
                       tests=str(len(result['checks'])),
                       failures=str(sum(not c['passed'] for c in result['checks'])))
    for c in result['checks']:
        case = ET.SubElement(suite, 'testcase', classname='rgd_assert', name=c['name'])
        if not c['passed']:
            msg = (f'{c["breaches"]}/{c["samples"]} samples breached'
                   + (f'; first: {c["first_breach"]}' if c['first_breach'] else ''))
            ET.SubElement(case, 'failure', message=msg)
    return ET.tostring(suite, encoding='unicode') + '\n'
