"""`rgd assert` — health contract over a window of snapshots (no DDS needed)."""

import json
import threading
import time
import urllib.request
from xml.etree import ElementTree as ET

import pytest

from ros_graph_debugger.assertions import evaluate, render_junit, render_markdown
from ros_graph_debugger.cli import main
from ros_graph_debugger.recording import read_recording


def _snap(t, *, rate=10.0, issues=(), nodes=('/camera', '/detector')):
    return {
        'timestamp': t,
        'nodes': [{'id': n} for n in nodes],
        'topics': [{'name': '/objects', 'rate_hz': rate}],
        'issues': [{'kind': k, 'severity': sev, 'title': f'{k} title'}
                   for k, sev in issues],
    }


def _check(result, prefix):
    return next(c for c in result['checks'] if c['name'].startswith(prefix))


def test_clean_window_passes():
    snaps = [_snap(t) for t in range(5)]
    r = evaluate(snaps, expect_rates={'/objects': 8}, require_nodes=['/detector'])
    assert r['passed'] and r['samples'] == 5
    assert all(c['breaches'] == 0 for c in r['checks'])


def test_critical_issue_fails_and_reports_first_breach():
    snaps = [_snap(0), _snap(1, issues=[('bottleneck', 'critical')]), _snap(2)]
    r = evaluate(snaps)
    c = _check(r, 'health never critical')
    assert not r['passed']
    assert c['breaches'] == 1
    assert c['first_breach'] == 'critical: bottleneck title'


def test_fail_on_degraded_catches_warnings_but_default_does_not():
    snaps = [_snap(0, issues=[('tf_stale', 'warning')])]
    assert evaluate(snaps)['passed']
    assert not evaluate(snaps, fail_on='degraded')['passed']


def test_ignored_kinds_leave_the_health_verdict():
    snaps = [_snap(0, issues=[('high_cpu', 'critical')])]
    assert evaluate(snaps, ignore_kinds=['high_cpu'])['passed']


def test_rate_expectation_breaches_on_low_or_unmeasured_rate():
    snaps = [_snap(0, rate=12.0), _snap(1, rate=4.0), _snap(2, rate=None)]
    c = _check(evaluate(snaps, expect_rates={'/objects': 10}), '/objects')
    assert c['breaches'] == 2
    assert c['first_breach'] == '4.0 Hz < 10 Hz'

    c = _check(evaluate(snaps, expect_rates={'/missing': 1}), '/missing')
    assert c['first_breach'] == 'topic not in graph'


def test_forbid_and_presence_checks():
    snaps = [_snap(0, issues=[('qos_mismatch', 'warning')], nodes=['/camera'])]
    r = evaluate(snaps, fail_on='never', forbid_kinds=['qos_mismatch'],
                 require_nodes=['/detector'], require_topics=['/objects'])
    assert not _check(r, 'no qos_mismatch')['passed']
    assert not _check(r, 'node /detector')['passed']
    assert _check(r, 'topic /objects')['passed']


def test_max_breach_tolerates_a_share_of_bad_samples():
    snaps = [_snap(t, rate=4.0 if t == 0 else 12.0) for t in range(10)]
    assert not evaluate(snaps, expect_rates={'/objects': 10})['passed']
    assert evaluate(snaps, expect_rates={'/objects': 10}, max_breach=0.1)['passed']


def test_settle_drops_warmup_samples():
    snaps = [_snap(0, rate=None), _snap(1, rate=None), _snap(5), _snap(6)]
    r = evaluate(snaps, expect_rates={'/objects': 10}, settle=3)
    assert r['passed'] and r['samples'] == 2


def test_no_samples_never_passes():
    r = evaluate([])
    assert not r['passed'] and r['samples'] == 0
    assert 'No samples' in render_markdown(r)


def test_invalid_fail_on_is_rejected():
    with pytest.raises(ValueError):
        evaluate([_snap(0)], fail_on='sometimes')


def test_renderers():
    snaps = [_snap(0, issues=[('bottleneck', 'critical')])]
    r = evaluate(snaps, expect_rates={'/objects': 5})
    md = render_markdown(r)
    assert md.startswith('## rgd assert: FAIL')
    assert '❌ | health never critical' in md
    assert '✅ | /objects >= 5 Hz' in md

    suite = ET.fromstring(render_junit(r))
    assert suite.get('tests') == '2' and suite.get('failures') == '1'
    failed = [c for c in suite if c.find('failure') is not None]
    assert [c.get('name') for c in failed] == ['health never critical']


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_demo_fails_on_the_scripted_stall(capsys):
    assert main(['assert', '--demo']) == 1
    out = capsys.readouterr().out
    assert 'Likely bottleneck: detector' in out


def test_cli_demo_passes_when_contract_allows_it(capsys, tmp_path):
    junit = tmp_path / 'assert.xml'
    rc = main(['assert', '--demo', '--fail-on', 'never',
               '--require-node', '/detector', '--forbid', 'qos_mismatch',
               '--junit', str(junit), '--json'])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)['passed'] is True
    assert ET.parse(junit).getroot().get('failures') == '0'


def test_cli_recording_file_and_bad_expect(tmp_path, capsys):
    path = tmp_path / 'run.rgd.json'
    with open(path, 'w') as f:
        f.write(json.dumps({'rgd_recording': '1', 'started': 0, 'interval': 1}) + '\n')
        for t in range(3):
            f.write(json.dumps(_snap(t)) + '\n')
    assert main(['assert', str(path), '--expect', '/objects=8']) == 0
    assert main(['assert', str(path), '--expect', '/objects=20']) == 1
    assert main(['assert', str(path), '--expect', 'nohz']) == 2


@pytest.fixture(scope='module')
def live_url():
    uvicorn = pytest.importorskip('uvicorn')
    from ros_graph_debugger.replay import ReplayStore
    from ros_graph_debugger.server import create_app

    snaps = [_snap(t) for t in range(3)]
    app = create_app(ReplayStore(snaps), web_dir='/nonexistent')
    port = 38941
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                           log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{port}'
    for _ in range(50):
        try:
            urllib.request.urlopen(url + '/api/v1/health', timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError('server did not start')
    yield url
    server.should_exit = True
    thread.join(timeout=5)


def test_cli_live_agent_and_record(live_url, tmp_path, capsys):
    rec = tmp_path / 'live.rgd.json'
    rc = main(['assert', live_url, '--duration', '0.6', '--interval', '0.2',
               '--expect', '/objects=5', '--record', str(rec)])
    assert rc == 0
    assert 'rgd assert: PASS' in capsys.readouterr().out
    _, snaps = read_recording(str(rec))
    assert len(snaps) >= 2


def test_cli_unreachable_agent_is_an_error():
    rc = main(['assert', 'http://127.0.0.1:9', '--duration', '0.1',
               '--interval', '0.1'])
    assert rc == 2
