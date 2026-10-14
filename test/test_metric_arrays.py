"""Inspection must preserve observations without inventing measurement facts."""

import builtins
from copy import deepcopy
import json

import pytest

from ros_graph_debugger.cli import main
from ros_graph_debugger.metric_arrays import inspect_arrays, read_dump


def metric(value='2.075000', name='processing_time/lateral', unit='millisecond'):
    return {'name': name, 'unit': unit, 'value': value}


def message(*metrics, sec=10):
    return {'stamp': {'sec': sec, 'nanosec': 123}, 'metric_array': list(metrics)}


def test_republication_is_not_unique_source_measurements_or_freshness():
    docs = [message(metric(), sec=i) for i in range(100)]
    original = deepcopy(docs)
    result = inspect_arrays(docs, '/system/processing_time_checker/metrics')
    assert result['counts'] == {'messages': 100, 'entries': 100,
                               'valid_duration_observations': 100,
                               'unusable_duration_observations': 0}
    assert result['source_measurement_count'] is None
    assert result['source_freshness'] == result['node_ownership'] == 'unknown'
    assert result['topic_context_source'] == 'user_supplied'
    assert 'callbacks' not in result and 'issues' not in result
    assert 'verdict' not in result and 'p95_ms' not in result
    assert [d['array_stamp']['sec'] for d in result['messages']] == list(range(100))
    for d in result['messages']:
        row, = d['metrics']
        assert row['node'] is row['source_topic'] is row['measurement_stamp'] is None
        assert row['processing_time_ms'] == 2.075
        assert row['value_text'] == '2.075000'
    assert docs == original


@pytest.mark.parametrize('entry,reason', [
    (metric('NaN'), 'non_finite_value'),
    (metric('inf'), 'non_finite_value'),
    (metric('-inf'), 'non_finite_value'),
    (metric('1e999'), 'non_finite_value'),
    (metric('-1'), 'negative_duration'),
    (metric('-1e-400'), 'negative_duration'),
    (metric('1e-400'), 'underflow_duration'),
    (metric('bad'), 'non_numeric_value'),
    (metric(''), 'non_numeric_value'),
    (metric(True), 'invalid_value_type'),
    (metric(3.0), 'invalid_value_type'),
    (metric('3', unit='second'), 'unsupported_unit'),
    (metric('3', name='distance/lateral'), 'not_processing_time'),
    (metric('3', name=['lateral']), 'invalid_name'),
])
def test_unusable_values_are_preserved_with_reason_not_zero(entry, reason):
    result = inspect_arrays([message(entry)])
    row, = result['messages'][0]['metrics']
    assert row['processing_time_ms'] is None and row['reason'] == reason
    assert row['status'] == 'unusable_duration'
    assert result['counts']['unusable_duration_observations'] == 1
    if isinstance(entry['value'], str):
        assert row['value_text'] == entry['value']
    else:
        assert row['value_text'] is None
    json.dumps(result, allow_nan=False)


def test_duplicates_and_zero_and_large_values_do_not_get_aggregated():
    result = inspect_arrays([message(metric('0'), metric('50'),
                                    metric('1.7976931348623157e308', name='processing_time/total'))])
    rows = result['messages'][0]['metrics']
    assert [r['processing_time_ms'] for r in rows] == [0, 50, 1.7976931348623157e308]
    assert [r['duplicate_name'] for r in rows] == [True, True, False]
    assert [r['entry_index'] for r in rows] == [0, 1, 2]
    assert all(r['status'] == 'valid_duration' for r in rows)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('stamp', [None, {}, {'sec': True, 'nanosec': 0},
                                    {'sec': 0, 'nanosec': 10 ** 9},
                                    {'sec': float('inf'), 'nanosec': 0}])
