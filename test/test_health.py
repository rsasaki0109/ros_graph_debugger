"""System health rollup."""

from ros_graph_debugger.health import health_line, summarize_health


def _issue(sev, title):
    return {'severity': sev, 'title': title}


def test_clean_system_is_ok():
    s = summarize_health({'issues': []})
    assert s['verdict'] == 'ok'
    assert s['headline'] is None
    assert s['counts'] == {'critical': 0, 'warning': 0, 'info': 0}


def test_warning_only_is_degraded():
    s = summarize_health({'issues': [_issue('warning', 'slow topic'),
                                     _issue('info', 'fyi')]})
    assert s['verdict'] == 'degraded'
    assert s['headline'] == 'slow topic'  # most severe present
    assert s['counts']['warning'] == 1 and s['counts']['info'] == 1


def test_any_critical_is_critical_and_leads_the_headline():
    s = summarize_health({'issues': [_issue('warning', 'w'),
                                     _issue('critical', 'bottleneck'),
                                     _issue('info', 'i')]})
    assert s['verdict'] == 'critical'
    assert s['headline'] == 'bottleneck'  # critical wins regardless of order
    assert s['issue_count'] == 3


def test_health_line_format():
    line = health_line({'issues': [_issue('critical', 'Likely bottleneck: detector'),
                                   _issue('critical', 'TF stale')]})
    assert line.startswith('**System: CRITICAL**')
    assert '2 critical' in line
    assert 'top: Likely bottleneck: detector' in line


def test_health_line_clean():
    assert health_line({'issues': []}) == '**System: OK** — no issues'
