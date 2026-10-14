"""Known CDR1 schemas only; corrupt payloads must not become empty success."""
import builtins
import json
import struct
import sys
from types import SimpleNamespace as NS

import pytest

from ros_graph_debugger.bag_metrics import decode_metric_array, inspect_metric_bag
from ros_graph_debugger.cli import main

TOPIC = '/system/processing_time_checker/metrics'
TYPE = 'tier4_metric_msgs/msg/MetricArray'


def cdr(entries=(), order='<', stamp=(123, 456)):
    body = bytearray(struct.pack(order + 'iII', *stamp, len(entries)))
    for entry in entries:
        for value in entry:
            body.extend(b'\0' * ((-len(body)) % 4))
            data = value.encode('utf-8') + b'\0'
            body.extend(struct.pack(order + 'I', len(data)))
            body.extend(data)
    return (b'\x00\x01\0\0' if order == '<' else b'\x00\x00\0\0') + body


@pytest.mark.parametrize('order', ['<', '>'])
def test_cdr_endianness_alignment_unicode_and_empty_array(order):
    entries = [('processing_time/制御', 'millisecond', '12.5'),
               ('processing_time/lateral', 'millisecond', '-1e-400')]
    result = decode_metric_array(cdr(entries, order))
    assert result == {'stamp': {'sec': 123, 'nanosec': 456}, 'metric_array':
                      [dict(zip(('name', 'unit', 'value'), e)) for e in entries]}
    assert decode_metric_array(cdr(order=order))['metric_array'] == []


@pytest.mark.parametrize('raw', [
    b'', b'\x00\x07\0\0' + b'\0' * 12,
    b'\x00\x01\0\x01' + b'\0' * 12,
    cdr()[:-1], cdr() + b'\0',
    b'\x00\x01\0\0' + struct.pack('<iII', 0, 0, 0xffffffff),
    cdr([('', '', '')])[:-1],
    b'\x00\x01\0\0' + struct.pack('<iIII', 0, 0, 1, 0) + b'\0' * 20,
    b'\x00\x01\0\0' + struct.pack('<iIII', 0, 0, 1, 0xffffffff) + b'\0' * 20,
    cdr([('a', 'b', 'c')])[:-1] + b'x',
    cdr([('a', 'b', 'c')]).replace(b'a\0', b'\xff\0'),
    cdr([('a\0b', 'b', 'c')]),
])
def test_invalid_encoding_lengths_utf8_and_trailing_bytes_are_errors(raw):
    with pytest.raises(ValueError):
        decode_metric_array(raw)


@pytest.mark.parametrize('order', ['<', '>'])
def test_cpp_final_alignment_padding_and_unpadded_python_form(order):
    raw = cdr([('a', 'b', 'c')], order)
    padding = (-(len(raw) - 4)) % 4
    assert padding == 2
    assert decode_metric_array(raw + b'\xa5' * padding) == decode_metric_array(raw)
    with pytest.raises(ValueError, match='trailing'):
        decode_metric_array(raw + b'\0')
    with pytest.raises(ValueError, match='trailing'):
        decode_metric_array(raw + b'\0' * 4)


def fake_reader(monkeypatch, *, entries=None, rows=()):
    class Reader:
        def __init__(self):
            self.rows = iter(rows)
            self.next_row = None

        def open(self, *args):
            pass

        def get_all_topics_and_types(self):
            return entries if entries is not None else [NS(name=TOPIC, type=TYPE, serialization_format='cdr')]

        def set_filter(self, storage_filter):
            assert storage_filter['topics'] == [TOPIC]

        def has_next(self):
            self.next_row = next(self.rows, None)
            return self.next_row is not None

        def read_next(self):
            return self.next_row

    monkeypatch.setitem(sys.modules, 'rosbag2_py', NS(SequentialReader=Reader,
                        StorageOptions=lambda **kwargs: kwargs,
                        StorageFilter=lambda **kwargs: kwargs,
                        ConverterOptions=lambda **kwargs: kwargs))


