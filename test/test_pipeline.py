"""Pipeline path tracer: the constraining source->sink route through a focus."""

from ros_graph_debugger.pipeline import resolve_focus, trace_pipeline_path


def _node(nid, pubs, subs):
    return {'id': nid, 'name': nid.lstrip('/'), 'publishers': pubs, 'subscribers': subs}


def _topic(name, rate, pubs, subs, status='ok'):
    return {'name': name, 'rate_hz': rate, 'status': status,
            'publishers': pubs, 'subscribers': subs}


def _linear():
    """camera -> /cam -> detector -> /obj(4.1) -> tracker -> /trk -> planner -> /traj -> controller"""
    nodes = [
        _node('/camera', ['/cam'], []),
        _node('/detector', ['/obj'], ['/cam']),
        _node('/tracker', ['/trk'], ['/obj']),
        _node('/planner', ['/traj'], ['/trk']),
        _node('/controller', [], ['/traj']),
    ]
    topics = [
        _topic('/cam', 30.0, ['/camera'], ['/detector']),
        _topic('/obj', 4.1, ['/detector'], ['/tracker'], status='critical'),
        _topic('/trk', 10.0, ['/tracker'], ['/planner']),
        _topic('/traj', 10.0, ['/planner'], ['/controller']),
    ]
    return {'nodes': nodes, 'topics': topics, 'issues': []}


def test_path_through_node_spans_source_to_sink():
    p = trace_pipeline_path(_linear(), '/detector')
    assert p['nodes'] == ['/camera', '/detector', '/tracker', '/planner', '/controller']
    assert p['pivot'] == '/detector'
    # The 4.1 Hz hop is the constraining link.
    assert p['bottleneck_topic'] == '/obj'
    assert [h['topic'] for h in p['hops']] == ['/cam', '/obj', '/trk', '/traj']


def test_path_through_topic_pivots_on_its_publisher():
    p = trace_pipeline_path(_linear(), '/obj')
    assert p['pivot'] == '/detector'  # /obj's publisher
    assert p['nodes'][0] == '/camera' and p['nodes'][-1] == '/controller'
    assert p['bottleneck_topic'] == '/obj'


def test_path_follows_the_lowest_rate_branch():
    # detector fans out to a fast and a slow consumer; the path takes the slow one.
    d = _linear()
    d['nodes'].append(_node('/logger', [], ['/obj_fast']))
    d['nodes'][1]['publishers'].append('/obj_fast')
    d['topics'].append(_topic('/obj_fast', 50.0, ['/detector'], ['/logger']))
    p = trace_pipeline_path(d, '/detector')
    # From detector the constraining (lowest-rate) output is /obj (4.1), not /obj_fast.
    first_out = next(h for h in p['hops'] if h['from'] == '/detector')
    assert first_out['topic'] == '/obj'


def test_cycle_terminates():
    d = {'nodes': [_node('/a', ['/t1'], ['/t2']), _node('/b', ['/t2'], ['/t1'])],
         'topics': [_topic('/t1', 5.0, ['/a'], ['/b']),
                    _topic('/t2', 5.0, ['/b'], ['/a'])],
         'issues': []}
    p = trace_pipeline_path(d, '/a')
    assert p is not None and len(p['hops']) < 50  # bounded, no infinite loop


def test_hops_carry_consumer_callback_p95():
    d = _linear()
    d['callbacks'] = [
        {'node': '/tracker', 'topic': '/obj', 'p95_ms': 210.0},  # consumes /obj
        {'node': '/detector', 'topic': '/cam', 'p95_ms': 12.0},
    ]
    p = trace_pipeline_path(d, '/detector')
    obj_hop = next(h for h in p['hops'] if h['topic'] == '/obj')
    assert obj_hop['cb_p95_ms'] == 210.0  # tracker's callback on /obj
    assert p['cb_bottleneck_node'] == '/tracker'  # slowest callback on the path


def test_hops_have_none_callback_without_traces():
    p = trace_pipeline_path(_linear(), '/detector')  # no callbacks key
    assert all(h['cb_p95_ms'] is None for h in p['hops'])
    assert p['cb_bottleneck_node'] is None


def test_unknown_focus_returns_none():
    assert trace_pipeline_path(_linear(), '/nope') is None


def test_isolated_node_has_no_path():
    d = {'nodes': [_node('/lonely', [], [])], 'topics': [], 'issues': []}
    assert trace_pipeline_path(d, '/lonely') is None


def test_resolve_focus_prefers_node_then_topic():
    d = _linear()
    assert resolve_focus(d, '/detector') == ('node', '/detector')
    assert resolve_focus(d, 'detector') == ('node', '/detector')   # by name
    assert resolve_focus(d, '/obj') == ('topic', '/obj')
    assert resolve_focus(d, 'nope') == (None, None)
