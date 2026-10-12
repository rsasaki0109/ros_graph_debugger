"""Tier C turn-key: `ros2 trace` session -> callback-duration rows.

One synthetic session drives both layers: the pure owner resolution in
`tracing.py` (always runs), and — when the babeltrace2 Python bindings are
importable — a real CTF trace written by babeltrace2's own `sink.ctf.fs` and
read back through `trace_convert` / `rgd trace convert`.
"""

import json
import random

import pytest

from ros_graph_debugger.cli import main
from ros_graph_debugger.tracing import (
    callbacks_from_trace_file,
    resolve_callback_owners,
    trace_events_to_rows,
)

MS = 1_000_000

# Two processes deliberately reuse the same pointer values: handles are only
# unique per process, so attribution must key on (pid, handle).
SESSION = [
    # pid 100: /perception/detector — subscription, timer and service callbacks
    ('ros2:rcl_node_init', 100, 1, {'node_handle': 0xA1, 'rmw_handle': 0x1,
                                    'node_name': 'detector',
                                    'namespace': '/perception'}),
    ('ros2:rcl_subscription_init', 100, 2, {'subscription_handle': 0xB1,
                                            'node_handle': 0xA1,
                                            'rmw_subscription_handle': 0x2,
                                            'topic_name': '/image_raw',
                                            'queue_depth': 10}),
    ('ros2:rclcpp_subscription_init', 100, 3, {'subscription_handle': 0xB1,
                                               'subscription': 0xD1}),
    ('ros2:rclcpp_subscription_callback_added', 100, 4, {'subscription': 0xD1,
                                                         'callback': 0xC1}),
    ('ros2:rcl_timer_init', 100, 5, {'timer_handle': 0xE1, 'period': 100 * MS}),
    ('ros2:rclcpp_timer_link_node', 100, 6, {'timer_handle': 0xE1,
                                             'node_handle': 0xA1}),
    ('ros2:rclcpp_timer_callback_added', 100, 7, {'timer_handle': 0xE1,
                                                  'callback': 0xC2}),
    ('ros2:rcl_service_init', 100, 8, {'service_handle': 0xF1,
                                       'node_handle': 0xA1,
                                       'rmw_service_handle': 0x3,
                                       'service_name': '/perception/detector/reset'}),
    ('ros2:rclcpp_service_callback_added', 100, 9, {'service_handle': 0xF1,
                                                    'callback': 0xC3}),
    # pid 200: /tracker — same pointer values, different node and topic
    ('ros2:rcl_node_init', 200, 10, {'node_handle': 0xA1, 'rmw_handle': 0x1,
                                     'node_name': 'tracker', 'namespace': '/'}),
    ('ros2:rcl_subscription_init', 200, 11, {'subscription_handle': 0xB1,
                                             'node_handle': 0xA1,
                                             'rmw_subscription_handle': 0x2,
                                             'topic_name': '/objects',
                                             'queue_depth': 10}),
    ('ros2:rclcpp_subscription_init', 200, 12, {'subscription_handle': 0xB1,
                                                'subscription': 0xD1}),
    ('ros2:rclcpp_subscription_callback_added', 200, 13, {'subscription': 0xD1,
                                                          'callback': 0xC1}),
    # invocations
    ('ros2:callback_start', 100, 100 * MS, {'callback': 0xC1, 'is_intra_process': 0}),
    ('ros2:callback_end', 100, 120 * MS, {'callback': 0xC1}),
    ('ros2:callback_start', 200, 121 * MS, {'callback': 0xC1, 'is_intra_process': 0}),
    ('ros2:callback_end', 200, 125 * MS, {'callback': 0xC1}),
    ('ros2:callback_start', 100, 130 * MS, {'callback': 0xC2, 'is_intra_process': 0}),
    ('ros2:callback_end', 100, 131 * MS, {'callback': 0xC2}),
    ('ros2:callback_start', 100, 140 * MS, {'callback': 0xC3, 'is_intra_process': 0}),
    ('ros2:callback_end', 100, 145 * MS, {'callback': 0xC3}),
    ('ros2:callback_start', 100, 200 * MS, {'callback': 0xC1, 'is_intra_process': 0}),
    ('ros2:callback_end', 100, 230 * MS, {'callback': 0xC1}),
    # a callback registered before the trace started: no owner chain
    ('ros2:callback_start', 100, 240 * MS, {'callback': 0xC9, 'is_intra_process': 0}),
    ('ros2:callback_end', 100, 242 * MS, {'callback': 0xC9}),
]

