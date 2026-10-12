"""Offline rosbag2 / MCAP analysis: windows of a bag -> a snapshot recording.

The pure core always runs on a synthetic drive. With ROS 2 (as in CI), the
same drive is written as a real bag with rosbag2_py and read back through
`rgd bag analyze`.
"""

import json
import struct

import pytest

from ros_graph_debugger.assertions import evaluate
from ros_graph_debugger.bag_analyze import BagAnalyzer, stamp_from_cdr
from ros_graph_debugger.config import Thresholds
from ros_graph_debugger.model import DiagnosticStatus
from ros_graph_debugger.recording import read_recording
from ros_graph_debugger.report import build_report

T0 = 1_700_000_000.0  # bag clock (epoch seconds)

TYPES = {'/pose': 'geometry_msgs/msg/PoseStamped', '/tf': 'tf2_msgs/msg/TFMessage',
         '/tf_static': 'tf2_msgs/msg/TFMessage', '/map': 'std_msgs/msg/String',
         '/diagnostics': 'diagnostic_msgs/msg/DiagnosticArray'}


def drive():
    """A 10 s drive: /pose 10 Hz (2 Hz between 4 and 7 s, 50 ms old),
    odom->base_link TF at 20 Hz until 6 s, one static TF and one /map at the
    start, a diagnostic at 1 Hz that goes ERROR at 8 s.
    Yields (kind, t, payload)."""
    events = []
    for i in range(100):
        t = i * 0.1
        if 4.0 <= t < 7.0 and i % 5:
            continue
        events.append(('pose', t, None))
    for i in range(120):
        events.append(('tf', i * 0.05, ('odom', 'base_link')))
    events.append(('tf_static', 0.0, ('base_link', 'lidar')))
    events.append(('map', 0.5, None))
    for i in range(10):
        events.append(('diag', i + 0.5, 2 if i >= 8 else 0))
    return sorted(events, key=lambda e: e[1])


def _thresholds():
    th = Thresholds()
    th.expected_min_rate['/pose'] = 8.0
    return th


def _feed(analyzer):
    for kind, t, payload in drive():
        bag_t = T0 + t
        if kind == 'pose':
            analyzer.add_message('/pose', bag_t, 100, stamp=bag_t - 0.05)
        elif kind in ('tf', 'tf_static'):
            topic = '/' + kind
            analyzer.add_message(topic, bag_t, 80)
            analyzer.add_tf(bag_t, *payload, static=kind == 'tf_static')
        elif kind == 'map':
            analyzer.add_message('/map', bag_t, 5000)
        else:
            analyzer.add_message('/diagnostics', bag_t, 60)
            analyzer.add_diagnostic(bag_t, DiagnosticStatus(
                name='lidar driver', level=payload,
                message='no packets' if payload else 'ok'))
    return analyzer


def _kinds(snap):
    return {i['kind'] for i in snap['issues']}


def _assert_drive_findings(snaps):
    """What any analysis of drive() must find, in bag time."""
    at = {round(s['timestamp'] - T0, 1): s for s in snaps}
    pose = {t: next(x for x in s['topics'] if x['name'] == '/pose') for t, s in at.items()}
    assert pose[3.0]['rate_hz'] == pytest.approx(10.0, abs=0.6)
    assert pose[3.0]['header_age_ms'] == pytest.approx(50.0, abs=1.0)
    assert pose[6.0]['rate_hz'] == pytest.approx(2.0, abs=0.6)
    assert 'rate_drop' not in _kinds(at[3.0])
    assert 'rate_drop' in _kinds(at[6.0])
    assert pose[6.0]['rate_measurement_scope'] == 'bag_recording'
    rate = next(i for i in at[6.0]['issues'] if i['kind'] == 'rate_drop')
    assert 'rate measurement scope: bag_recording' in rate['evidence']
    assert 'delivery completeness: unknown' in rate['evidence']
    assert 'tf_stale' not in _kinds(at[5.0])
    assert 'tf_stale' in _kinds(at[9.0])
    assert 'diagnostics_error' not in _kinds(at[7.0])
    assert 'diagnostics_error' in _kinds(at[9.0])
    # one-shot topics are never "stale"
    stale = {i['related_topics'][0] for s in snaps for i in s['issues']
             if i['kind'] == 'topic_stale'}
    assert not stale & {'/tf_static', '/map'}


