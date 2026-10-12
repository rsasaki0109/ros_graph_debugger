"""Shared ROS buses must not invent data-flow dependencies between nodes."""

import copy

import pytest

from ros_graph_debugger.federation import merge_snapshots
from ros_graph_debugger.pipeline import pipeline_timeline, trace_pipeline_path
from ros_graph_debugger.recording import make_header
from ros_graph_debugger.report import build_report, render_markdown


def graph(control='/parameter_events', declared='rcl_interfaces/msg/ParameterEvent'):
    nodes = [
        {'id': '/source', 'name': 'source', 'publishers': ['/input', control],
         'subscribers': [control]},
        {'id': '/worker', 'name': 'worker', 'publishers': ['/output', control],
         'subscribers': ['/input', control]},
        {'id': '/sink', 'name': 'sink', 'publishers': [control],
         'subscribers': ['/output', control]},
        {'id': '/aux', 'name': 'aux', 'publishers': [control], 'subscribers': [control]},
    ]
    topics = [
        {'name': '/input', 'type': 'std_msgs/msg/Float64', 'rate_hz': 20., 'status': 'ok',
         'publishers': ['/source'], 'subscribers': ['/worker']},
        {'name': '/output', 'type': 'std_msgs/msg/Float64', 'rate_hz': 5., 'status': 'ok',
         'publishers': ['/worker'], 'subscribers': ['/sink']},
        {'name': control, 'type': declared, 'rate_hz': 0.1, 'status': 'critical',
         'publishers': [n['id'] for n in nodes], 'subscribers': [n['id'] for n in nodes]},
    ]
    return {
        'timestamp': 10., 'nodes': nodes, 'topics': topics,
        'callbacks': [
            {'node': '/worker', 'callback': 'input', 'topic': '/input',
             'count': 50, 'p95_ms': 12., 'source': 'trace'},
            {'node': '/aux', 'callback': 'control', 'topic': control,
             'count': 10, 'p95_ms': 1000., 'source': 'application_report'},
        ],
        'issues': [{'id': 'stale-control', 'severity': 'critical', 'kind': 'topic_stale',
                    'title': 'Control is stale', 'explanation': 'An observed control fault',
                    'related_nodes': ['/aux'], 'related_topics': [control], 'related_frames': [],
                    'evidence': ['retained evidence'], 'suggested_actions': []}],
    }


@pytest.mark.parametrize('name,declared', [
    ('/parameter_events', 'rcl_interfaces/msg/ParameterEvent'),
    ('/rosout', 'rcl_interfaces/msg/Log'),
    ('/clock', 'rosgraph_msgs/msg/Clock'),
    ('/diagnostics', 'diagnostic_msgs/msg/DiagnosticArray'),
    ('/statistics', 'statistics_msgs/msg/MetricsMessage'),
    ('/tf_static', 'tf2_msgs/msg/TFMessage'),
    ('/robot/remapped_parameter_bus', 'rcl_interfaces/msg/ParameterEvent'),
    ('/robot/remapped_clock', 'rosgraph_msgs/msg/Clock'),
])
def test_slow_control_bus_does_not_choose_unrelated_nodes_or_durations(name, declared):
    sample = graph(name, declared)
    original = copy.deepcopy(sample)
    path = trace_pipeline_path(sample, '/worker')
    assert path['nodes'] == ['/source', '/worker', '/sink']
    assert [h['topic'] for h in path['hops']] == ['/input', '/output']
    assert path['bottleneck_topic'] == '/output'
    assert path['cb_bottleneck_node'] == '/worker'
    assert path['hops'][0]['cb_p95_ms'] == 12. and path['hops'][0]['cb_source'] == 'trace'
    assert sample == original  # Faults, buses and their observations stay in the snapshot.