def test_bad_stamp_does_not_become_a_source_timestamp(stamp):
    result = inspect_arrays([{'stamp': stamp, 'metric_array': [metric()]}])
    msg, = result['messages']
    assert msg['array_stamp'] is None and msg['array_stamp_status'] == 'invalid'
    assert msg['metrics'][0]['measurement_stamp'] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('docs', [[], [None], [{'callbacks': []}],
                                 [{'metric_array': {}}], [{'metric_array': [None]}]])
def test_invalid_structure_fails_instead_of_empty_success(docs):
    with pytest.raises(ValueError):
        inspect_arrays(docs)


def test_empty_array_is_distinct_from_empty_input_and_missing_stamp():
    result = inspect_arrays([{'metric_array': []}])
    assert result['counts']['messages'] == 1 and result['counts']['entries'] == 0
    assert result['messages'][0]['array_stamp_status'] == 'missing'
    assert result['source_freshness'] == 'unknown'


def test_yaml_stream_and_json_array_keep_identical_observations(tmp_path):
    yaml_path = tmp_path / 'echo.txt'
    yaml_path.write_text('stamp: {sec: 10, nanosec: 123}\nmetric_array:\n'
                         '- {name: processing_time/lateral, unit: millisecond, value: "2.075000"}\n'
                         '---\nstamp: {sec: 11, nanosec: 123}\nmetric_array:\n'
                         '- {name: processing_time/lateral, unit: millisecond, value: "2.075000"}\n---\n')
    json_path = tmp_path / 'echo.json'
    expected = [message(metric()), message(metric(), sec=11)]
    json_path.write_text(json.dumps(expected))
    assert read_dump(str(yaml_path)) == read_dump(str(json_path)) == expected
    assert inspect_arrays(read_dump(str(yaml_path))) == inspect_arrays(expected)


@pytest.mark.parametrize('text,fmt', [
    ('{"metric_array":[],"metric_array":[]}', 'json'),
    ('metric_array: []\nmetric_array: []\n', 'yaml'),
    ('metric_array:\n- {name: processing_time/a, unit: millisecond, value: "1", value: "2"}\n', 'yaml'),
    ('metric_array: [', 'yaml'),
    ('', 'auto'),
])
def test_ambiguous_or_unreadable_dump_fails(tmp_path, text, fmt):
    path = tmp_path / 'dump'
    path.write_text(text)
    with pytest.raises(ValueError):
        read_dump(str(path), fmt)


def test_json_cli_is_stdlib_only_and_yaml_missing_dependency_is_actionable(tmp_path, monkeypatch, capsys):
    actual_import = builtins.__import__

    def without_yaml(name, *args, **kwargs):
        if name == 'yaml':
            raise ImportError('optional YAML unavailable')
        return actual_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_yaml)
    path = tmp_path / 'message.json'
    path.write_text(json.dumps(message(metric())))
    assert main(['metrics', 'inspect', str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['counts']['valid_duration_observations'] == 1
    assert main(['metrics', 'inspect', str(path), '--input-format', 'yaml']) == 2
    output = capsys.readouterr()
    assert output.out == '' and 'PyYAML' in output.err


def test_cli_bad_input_does_not_overwrite_existing_output(tmp_path, capsys):
    path, out = tmp_path / 'bad.json', tmp_path / 'existing.json'
    path.write_text('{"callbacks": []}')
    out.write_text('keep')
    assert main(['metrics', 'inspect', str(path), '--out', str(out)]) == 2
    assert out.read_text() == 'keep'
    assert capsys.readouterr().out == ''


def test_cli_unusable_value_is_an_inspection_result_not_health(tmp_path, capsys):
    path, out = tmp_path / 'dump.json', tmp_path / 'out.json'
    path.write_text(json.dumps(message(metric('NaN'))))
    assert main(['metrics', 'inspect', str(path), '--topic', '/metrics', '--out', str(out)]) == 0
    result = json.loads(out.read_text())
    assert result['counts']['unusable_duration_observations'] == 1
    assert result['messages'][0]['metrics'][0]['reason'] == 'non_finite_value'
    assert result['topic_context'] == '/metrics' and 'verdict' not in result
    assert capsys.readouterr().out == ''
