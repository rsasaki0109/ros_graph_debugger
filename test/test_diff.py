"""Regression diff between two recordings — pure, no ROS."""

from ros_graph_debugger.diff import diff_reports, render_diff_markdown
from ros_graph_debugger.recording import make_header
from ros_graph_debugger.report import build_report

PROFILE = {'name': 'autoware', 'groups': {
    'perception': {'topic_patterns': ['^/perception/.*']}}}


def _snap(ts, objects_rate, det_cb_p95, bottleneck):
    issues = []
    if bottleneck:
        issues.append({'severity': 'critical', 'kind': 'bottleneck',
                       'title': 'Likely bottleneck: detector',
                       'related_nodes': ['/detector'],
                       'related_topics': ['/perception/objects']})
    return {
        'timestamp': ts, 'profile': 'autoware', 'nodes': [],
        'topics': [{'name': '/perception/objects', 'type': 's',
                    'publisher_count': 1, 'subscriber_count': 1,
                    'rate_hz': objects_rate, 'status': 'ok'}],
        'edges': [], 'tf_edges': [], 'diagnostics': [],
        'callbacks': [{'node': '/detector', 'callback': 'sub /image',
                       'topic': '/image', 'p95_ms': det_cb_p95}],
        'issues': issues}


def _report(rate, cb, bottleneck):
    header = make_header(100.0, 1.0, PROFILE)
    snaps = [_snap(100.0 + i, rate, cb, bottleneck) for i in range(5)]
    return build_report(header, snaps)


def test_diff_flags_rate_callback_and_issue_regressions():
    base = _report(10.0, 20.0, bottleneck=False)
    cur = _report(4.0, 210.0, bottleneck=True)
    d = diff_reports(base, cur)

    assert d['verdict'] == 'regressed'
    assert d['topic_regressions'][0]['name'] == '/perception/objects'
    assert d['topic_regressions'][0]['pct'] < 0
    assert d['callback_regressions'][0]['node'] == '/detector'
    assert any(i['kind'] == 'bottleneck' for i in d['new_issues'])
    assert d['health']['base'] == 'ok' and d['health']['cur'] == 'critical'


def test_diff_improvement_is_recognised():
    base = _report(4.0, 210.0, bottleneck=True)
    cur = _report(10.0, 20.0, bottleneck=False)
    d = diff_reports(base, cur)
    assert d['verdict'] == 'improved'
    assert d['topic_improvements'][0]['pct'] > 0
    assert any(i['kind'] == 'bottleneck' for i in d['resolved_issues'])
    assert not d['topic_regressions'] and not d['callback_regressions']


def test_diff_stable_within_tolerance():
    base = _report(10.0, 20.0, bottleneck=False)
    cur = _report(10.5, 21.0, bottleneck=False)  # < 20% change either way
    d = diff_reports(base, cur)
    assert d['verdict'] == 'stable'
    assert not d['topic_regressions'] and not d['topic_improvements']


def test_render_diff_markdown():
    d = diff_reports(_report(10.0, 20.0, False), _report(4.0, 210.0, True))
    md = render_diff_markdown(d)
    assert '**Verdict: REGRESSED**' in md
    assert 'system health ok → critical' in md
    assert 'Rate drop:' in md and '/perception/objects' in md
    assert 'Slower callback:' in md
    assert 'New issue: [bottleneck]' in md
