"""Fleet-wide snapshot merge — pure, no agents needed."""

from ros_graph_debugger.federation import (
    FederatedStore,
    merge_snapshots,
)
from ros_graph_debugger.health import summarize_health
from ros_graph_debugger.markdown import snapshot_to_markdown


def _snap(rate, critical):
    issues = []
    if critical:
        issues = [{'id': 'bn1', 'severity': 'critical', 'kind': 'bottleneck',
                   'title': 'Likely bottleneck: detector',
                   'related_nodes': ['/detector'], 'related_topics': ['/scan'],
                   'related_frames': ['base_link']}]
    return {
        'timestamp': 1.0, 'profile': 'nav2',
        'nodes': [{'id': '/detector', 'name': 'detector',
                   'publishers': ['/scan'], 'subscribers': []}],
        'topics': [{'name': '/scan', 'type': 's', 'publisher_count': 1,
                    'subscriber_count': 0, 'publishers': ['/detector'],
                    'subscribers': [], 'rate_hz': rate, 'status': 'ok'}],
        'edges': [], 'tf_edges': [{'parent': 'map', 'child': 'base_link',
                                   'status': 'ok', 'age_ms': 10.0}],
        'diagnostics': [], 'callbacks': [], 'issues': issues}


def test_merge_namespaces_by_host():
    merged = merge_snapshots({'robot1': _snap(10.0, False),
                              'robot2': _snap(2.0, True)})
    ids = {n['id'] for n in merged['nodes']}
    assert ids == {'/robot1/detector', '/robot2/detector'}  # distinct per robot
    names = {t['name'] for t in merged['topics']}
    assert names == {'/robot1/scan', '/robot2/scan'}
    # TF frames are host-prefixed too.
    assert {e['parent'] for e in merged['tf_edges']} == {'robot1/map', 'robot2/map'}
    # Every element carries its host.
    assert all(n['host'] in ('robot1', 'robot2') for n in merged['nodes'])
    assert merged['profile'] == 'federated'


def test_merge_aggregates_issues_and_per_host_health():
    merged = merge_snapshots({'robot1': _snap(10.0, False),
                              'robot2': _snap(2.0, True)})
    # The one critical issue is namespaced and points at robot2's node/topic.
    assert len(merged['issues']) == 1
    iss = merged['issues'][0]
    assert iss['title'].startswith('[robot2]')
    assert iss['related_nodes'] == ['/robot2/detector']
    assert iss['related_topics'] == ['/robot2/scan']
    assert iss['related_frames'] == ['robot2/base_link']
    # Per-host verdicts surface in `hosts`.
    by_host = {h['host']: h['verdict'] for h in merged['hosts']}
    assert by_host == {'robot1': 'ok', 'robot2': 'critical'}
    # Fleet rollup reads critical (worst across the fleet).
    assert summarize_health(merged)['verdict'] == 'critical'


def test_merge_accepts_list_and_renders_markdown():
    merged = merge_snapshots([('a', _snap(10.0, False)), ('b', _snap(1.0, True))])
    md = snapshot_to_markdown(merged)
    assert '[b] Likely bottleneck: detector' in md
    assert '/b/scan' in md  # namespaced topic shows in the table


def test_merge_tolerates_empty_and_missing_sections():
    merged = merge_snapshots({'r1': {}, 'r2': _snap(5.0, False)})
    assert len(merged['nodes']) == 1  # r1 contributed nothing, no crash
    assert {h['host'] for h in merged['hosts']} == {'r1', 'r2'}


def test_federated_store_merges_via_injected_fetch():
    data = {'http://a': _snap(10.0, False), 'http://b': _snap(2.0, True)}
    store = FederatedStore([('alpha', 'http://a'), ('beta', 'http://b')],
                           fetch=lambda base: data[base])
    snap = store.snapshot().to_dict()
    assert {n['id'] for n in snap['nodes']} == {'/alpha/detector', '/beta/detector'}
    assert summarize_health(snap)['verdict'] == 'critical'  # beta is critical
    assert {h['host'] for h in snap['hosts']} == {'alpha', 'beta'}


def test_federated_store_skips_unreachable_agent():
    def fetch(base):
        if base == 'http://down':
            raise OSError('connection refused')
        return _snap(10.0, False)
    store = FederatedStore([('up', 'http://up'), ('down', 'http://down')],
                           fetch=fetch)
    snap = store.snapshot().to_dict()
    # The reachable agent still contributes; the dead one is just absent.
    assert {n['id'] for n in snap['nodes']} == {'/up/detector'}
    # refresh() picks up the agent once it recovers.
    store._fetch = lambda base: _snap(5.0, False)
    store.refresh()
    assert {n['id'] for n in store.snapshot().to_dict()['nodes']} == \
        {'/up/detector', '/down/detector'}