# --------------------------------------------------------------------------- #
# Pure core
# --------------------------------------------------------------------------- #
def test_stamp_from_cdr_reads_the_header_without_deserializing():
    le = b'\x00\x01\x00\x00' + struct.pack('<iI', 100, 500_000_000) + b'frame'
    be = b'\x00\x00\x00\x00' + struct.pack('>iI', 100, 500_000_000)
    assert stamp_from_cdr(le) == pytest.approx(100.5)
    assert stamp_from_cdr(be) == pytest.approx(100.5)
    assert stamp_from_cdr(b'\x00\x01\x00\x00' + struct.pack('<iI', 0, 0)) is None
    assert stamp_from_cdr(b'\x07\x01' + b'\x00' * 10) is None  # not CDR
    assert stamp_from_cdr(b'\x00\x01') is None


def test_windows_find_the_drives_problems():
    snaps = _feed(BagAnalyzer(TYPES, _thresholds())).snapshots(interval=1.0, window=2.0)
    assert round(snaps[0]['timestamp'] - T0, 1) == 2.0  # first window is full
    assert round(snaps[-1]['timestamp'] - T0, 1) == pytest.approx(9.9, abs=0.1)
    assert all(s['nodes'] == [] for s in snaps)  # a bag records no graph
    _assert_drive_findings(snaps)


def test_bag_snapshots_feed_assert_and_report():
    snaps = _feed(BagAnalyzer(TYPES, _thresholds())).snapshots()
    r = evaluate(snaps, fail_on='never', expect_rates={'/pose': 8},
                 forbid_kinds=['tf_stale'])
    assert not r['passed']
    summary = build_report({'profile': None}, snaps)
    pose = next(t for t in summary['topics'] if t['name'] == '/pose')
    assert pose['rate_min'] == pytest.approx(2.0, abs=0.6)


def test_empty_bag_has_no_snapshots():
    assert BagAnalyzer(TYPES, Thresholds()).snapshots() == []


# --------------------------------------------------------------------------- #
# Real bags through rosbag2_py and the CLI
# --------------------------------------------------------------------------- #
def _write_bag(path, storage_id):
    rosbag2_py = pytest.importorskip('rosbag2_py')
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus as Status
    from geometry_msgs.msg import PoseStamped, TransformStamped
    from rclpy.serialization import serialize_message
    from std_msgs.msg import String
    from tf2_msgs.msg import TFMessage

    writer = rosbag2_py.SequentialWriter()
    writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id=storage_id),
                rosbag2_py.ConverterOptions(input_serialization_format='cdr',
                                            output_serialization_format='cdr'))
    for i, (name, ttype) in enumerate(TYPES.items()):
        try:  # Jazzy added a leading topic id
            meta = rosbag2_py.TopicMetadata(id=i, name=name, type=ttype,
                                            serialization_format='cdr')
        except TypeError:
            meta = rosbag2_py.TopicMetadata(name=name, type=ttype,
                                            serialization_format='cdr')
        writer.create_topic(meta)

    def stamp(msg, t):
        msg.header.stamp.sec = int(t)
        msg.header.stamp.nanosec = int(round((t - int(t)) * 1e9))
        return msg

    for kind, t, payload in drive():
        bag_t = T0 + t
        if kind == 'pose':
            topic, msg = '/pose', stamp(PoseStamped(), bag_t - 0.05)
        elif kind in ('tf', 'tf_static'):
            tr = stamp(TransformStamped(), bag_t)
            tr.header.frame_id, tr.child_frame_id = payload
            topic, msg = '/' + kind, TFMessage(transforms=[tr])
        elif kind == 'map':
            topic, msg = '/map', String(data='x' * 5000)
        else:
            st = Status(name='lidar driver', message='no packets' if payload else 'ok')
            st.level = bytes([payload]) if isinstance(st.level, bytes) else payload
            topic, msg = '/diagnostics', DiagnosticArray(status=[st])
        writer.write(topic, serialize_message(msg), int(round(bag_t * 1e9)))
    close = getattr(writer, 'close', None)
    if close:
        close()
    del writer


