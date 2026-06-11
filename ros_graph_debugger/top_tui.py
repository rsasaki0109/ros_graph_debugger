"""Terminal dashboard for ``rgd top``.

Textual is imported lazily by ``run_top`` so the base package can stay usable
without the optional TUI dependency.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable


SEV_RANK = {'critical': 3, 'warning': 2, 'info': 1, 'ok': 0, 'unknown': 0}
STATUS_RANK = {'critical': 3, 'warning': 2, 'ok': 1, 'unknown': 0}
BLOCKS = '▁▂▃▄▅▆▇█'
SORT_KEYS = ('severity', 'rate', 'cpu', 'callback')


def textual_missing_message() -> str:
    return ('rgd top requires the optional Textual UI dependency.\n'
            'Install it with: pip install ros_graph_debugger[tui]')


@dataclass
class TopRow:
    kind: str
    name: str
    status: str
    rate_hz: float | None = None
    expected_hz: float | None = None
    cpu_percent: float | None = None
    callback_p95_ms: float | None = None
    spark: str = ''
    issue: str = ''


@dataclass
class TopModel:
    timestamp: float
    readiness: list[tuple[str, str]]
    rows: list[TopRow]
    issues: list[dict]


@dataclass
class TopHistory:
    window_s: float = 30.0
    rates: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    def update(self, snap: dict) -> None:
        now = float(snap.get('timestamp') or time.time())
        active = set()
        for topic in snap.get('topics', []):
            name = topic.get('name', '')
            rate = topic.get('rate_hz')
            if not isinstance(rate, (int, float)):
                continue
            active.add(name)
            samples = self.rates.setdefault(name, [])
            if not samples or samples[-1] != (now, float(rate)):
                samples.append((now, float(rate)))
            cutoff = now - self.window_s
            while samples and samples[0][0] < cutoff:
                samples.pop(0)
            if len(samples) > 90:
                del samples[:-90]
        for name in list(self.rates):
            if name not in active:
                del self.rates[name]

    def sparkline(self, topic: str, width: int = 12) -> str:
        samples = self.rates.get(topic, [])
        return sparkline([rate for _, rate in samples], width=width)


def sparkline(values: list[float], width: int = 12) -> str:
    if width <= 0:
        return ''
    if not values:
        return ''
    vals = values[-width:]
    vmax = max(vals)
    vmin = min(vals)
    if vmax <= vmin:
        idx = 0 if vmax <= 0 else len(BLOCKS) // 2
        return BLOCKS[idx] * len(vals)
    out = []
    for v in vals:
        pos = round((v - vmin) / (vmax - vmin) * (len(BLOCKS) - 1))
        out.append(BLOCKS[max(0, min(len(BLOCKS) - 1, pos))])
    return ''.join(out)


def build_top_model(snap: dict, history: TopHistory | None = None,
                    sort_key: str = 'severity', query: str = '',
                    profile: dict | None = None) -> TopModel:
    if history:
        history.update(snap)
    profile = profile or {}
    issue_map = _issue_map(snap)
    cb_map = _callback_map(snap)
    expected = _expected_rates(snap)
    rows: list[TopRow] = []

    for topic in snap.get('topics', []):
        name = topic.get('name', '')
        if name in ('/rosout', '/parameter_events'):
            continue
        rows.append(TopRow(
            kind='topic',
            name=name,
            status=topic.get('status', 'unknown'),
            rate_hz=_num(topic.get('rate_hz')),
            expected_hz=expected.get(name),
            callback_p95_ms=cb_map.get(('topic', name)),
            spark=history.sparkline(name) if history else '',
            issue=issue_map.get(('topic', name), ''),
        ))

    for node in snap.get('nodes', []):
        node_id = node.get('id', '')
        rows.append(TopRow(
            kind='node',
            name=node_id,
            status=node.get('status', 'unknown'),
            cpu_percent=_num(node.get('cpu_percent')),
            callback_p95_ms=cb_map.get(('node', node_id)),
            issue=issue_map.get(('node', node_id), ''),
        ))

    rows = _filter_rows(rows, query)
    rows.sort(key=lambda r: _sort_value(r, sort_key), reverse=True)
    return TopModel(
        timestamp=float(snap.get('timestamp') or 0.0),
        readiness=_readiness(snap, profile),
        rows=rows[:40],
        issues=sorted(snap.get('issues', []),
                      key=lambda i: SEV_RANK.get(i.get('severity'), 0),
                      reverse=True)[:8],
    )


def render_plain(model: TopModel, width: int = 100, height: int = 28) -> str:
    width = max(40, width)
    height = max(10, height)
    out = []
    readiness = ' '.join(f'[{stage}:{status.upper()}]'
                         for stage, status in model.readiness)
    out.append(_clip('ROS Graph Top  ' + readiness, width))
    out.append(_clip('KIND  STATUS    RATE  EXPECT  CPU  P95   SPARK        NAME', width))
    row_budget = max(3, height - 7)
    for row in model.rows[:row_budget]:
        line = (f'{row.kind[:5]:<5} {row.status[:8]:<8} '
                f'{_fmt(row.rate_hz, 5)} {_fmt(row.expected_hz, 6)} '
                f'{_fmt(row.cpu_percent, 4)} {_fmt(row.callback_p95_ms, 5)} '
                f'{row.spark[:12]:<12} {row.name}')
        out.append(_clip(line, width))
    out.append(_clip('Issues', width))
    for issue in model.issues[:max(1, height - len(out))]:
        evidence = (issue.get('evidence') or [''])[0]
        line = f'[{issue.get("severity", "info").upper()}] {issue.get("title", "")} {evidence}'
        out.append(_clip(line, width))
    return '\n'.join(out[:height])


def create_top_app(source, *, interval: float = 0.5, profile: dict | None = None):
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.widgets import DataTable, Footer, Header, Input, Static

    class TopApp(App):
        CSS = """
        Screen { background: #0d1117; color: #c9d1d9; }
        #ready { height: 3; padding: 0 1; }
        #table { height: 1fr; }
        #issues { height: 7; border: tall #2d333b; padding: 0 1; }
        Input { height: 3; }
        .critical { color: #f85149; text-style: bold; }
        .warning { color: #d29922; }
        .ok { color: #3fb950; }
        """
        BINDINGS = [
            ('q', 'quit', 'Quit'),
            ('s', 'cycle_sort', 'Sort'),
            ('/', 'filter', 'Filter'),
            ('p', 'pause', 'Pause'),
        ]

        def __init__(self):
            super().__init__()
            self.history = TopHistory()
            self.sort_idx = 0
            self.query = ''
            self.paused = False
            self.last = None

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Vertical():
                yield Static('', id='ready')
                table = DataTable(id='table', zebra_stripes=True)
                table.add_columns('kind', 'status', 'rate', 'expect', 'cpu',
                                  'p95', 'spark', 'name')
                yield table
                yield Static('', id='issues')
                yield Input(placeholder='filter rows...', id='filter')
            yield Footer()

        def on_mount(self) -> None:
            self.query_one('#filter', Input).display = False
            self.set_interval(interval, self.refresh_snapshot)
            self.refresh_snapshot()

        def refresh_snapshot(self) -> None:
            if self.paused and self.last is not None:
                snap = self.last
            else:
                snap = source()
                self.last = snap
            model = build_top_model(snap, self.history,
                                    SORT_KEYS[self.sort_idx], self.query,
                                    profile=profile)
            self._render(model)

        def _render(self, model: TopModel) -> None:
            ready = ' '.join(f'[{stage}:{status.upper()}]'
                             for stage, status in model.readiness)
            self.query_one('#ready', Static).update(
                f' sort={SORT_KEYS[self.sort_idx]}  filter={self.query or "-"}  {ready}')
            table = self.query_one('#table', DataTable)
            table.clear()
            for row in model.rows:
                table.add_row(row.kind, row.status, _fmt(row.rate_hz, 5),
                              _fmt(row.expected_hz, 5),
                              _fmt(row.cpu_percent, 4),
                              _fmt(row.callback_p95_ms, 5), row.spark,
                              row.name)
            issue_lines = []
            for issue in model.issues:
                evidence = (issue.get('evidence') or [''])[0]
                issue_lines.append(
                    f'[{issue.get("severity", "info").upper()}] '
                    f'{issue.get("title", "")}  {evidence}')
            self.query_one('#issues', Static).update('\n'.join(issue_lines)
                                                     or 'No issues.')

        def action_cycle_sort(self) -> None:
            self.sort_idx = (self.sort_idx + 1) % len(SORT_KEYS)
            self.refresh_snapshot()

        def action_pause(self) -> None:
            self.paused = not self.paused
            self.refresh_snapshot()

        def action_filter(self) -> None:
            inp = self.query_one('#filter', Input)
            inp.display = True
            inp.focus()

        def on_input_submitted(self, event) -> None:
            if event.input.id != 'filter':
                return
            self.query = event.value.strip()
            event.input.display = False
            self.refresh_snapshot()

    return TopApp()


def run_top(source, *, interval: float = 0.5, profile: dict | None = None) -> int:
    try:
        app = create_top_app(source, interval=interval, profile=profile)
    except ImportError:
        print(textual_missing_message())
        return 0
    app.run()
    return 0


def http_source(base: str) -> Callable[[], dict]:
    base = base.rstrip('/')

    def fetch() -> dict:
        with urllib.request.urlopen(base + '/api/v1/snapshot', timeout=5) as r:
            return json.loads(r.read())

    return fetch


def recording_source(snaps: list[dict], loop: bool = True) -> Callable[[], dict]:
    idx = {'value': 0}

    def next_snap() -> dict:
        if not snaps:
            return {'nodes': [], 'topics': [], 'issues': []}
        snap = snaps[idx['value']]
        if idx['value'] < len(snaps) - 1:
            idx['value'] += 1
        elif loop:
            idx['value'] = 0
        return snap

    return next_snap


def _issue_map(snap: dict) -> dict[tuple[str, str], str]:
    out = {}
    issues = sorted(snap.get('issues', []),
                    key=lambda i: SEV_RANK.get(i.get('severity'), 0),
                    reverse=True)
    for issue in issues:
        title = issue.get('title', '')
        for topic in issue.get('related_topics', []):
            out.setdefault(('topic', topic), title)
        for node in issue.get('related_nodes', []):
            out.setdefault(('node', node), title)
    return out


def _callback_map(snap: dict) -> dict[tuple[str, str], float]:
    out = {}
    for cb in snap.get('callbacks', []):
        p95 = _num(cb.get('p95_ms'))
        if p95 is None:
            continue
        node = cb.get('node', '')
        topic = cb.get('topic', '')
        out[('node', node)] = max(out.get(('node', node), 0.0), p95)
        if topic:
            out[('topic', topic)] = max(out.get(('topic', topic), 0.0), p95)
    return out


def _expected_rates(snap: dict) -> dict[str, float]:
    expected = {}
    for issue in snap.get('issues', []):
        for evidence in issue.get('evidence', []):
            m = re.search(r'([^:]+):\s*[0-9.]+\s*Hz\s*\(expected\s*[>=]+\s*([0-9.]+)\)',
                          evidence)
            if m:
                expected[m.group(1).strip()] = float(m.group(2))
    return expected


def _readiness(snap: dict, profile: dict | None) -> list[tuple[str, str]]:
    groups = (profile or {}).get('groups') or {}
    if not groups:
        crit = any(i.get('severity') == 'critical' for i in snap.get('issues', []))
        warn = any(i.get('severity') == 'warning' for i in snap.get('issues', []))
        return [('system', 'critical' if crit else 'warning' if warn else 'ok')]
    compiled = {}
    for stage, data in groups.items():
        compiled[stage] = []
        for pat in data.get('topic_patterns', []):
            try:
                compiled[stage].append(re.compile(pat))
            except re.error:
                pass
    worst = {stage: 'unknown' for stage in groups}

    def stage_of(topic):
        for stage, pats in compiled.items():
            if any(p.search(topic) for p in pats):
                return stage
        return None

    def bump(stage, status):
        if stage and STATUS_RANK.get(status, 0) > STATUS_RANK.get(worst[stage], 0):
            worst[stage] = status

    for topic in snap.get('topics', []):
        bump(stage_of(topic.get('name', '')), topic.get('status', 'unknown'))
    for issue in snap.get('issues', []):
        status = 'ok' if issue.get('severity') == 'info' else issue.get('severity', 'unknown')
        for topic in issue.get('related_topics', []):
            bump(stage_of(topic), status)
    return [(stage, worst[stage]) for stage in groups]


def _filter_rows(rows: list[TopRow], query: str) -> list[TopRow]:
    q = query.lower().strip()
    if not q:
        return rows
    return [r for r in rows if q in r.name.lower() or q in r.issue.lower()
            or q in r.kind.lower()]


def _sort_value(row: TopRow, sort_key: str):
    if sort_key == 'rate':
        return row.rate_hz or -1.0
    if sort_key == 'cpu':
        return row.cpu_percent or -1.0
    if sort_key == 'callback':
        return row.callback_p95_ms or -1.0
    return (SEV_RANK.get(row.status, 0), bool(row.issue),
            row.callback_p95_ms or 0.0, row.cpu_percent or 0.0)


def _fmt(value, width: int) -> str:
    if not isinstance(value, (int, float)):
        return '—'.rjust(width)
    return f'{value:.1f}'.rjust(width)


def _num(value):
    return float(value) if isinstance(value, (int, float)) else None


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(0, width - 1)] + '…'