EXPECTED_ROWS = [
    {'node': '/perception/detector', 'callback': 'sub /image_raw',
     'topic': '/image_raw', 'duration_ms': 20.0},
    {'node': '/tracker', 'callback': 'sub /objects', 'topic': '/objects',
     'duration_ms': 4.0},
    {'node': '/perception/detector', 'callback': 'timer 100 ms', 'topic': '',
     'duration_ms': 1.0},
    {'node': '/perception/detector', 'callback': 'srv /perception/detector/reset',
     'topic': '', 'duration_ms': 5.0},
    {'node': '/perception/detector', 'callback': 'sub /image_raw',
     'topic': '/image_raw', 'duration_ms': 30.0},
]
EXPECTED_ROWS = [dict(row, source='trace') for row in EXPECTED_ROWS]


def _events():
    return [{'name': n, 'pid': pid, 't_ns': t, 'fields': dict(f)}
            for n, pid, t, f in SESSION]


# --------------------------------------------------------------------------- #
# Pure layer
# --------------------------------------------------------------------------- #
def test_owners_follow_each_registration_chain_per_process():
    owners = resolve_callback_owners(_events())
    assert owners == {
        (100, 0xC1): {'node': '/perception/detector', 'topic': '/image_raw',
                      'callback': 'sub /image_raw'},
        (100, 0xC2): {'node': '/perception/detector', 'topic': '',
                      'callback': 'timer 100 ms'},
        (100, 0xC3): {'node': '/perception/detector', 'topic': '',
                      'callback': 'srv /perception/detector/reset'},
        (200, 0xC1): {'node': '/tracker', 'topic': '/objects',
                      'callback': 'sub /objects'},
    }


def test_broken_chain_is_not_attributed():
    events = [e for e in _events()
              if e['name'] != 'ros2:rclcpp_subscription_init' or e['pid'] != 200]
    assert (200, 0xC1) not in resolve_callback_owners(events)


def test_events_to_rows_pairs_attributes_and_counts():
    shuffled = _events()
    random.Random(0).shuffle(shuffled)  # order must not matter
    rows, counts = trace_events_to_rows(shuffled)
    assert rows == EXPECTED_ROWS
    assert counts == {'events': len(SESSION), 'callbacks_resolved': 4,
                      'invocations': 6, 'attributed': 5, 'unattributed': 1}


def test_trace_file_accepts_a_session_directory(tmp_path, monkeypatch):
    import ros_graph_debugger.trace_convert as tc
    monkeypatch.setattr(tc, 'read_ctf_events', lambda path: iter(_events()))
    stats = {(c.node, c.callback): c for c in callbacks_from_trace_file(str(tmp_path))}
    det = stats[('/perception/detector', 'sub /image_raw')]
    assert (det.count, det.mean_ms, det.max_ms) == (2, 25.0, 30.0)


def test_cli_without_bt2_explains_how_to_install(tmp_path, monkeypatch, capsys):
    import sys
    monkeypatch.setitem(sys.modules, 'bt2', None)  # import bt2 -> ImportError
    rc = main(['trace', 'convert', str(tmp_path), '--out', str(tmp_path / 'o.ndjson')])
    assert rc == 2
    assert 'python3-bt2' in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Real CTF, written and read by babeltrace2