@pytest.mark.parametrize('storage_id', ['sqlite3', 'mcap'])
def test_rgd_bag_analyze_on_a_real_bag(tmp_path, capsys, storage_id):
    from ros_graph_debugger.cli import main

    bag = tmp_path / f'drive_{storage_id}'
    try:
        _write_bag(bag, storage_id)
    except RuntimeError as exc:  # storage plugin not installed (MCAP on Humble)
        pytest.skip(f'{storage_id} storage unavailable: {exc}')
    out = tmp_path / 'drive.rgd.json'
    rc = main(['bag', 'analyze', str(bag), '--out', str(out), '--expect', '/pose=8'])
    assert rc == 0, capsys.readouterr().err
    assert 'wrote ' in capsys.readouterr().err
    header, snaps = read_recording(str(out))
    assert header['started'] == pytest.approx(T0, abs=0.01)
    _assert_drive_findings(snaps)
    stamped = next(t for t in snaps[0]['topics'] if t['name'] == '/pose')
    assert json.dumps(stamped)  # JSON-clean


def test_rgd_bag_analyze_errors(tmp_path, capsys):
    from ros_graph_debugger.cli import main
    assert main(['bag', 'analyze', str(tmp_path / 'nope'), '--out',
                 str(tmp_path / 'o'), '--interval', '0']) == 2
    assert main(['bag', 'analyze', str(tmp_path / 'nope'), '--out',
                 str(tmp_path / 'o'), '--expect', 'bad']) == 2


def test_reader_path_with_stubbed_ros(monkeypatch, tmp_path):
    """analyze_bag's reader loop against stub rosbag2_py / rclpy modules:
    real CDR bytes for /pose, TF and diagnostics via a stub deserializer."""
    import sys
    import types
    from types import SimpleNamespace as NS

    from ros_graph_debugger.bag_analyze import analyze_bag

    messages, decoded = [], {}
    for kind, t, payload in drive():
        bag_ns = int(round((T0 + t) * 1e9))
        if kind == 'pose':
            s = T0 + t - 0.05
            raw = b'\x00\x01\x00\x00' + struct.pack('<iI', int(s), int(round((s % 1) * 1e9)))
            messages.append(('/pose', raw + b'\x00' * 88, bag_ns))
            continue
        key = f'{kind}:{t}'.encode()
        if kind in ('tf', 'tf_static'):
            tr = NS(header=NS(frame_id=payload[0]), child_frame_id=payload[1])
            decoded[key] = NS(transforms=[tr])
            messages.append(('/' + kind, key, bag_ns))
        elif kind == 'map':
            messages.append(('/map', b'x' * 5000, bag_ns))
        else:
            decoded[key] = NS(status=[NS(name='lidar driver', level=bytes([payload]),
                                         message='', hardware_id='')])
            messages.append(('/diagnostics', key, bag_ns))

    class Reader:
        def open(self, storage, converter):
            assert storage.storage_id == 'mcap'
            self._it = iter(messages)
            self._next = next(self._it, None)

        def get_all_topics_and_types(self):
            return [NS(name=n, type=t) for n, t in TYPES.items()]

        def has_next(self):
            return self._next is not None

        def read_next(self):
            cur, self._next = self._next, next(self._it, None)
            return cur

    class Msg:
        def __init__(self, first):
            self._first = first

        def get_fields_and_field_types(self):
            return {self._first: 'std_msgs/Header'} if self._first else {'data': 'string'}

    stub = {
        'rosbag2_py': types.SimpleNamespace(
            SequentialReader=Reader, StorageOptions=lambda **kw: NS(**kw),
            ConverterOptions=lambda **kw: NS(**kw)),
        'rclpy': types.ModuleType('rclpy'),
        'rclpy.serialization': types.SimpleNamespace(
            deserialize_message=lambda raw, mt: decoded[raw]),
        'rosidl_runtime_py': types.ModuleType('rosidl_runtime_py'),
        'rosidl_runtime_py.utilities': types.SimpleNamespace(
            get_message=lambda t: Msg('header' if t.endswith('PoseStamped') else None)),
    }
    for name, mod in stub.items():
        monkeypatch.setitem(sys.modules, name, mod)

    bag = tmp_path / 'drive.mcap'
    bag.write_bytes(b'')
    analyzer = analyze_bag(str(bag), _thresholds())
    _assert_drive_findings(analyzer.snapshots())
