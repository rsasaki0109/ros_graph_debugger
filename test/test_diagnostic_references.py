"""Diagnostic task labels must not fabricate graph-node attribution."""

import copy

import pytest

from ros_graph_debugger.analysis import analyze
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.federation import merge_snapshots
from ros_graph_debugger.markdown import snapshot_to_markdown
from ros_graph_debugger.model import DiagnosticStatus, NodeInfo, RuntimeGraphStore
from ros_graph_debugger.recording import make_header
from ros_graph_debugger.report import build_report, render_markdown, render_html


def capture(name='gyro_odometer: gyro_odometer_status', level=2,
            hardware_id='gyro_odometer'):
    store = RuntimeGraphStore()
    store.set_graph({node: NodeInfo(id=node, name='gyro_odometer')
                     for node in ('/gyro_odometer', '/other/gyro_odometer')}, {})
    store.set_diagnostics({name: DiagnosticStatus(name=name, level=level,
                          hardware_id=hardware_id, message='IMU msg is timeout')})
    store.set_issues(analyze(store, Thresholds()))
    return store.snapshot().to_dict()


@pytest.mark.parametrize('level,kind,severity', [
    (1, 'diagnostics_warn', 'warning'), (2, 'diagnostics_error', 'critical'),
    (3, 'diagnostics_error', 'critical'),
])
@pytest.mark.parametrize('name', ['gyro_odometer: gyro_odometer_status',
                                  '/gyro_odometer', 'arbitrary device task'])
def test_label_or_graph_name_coincidence_is_not_node_attribution(name, level, kind, severity):
    sample = capture(name, level)
    issue, = sample['issues']
    assert issue['kind'] == kind and issue['severity'] == severity
    assert issue['explanation'] == 'IMU msg is timeout'
    assert issue['related_diagnostics'] == [sample['diagnostics'][0]['name']]
    assert issue['related_nodes'] == []
    assert f'level: {level}' in issue['evidence']
    assert f'diagnostic name: {name}' in issue['evidence']
    assert 'node attribution: unknown' in issue['evidence']
    if level >= 2:
        assert 'hardware_id: gyro_odometer' in issue['evidence']


def test_hardware_id_matching_a_visible_node_is_not_a_node_identity():
    issue, = capture(hardware_id='/gyro_odometer')['issues']
    assert 'hardware_id: /gyro_odometer' in issue['evidence']
    assert issue['related_nodes'] == []


def test_normal_diagnostic_clears_issue_without_removing_status():
    sample = capture(level=0)
    assert sample['issues'] == []
    assert len(sample['diagnostics']) == 1 and sample['diagnostics'][0]['level'] == 0


def test_markdown_keeps_diagnostic_reference_and_unknown_origin():
    sample = capture()
    original = copy.deepcopy(sample)
    text = snapshot_to_markdown(sample)
    assert '- Diagnostics: gyro_odometer: gyro_odometer_status' in text
    assert 'node attribution: unknown' in text
    assert '- Related: gyro_odometer: gyro_odometer_status' not in text
    assert sample == original


def test_federation_references_matching_host_diagnostic_without_fabricated_node():
    source = capture('/gyro_odometer')
    original = copy.deepcopy(source)
    merged = merge_snapshots([('robot1', source), ('robot2', source)])
    names = {d['name'] for d in merged['diagnostics']}
    assert names == {'robot1//gyro_odometer', 'robot2//gyro_odometer'}
    for issue in merged['issues']:
        assert issue['related_diagnostics'] == [issue['host'] + '//gyro_odometer']
        assert issue['related_diagnostics'][0] in names
        assert issue['related_nodes'] == []
    assert source == original


def test_legacy_references_are_preserved_without_inventing_new_metadata():
    sample = capture()
    label = sample['diagnostics'][0]['name']
    sample['issues'][0].pop('related_diagnostics')
    sample['issues'][0]['related_nodes'] = [label]
    original = copy.deepcopy(sample)
    merged = merge_snapshots([('robot1', sample)])
    assert 'related_diagnostics' not in merged['issues'][0]
    assert merged['issues'][0]['related_nodes'] == ['/robot1/' + label]
    assert sample == original


@pytest.mark.parametrize('level', [1, 2])
def test_report_keeps_diagnostic_evidence_and_separate_task_references(level):
    sample = capture(level=level)
    original = copy.deepcopy(sample)
    summary = build_report(make_header(0, 1), [sample, sample])
    issue, = summary['issue_catalog']
    assert issue['related_diagnostics'] == ['gyro_odometer: gyro_odometer_status']
    assert issue['related_nodes'] == [] and issue['count'] == 2
    assert summary['bottlenecks'] == []  # Diagnostic issues are a distinct section.
    for render in (render_markdown, render_html):
        text = render(summary)
        assert 'Diagnostic issues (representative samples)' in text
        assert 'Diagnostic tasks: gyro_odometer: gyro_odometer_status' in text
        assert 'node attribution: unknown' in text
        assert f'level: {level}' in text
    assert sample == original


def test_report_does_not_infer_a_diagnostic_reference_from_legacy_node_labels():
    sample = capture()
    sample['issues'][0].pop('related_diagnostics')
    sample['issues'][0]['related_nodes'] = ['gyro_odometer: gyro_odometer_status']
    summary = build_report(make_header(0, 1), [sample])
    assert summary['issue_catalog'][0]['related_diagnostics'] is None
    for render in (render_markdown, render_html):
        assert 'Diagnostic tasks: unknown' in render(summary)


def test_report_escapes_diagnostic_labels_and_preserves_unknown_legacy_summary():
    label = '<script>task</script>'
    summary = build_report(make_header(0, 1), [capture(label)])
    text = render_html(summary)
    assert '<script>task</script>' not in text
    assert '&lt;script&gt;task&lt;/script&gt;' in text
    del summary['issue_catalog']
    assert 'Diagnostic issues (representative samples)' not in render_html(summary)
    assert 'Diagnostic issues (representative samples)' not in render_markdown(summary)