@pytest.mark.parametrize('declared', [None, ''])
@pytest.mark.parametrize('federated', [False, True])
def test_legacy_canonical_names_work_with_federated_host_prefix(declared, federated):
    sample = graph('/clock', declared)
    target = '/worker'
    expected = ['/source', '/worker', '/sink']
    if federated:
        sample = merge_snapshots({'robot1': sample})
        target = '/robot1/worker'
        expected = ['/robot1' + n for n in expected]
    assert trace_pipeline_path(sample, target)['nodes'] == expected


@pytest.mark.parametrize('name,declared', [
    ('/clock', 'std_msgs/msg/Float64'),
    ('/parameter_events', 'custom_msgs/msg/Output'),
    ('/custom/clock', None),
    ('/clock', {'unknown': 'future type metadata'}),
])
def test_custom_or_unidentified_streams_are_not_removed_by_similar_names(name, declared):
    path = trace_pipeline_path(graph(name, declared), '/worker')
    assert any(h['topic'] == name for h in path['hops'])


def clock_graph(connected=True):
    sample = graph('/clock', 'rosgraph_msgs/msg/Clock')
    for node in sample['nodes']:
        node['publishers'] = [t for t in node['publishers'] if t != '/clock']
        node['subscribers'] = [t for t in node['subscribers'] if t != '/clock']
    sample['nodes'][0]['publishers'].append('/clock')
    if connected:
        sample['nodes'][3]['subscribers'].append('/clock')
    clock = sample['topics'][2]
    clock.update(publishers=['/source'], subscribers=['/aux'] if connected else [], rate_hz=None)
    return sample


@pytest.mark.parametrize('target', ['/clock', 'clock'])
def test_explicit_clock_query_traverses_clock_instead_of_known_rate_data(target):
    path = trace_pipeline_path(clock_graph(), target)
    assert path['target'] == '/clock' and path['nodes'] == ['/source', '/aux']
    assert [h['topic'] for h in path['hops']] == ['/clock']
    assert path['hops'][0]['rate_hz'] is None


def test_unconnected_explicit_topic_does_not_return_unrelated_application_path():
    sample = clock_graph(connected=False)
    assert trace_pipeline_path(sample, '/clock') is None
    assert trace_pipeline_path(sample, '/worker')['nodes'] == ['/source', '/worker', '/sink']


def test_missing_publisher_query_does_not_infer_a_source_from_another_input():
    sample = clock_graph()
    sample['topics'][2].update(publishers=[], subscribers=['/worker'])
    sample['nodes'][0]['publishers'].remove('/clock')
    sample['nodes'][3]['subscribers'].remove('/clock')
    sample['nodes'][1]['subscribers'].append('/clock')
    assert trace_pipeline_path(sample, '/clock') is None
    assert trace_pipeline_path(sample, '/worker')['nodes'] == ['/source', '/worker', '/sink']


def test_control_only_node_has_no_default_data_path_but_explicit_topic_is_available():
    sample = clock_graph()
    assert trace_pipeline_path(sample, '/aux') is None
    assert trace_pipeline_path(sample, '/clock') is not None


def test_timeline_and_report_keep_application_metrics_and_control_faults():
    snapshots = [graph() for _ in range(3)]
    for index, sample in enumerate(snapshots):
        sample['timestamp'] += index
        sample['topics'][1]['rate_hz'] = [5., 3., 6.][index]
    timeline = pipeline_timeline(snapshots, '/worker')
    assert timeline['nodes'] == ['/source', '/worker', '/sink']
    assert [h['topic'] for h in timeline['hops']] == ['/input', '/output']
    assert timeline['hops'][1]['rate_hz'] == [5., 3., 6.]
    assert timeline['hops'][0]['cb_source'] == ['trace'] * 3
    summary = build_report(make_header(10, 1), snapshots, path_focus='/worker')
    assert summary['pipeline']['nodes'] == timeline['nodes']
    markdown = render_markdown(summary)
    assert 'retained evidence' in markdown and '/parameter_events' in markdown
    section = markdown.split('## Pipeline path over time')[1].split('\n## ')[0]
    assert '/parameter_events' not in section