# --------------------------------------------------------------------------- #
def _bt2_or_skip():
    # Not pytest.importorskip: a bt2 built for another Python raises a plain
    # ImportError, which newer pytest no longer turns into a skip.
    try:
        import bt2
    except ImportError:
        pytest.skip('babeltrace2 Python bindings (python3-bt2) not available')
    return bt2


def _write_ctf(path):
    """Write SESSION as a CTF trace with babeltrace2's `sink.ctf.fs` — the same
    on-disk format `ros2 trace` records, incl. a `vpid` common context."""
    import gc

    bt2 = _bt2_or_skip()
    by_name = {}
    for name, _, _, fields in SESSION:
        by_name.setdefault(name, fields)

    class _Iter(bt2._UserMessageIterator):
        def __init__(self, config, port):
            tc, sc, ecs = port.user_data
            stream = tc().create_stream(sc)
            pkt = stream.create_packet()
            msgs = [self._create_stream_beginning_message(stream),
                    self._create_packet_beginning_message(pkt, SESSION[0][2])]
            for name, pid, t, fields in SESSION:
                m = self._create_event_message(ecs[name], pkt, t)
                m.event.common_context_field['vpid'] = pid
                for k, v in fields.items():
                    m.event.payload_field[k] = v
                msgs.append(m)
            msgs += [self._create_packet_end_message(pkt, SESSION[-1][2]),
                     self._create_stream_end_message(stream)]
            self._msgs = iter(msgs)

        def __next__(self):
            return next(self._msgs)

    class _Source(bt2._UserSourceComponent, message_iterator_class=_Iter):
        def __init__(self, config, params, obj):
            tc = self._create_trace_class()
            ctx = tc.create_structure_field_class()
            ctx.append_member('vpid', tc.create_signed_integer_field_class(32))
            sc = tc.create_stream_class(
                default_clock_class=self._create_clock_class(frequency=10**9),
                supports_packets=True,
                packets_have_beginning_default_clock_snapshot=True,
                packets_have_end_default_clock_snapshot=True,
                event_common_context_field_class=ctx)
            ecs = {}
            for name, fields in by_name.items():
                payload = tc.create_structure_field_class()
                for k, v in fields.items():
                    payload.append_member(
                        k, tc.create_string_field_class() if isinstance(v, str)
                        else tc.create_unsigned_integer_field_class(64))
                ecs[name] = sc.create_event_class(name=name,
                                                  payload_field_class=payload)
            self._add_output_port('out', (tc, sc, ecs))

    graph = bt2.Graph()
    src = graph.add_component(_Source, 'src')
    sink = graph.add_component(
        bt2.find_plugin('ctf').sink_component_classes['fs'], 'sink',
        params={'path': str(path), 'assume-single-trace': True})
    graph.connect_ports(src.output_ports['out'], sink.input_ports['in'])
    graph.run()
    del graph, src, sink
    gc.collect()  # the CTF sink writes `metadata` when it is finalized


def test_rgd_trace_convert_reads_a_real_ctf_trace(tmp_path, capsys):
    trace = tmp_path / 'session'
    _write_ctf(trace)
    out = tmp_path / 'run.ndjson'
    assert main(['trace', 'convert', str(trace), '--out', str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows == EXPECTED_ROWS
    err = capsys.readouterr().err
    assert 'wrote 5 callback invocations' in err
    assert '1 invocations without a resolvable owner' in err

    # ...and the agent's --trace-file takes the session directory directly.
    stats = {(c.node, c.callback): c for c in callbacks_from_trace_file(str(trace))}
    assert stats[('/tracker', 'sub /objects')].count == 1


def test_rgd_trace_convert_on_a_non_trace_dir_is_an_error(tmp_path, capsys):
    _bt2_or_skip()
    rc = main(['trace', 'convert', str(tmp_path), '--out', str(tmp_path / 'o')])
    assert rc == 2
    assert 'cannot read trace' in capsys.readouterr().err
