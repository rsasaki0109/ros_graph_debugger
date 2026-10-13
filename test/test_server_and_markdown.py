"""Server endpoints + Markdown briefing tests.

The HTTP endpoints are exercised against a real uvicorn server in a thread
(faithful to how users hit it, and free of TestClient/httpx version coupling).
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
import uvicorn

from ros_graph_debugger.markdown import snapshot_to_markdown
from ros_graph_debugger.model import Issue, NodeInfo, RuntimeGraphStore, TopicInfo
from ros_graph_debugger.server import create_app


def _store():
    store = RuntimeGraphStore()
    t = TopicInfo(name='/objects', type='std_msgs/msg/String',
                  publisher_count=1, subscriber_count=1,
                  publishers=['/detector'], subscribers=['/tracker'])
    t.probed = True
    t.rate_hz = 4.2
    t.bandwidth_bps = 12_000
    t.status = 'warning'
    n = NodeInfo(id='/detector', name='detector', publishers=['/objects'])
    n.cpu_percent = 95.0
    n.rss_bytes = 1_800_000_000
    n.process_mapping_confidence = 'medium'
    store.set_graph({'/detector': n, '/tracker': NodeInfo(id='/tracker', name='tracker')},
                    {'/objects': t})
    store.set_issues([Issue(
        id='x', severity='critical', kind='bottleneck',
        title='Likely bottleneck: detector',
        explanation='output dropped while inputs healthy',
        evidence=['/objects: 4.2 Hz (expected >= 10.0)', 'detector CPU: 95%'],
        suggested_actions=['Inspect callback duration'],
        related_nodes=['/detector'], related_topics=['/objects'])])
    return store


@pytest.fixture(scope='module')
def base_url():
    app = create_app(_store(), web_dir='/nonexistent',
                     profile_data={'name': 'autoware', 'groups': {}})
    port = 38939
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error')
    server = uvicorn.Server(config)
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


def _get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_rest_endpoints(base_url):
    assert _get_json(base_url + '/api/v1/health')['status'] == 'ok'
    assert _get_json(base_url + '/api/v1/profile')['name'] == 'autoware'

    snap = _get_json(base_url + '/api/v1/snapshot')
    assert len(snap['nodes']) == 2
    assert len(snap['topics']) == 1
    assert len(snap['issues']) == 1
    assert snap['edges'][0]['topic'] == '/objects'

    issues = _get_json(base_url + '/api/v1/issues')
    assert issues[0]['kind'] == 'bottleneck'


def test_markdown_leads_with_health_verdict():
    md = snapshot_to_markdown(_store().snapshot())
    # The bottom line comes first: one critical bottleneck issue.
    assert '**System: CRITICAL**' in md
    assert 'top: Likely bottleneck: detector' in md


def test_summary_endpoint(base_url):
    s = _get_json(base_url + '/api/v1/summary')
    assert s['verdict'] == 'critical'
    assert s['counts']['critical'] >= 1
    assert s['headline'] == 'Likely bottleneck: detector'


def test_markdown_briefing():
    md = snapshot_to_markdown(_store().snapshot())
    assert '# ROS Graph Debugger' in md
    assert 'Likely bottleneck: detector' in md
    assert 'Evidence:' in md
    # topic table renders rate + node metrics section renders cpu
    assert '4.2 Hz' in md
    assert '95%' in md


def test_snapshot_md_endpoint(base_url):
    with urllib.request.urlopen(base_url + '/api/v1/snapshot.md', timeout=5) as r:
        text = r.read().decode()
    assert 'Runtime Snapshot' in text
    assert 'Likely bottleneck: detector' in text


def _store_with_unrelated():
    """detector -> /objects -> tracker, plus an unrelated planner -> /traj."""
    store = _store()
    d = store.snapshot().to_dict()
    nodes = {n['id']: NodeInfo(id=n['id'], name=n['name'],
                               publishers=n['publishers'], subscribers=n['subscribers'])
             for n in d['nodes']}
    nodes['/planner'] = NodeInfo(id='/planner', name='planner', publishers=['/traj'])
    topics = {t['name']: TopicInfo(name=t['name'], type=t['type'],
                                   publisher_count=t['publisher_count'],
                                   subscriber_count=t['subscriber_count'],
                                   publishers=t['publishers'], subscribers=t['subscribers'])
              for t in d['topics']}
    topics['/traj'] = TopicInfo(name='/traj', type='std_msgs/msg/String',
                                publisher_count=1, subscriber_count=0,
                                publishers=['/planner'], subscribers=[])
    store.set_graph(nodes, topics)
    return store


def test_focused_briefing_keeps_neighbourhood_drops_the_rest():
    md = snapshot_to_markdown(_store_with_unrelated().snapshot(), focus='/detector')
    assert 'Focused on **/detector**' in md
    # The detector's neighbourhood is present...
    assert '/objects' in md
    assert '/tracker' in md
    assert 'Likely bottleneck: detector' in md  # issue touches the focus
    # ...the unrelated planner/traj are sliced away.
    assert '/planner' not in md
    assert '/traj' not in md


def test_focused_briefing_resolves_by_name_and_suffix():
    snap = _store_with_unrelated().snapshot()
    assert 'Focused on **/detector**' in snapshot_to_markdown(snap, focus='detector')
    assert 'Focused on **/planner**' in snapshot_to_markdown(snap, focus='planner')


def test_focused_briefing_on_a_topic_keeps_its_endpoints():
    md = snapshot_to_markdown(_store_with_unrelated().snapshot(), focus='/objects')
    assert 'Focused on **/objects**' in md
    # Both endpoint nodes of /objects are named as neighbours...
    assert '/detector' in md and '/tracker' in md
    # ...and the unrelated planner/traj are sliced away.
    assert '/planner' not in md and '/traj' not in md


def test_focused_briefing_unknown_target_is_explicit():
    md = snapshot_to_markdown(_store().snapshot(), focus='/nope')
    assert 'No node or topic matching `/nope`' in md


def test_snapshot_md_endpoint_focus(base_url):
    url = base_url + '/api/v1/snapshot.md?focus=' + urllib.parse.quote('/detector')
    with urllib.request.urlopen(url, timeout=5) as r:
        text = r.read().decode()
    assert 'Focused on **/detector**' in text
    assert 'Likely bottleneck: detector' in text


def test_markdown_renders_callbacks_section():
    from ros_graph_debugger.model import CallbackStat
    store = _store()
    store.set_callbacks([CallbackStat(node='/detector', callback='sub /image',
                                      topic='/image', count=100, mean_ms=120.0,
                                      p95_ms=210.0, max_ms=260.0)])
    md = snapshot_to_markdown(store.snapshot())
    assert '## Callbacks' in md
    assert '210 ms' in md and 'sub /image' in md


def test_focused_briefing_includes_pipeline_path():
    md = snapshot_to_markdown(_store_with_unrelated().snapshot(), focus='/detector')
    assert '## Pipeline path' in md
    # detector -> /objects -> tracker is on the path, and the focus is bolded.
    assert '**detector**' in md
    assert '/objects' in md and '⟵ slowest' in md


def test_pipeline_path_line_shows_callback_time():
    from ros_graph_debugger.model import CallbackStat
    store = _store_with_unrelated()
    store.set_callbacks([CallbackStat(node='/tracker', callback='sub /objects',
                                      topic='/objects', p95_ms=210.0)])
    md = snapshot_to_markdown(store.snapshot(), focus='/detector')
    assert '[duration 210 ms; unknown ⟵ slowest duration]' in md


def test_path_endpoint(base_url):
    p = _get_json(base_url + '/api/v1/path?target=' + urllib.parse.quote('/detector'))
    assert p['pivot'] == '/detector'
    assert '/detector' in p['nodes'] and '/tracker' in p['nodes']
    assert p['bottleneck_topic'] == '/objects'


def test_path_endpoint_unknown_target_404(base_url):
    try:
        _get_json(base_url + '/api/v1/path?target=/nope')
        assert False, 'expected 404'
    except urllib.error.HTTPError as e:
        assert e.code == 404


@pytest.mark.parametrize('scope,label', [
    ('probe_receipt', 'probe receipts'),
    ('subscriber_statistics', 'subscriber statistics'),
    ('bag_recording', 'bag recording'),
    (None, 'unknown'),
    ('future_scope', 'unknown'),
    (['invalid'], 'unknown'),
])
def test_briefing_names_rate_location_even_without_issues(scope, label):
    store = _store()
    store.set_issues([])
    store.update_topic_metrics('/objects', rate_measurement_scope=scope)
    md = snapshot_to_markdown(store.snapshot(), focus='/objects')
    assert 'No issues detected.' in md
    assert '| rate scope |' in md
    assert f'| 4.2 Hz | {label} |' in md
    assert 'Delivery completeness and publisher-side throughput are unknown' in md
    assert 'low rate alone does not prove a slow publishing callback' in md


def test_briefing_separates_signed_dds_gap_probe_wait_and_header_age():
    store = _store()
    store.update_topic_metrics('/objects', rate_hz=None, probe_source_receipt_gap_p95_ms=-20,
                               probe_callback_delay_p95_ms=450, header_age_p95_ms=1300)
    md = snapshot_to_markdown(store.snapshot(), focus='/objects')
    assert '| DDS stamp gap p95 | probe wait p95 | age p95 |' in md
    assert '| -20.0 ms | 450.0 ms | 1300 ms |' in md
    assert 'clock synchronization is unknown' in md
    assert 'not a measured network latency or publisher callback duration' in md
    # Old snapshots have no metadata. Never fabricate a zero gap.
    old = store.snapshot().to_dict()
    del old['topics'][0]['probe_source_receipt_gap_p95_ms']
    assert '| — | 450.0 ms | 1300 ms |' in snapshot_to_markdown(old)


def test_briefing_shows_old_rate_with_separate_sample_processing_silence():
    store = _store()
    store.update_topic_metrics('/objects', rate_hz=30, probe_rate_window_samples=50,
                               probe_rate_window_duration_ms=1633.333,
                               probe_rate_window_clock='rmw_received_timestamp',
                               probe_last_sample_age_ms=9000)
    md = snapshot_to_markdown(store.snapshot(), focus='/objects')
    assert '| 30.0 Hz |' in md
    assert '| /objects | 50 | 1633.3 ms | RMW receive timestamps | 9000.0 ms ago |' in md
    assert 'retains its last sample window during silence' in md
    assert 'not middleware receipt age, header age or publisher activity' in md
    old = store.snapshot().to_dict()
    for t in old['topics']:
        for k in ('probe_rate_window_samples', 'probe_rate_window_duration_ms',
                  'probe_rate_window_clock', 'probe_last_sample_age_ms'):
            t.pop(k, None)
    assert '## Probe sample windows' not in snapshot_to_markdown(old)


@pytest.mark.parametrize('value', [None, True, -1, float('nan'), float('inf'), '0', {}])
def test_briefing_does_not_turn_invalid_sample_metadata_into_zero(value):
    store = _store()
    store.update_topic_metrics('/objects', probe_rate_window_samples=50,
                               probe_rate_window_duration_ms=value,
                               probe_rate_window_clock=['unknown'],
                               probe_last_sample_age_ms=value)
    md = snapshot_to_markdown(store.snapshot(), focus='/objects')
    assert '| /objects | 50 | — | unknown | — |' in md


def test_briefing_measured_empty_probe_is_distinct_from_unknown():
    store = _store()
    store.update_topic_metrics('/objects', rate_hz=None, probe_rate_window_samples=0)
    md = snapshot_to_markdown(store.snapshot(), focus='/objects')
    assert '| /objects | 0 | — | unknown | — |' in md
    assert '| /image |' not in md  # Focus keeps unrelated probe evidence out.