def test_bag_clocks_and_duplicate_cached_observations_remain_separate(monkeypatch):
    entry = ('processing_time/lateral', 'millisecond', '12.5')
    rows = [('/other', b'not this schema', 0), (TOPIC, cdr([entry, entry]), 10 ** 18 + 1),
            (TOPIC, cdr([entry]), 10 ** 18 + 2)]
    fake_reader(monkeypatch, rows=rows)
    result = inspect_metric_bag('fake.db3', TOPIC)
    assert result['counts']['messages'] == 2 and result['counts']['valid_duration_observations'] == 3
    assert [x['bag_received_timestamp_ns'] for x in result['messages']] == [10 ** 18 + 1, 10 ** 18 + 2]
    assert all(x['array_stamp'] == {'sec': 123, 'nanosec': 456} for x in result['messages'])
    assert all(r['duplicate_name'] for r in result['messages'][0]['metrics'])
    assert result['source_measurement_count'] is None and result['source_freshness'] == 'unknown'
    assert result['topic_context_source'] == 'bag_metadata'
    assert result['bag_source']['delivery_completeness'] == 'unknown'
    assert 'callbacks' not in result and 'verdict' not in result


@pytest.mark.parametrize('metadata', [[],
    [NS(name=TOPIC, type='custom/msg/MetricArray', serialization_format='cdr')],
    [NS(name=TOPIC, type=TYPE, serialization_format='json')],
    [NS(name=TOPIC, type=TYPE, serialization_format='cdr')] * 2,
])
def test_missing_unsupported_or_ambiguous_topic_fails(monkeypatch, metadata):
    fake_reader(monkeypatch, entries=metadata, rows=[(TOPIC, cdr(), 0)])
    with pytest.raises(ValueError):
        inspect_metric_bag('fake.db3', TOPIC)


def test_empty_topic_and_corrupt_later_message_do_not_overwrite_output(monkeypatch, tmp_path, capsys):
    out = tmp_path / 'existing.json'
    out.write_text('keep')
    for rows in [[], [(TOPIC, cdr(), 0), (TOPIC, b'bad', 1)]]:
        fake_reader(monkeypatch, rows=rows)
        assert main(['bag', 'metrics', 'fake.db3', '--topic', TOPIC, '--out', str(out)]) == 2
        assert out.read_text() == 'keep'
        assert capsys.readouterr().out == ''


def test_missing_rosbag_dependency_has_actionable_cli_error(monkeypatch, capsys):
    actual = builtins.__import__

    def without_rosbag(name, *args, **kwargs):
        if name == 'rosbag2_py':
            raise ImportError('not installed')
        return actual(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_rosbag)
    assert main(['bag', 'metrics', 'missing', '--topic', TOPIC]) == 2
    captured = capsys.readouterr()
    assert not captured.out and 'sourced ROS 2' in captured.err


@pytest.mark.parametrize('ttype', [TYPE, 'autoware_internal_metric_msgs/msg/MetricArray'])
def test_real_sqlite_bag_without_custom_message_bindings(tmp_path, capsys, ttype):
    # Mandatory native CI test; host pure runs explicitly exclude this test.
    import rosbag2_py
    path = tmp_path / 'metrics'
    writer = rosbag2_py.SequentialWriter()
    writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id='sqlite3'),
                rosbag2_py.ConverterOptions(input_serialization_format='cdr', output_serialization_format='cdr'))
    try:
        meta = rosbag2_py.TopicMetadata(id=0, name=TOPIC, type=ttype, serialization_format='cdr')
    except TypeError:
        meta = rosbag2_py.TopicMetadata(name=TOPIC, type=ttype, serialization_format='cdr')
    writer.create_topic(meta)
    for index, v in enumerate(['12.5', 'NaN', '-1e-400', '12.5']):
        writer.write(TOPIC, cdr([('processing_time/lateral', 'millisecond', v)]), 10 ** 18 + index)
    close = getattr(writer, 'close', None)
    if close:
        close()
    del writer
    assert main(['bag', 'metrics', str(path), '--topic', TOPIC]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['bag_source']['type'] == ttype
    assert result['counts']['messages'] == 4
    assert result['counts']['valid_duration_observations'] == 2
    assert result['counts']['unusable_duration_observations'] == 2
    assert [r['metrics'][0]['reason'] for r in result['messages']] == [None, 'non_finite_value', 'negative_duration', None]
    assert result['source_measurement_count'] is None
