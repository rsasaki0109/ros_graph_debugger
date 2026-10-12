"""Header age follows ROS time; receipt/rate clocks remain independent."""
import pytest
import rclpy
from rclpy.clock import ClockType
from rclpy.parameter import Parameter
from rclpy.serialization import serialize_message
from rclpy.time import Time
from nav_msgs.msg import Odometry

from ros_graph_debugger.agent import _parse_args, main
from ros_graph_debugger.config import ProbeConfig, Thresholds
from ros_graph_debugger.model import RuntimeGraphStore
from ros_graph_debugger.node import DebuggerNode


@pytest.fixture
def probe_callback(monkeypatch):
    rclpy.init(args=[])
    node = DebuggerNode(RuntimeGraphStore(), ProbeConfig(), Thresholds(), None)
    node.set_parameters([Parameter('use_sim_time', value=True)])
    callbacks = []
    # Metadata dispatch has dedicated tests; retain real serialization/ROSClock.
    monkeypatch.setattr('ros_graph_debugger.node.with_message_info', lambda cb: cb)
    monkeypatch.setattr(node, 'create_subscription',
                        lambda mt, name, cb, qos, **kw: callbacks.append(cb) or object())
    node._start_probe('/clock_case', 'nav_msgs/msg/Odometry')
    try:
        yield node, callbacks[0]
    finally:
        node.destroy_node()
        rclpy.shutdown()


@pytest.mark.parametrize('now_ns,expected_ms', [(100_020_000_000, 20.0),
                                               (100_500_000_000, 500.0)])
def test_probe_age_uses_ros_clock(probe_callback, now_ns, expected_ms):
    node, callback = probe_callback
    node.get_clock().set_ros_time_override(Time(nanoseconds=now_ns,
                                                clock_type=ClockType.ROS_TIME))
    message = Odometry()
    message.header.stamp.sec = 100
    callback(serialize_message(message), None)
    assert node._probes['/clock_case'].metrics()['header_age_p95_ms'] == expected_ms


def test_unstarted_simulation_clock_does_not_report_fresh_data(probe_callback):
    node, callback = probe_callback
    assert node.get_clock().now().nanoseconds == 0
    message = Odometry()
    message.header.stamp.sec = 100
    callback(serialize_message(message), None)
    probe = node._probes['/clock_case']
    assert probe.has_header is True
    assert probe.metrics()['header_age_p95_ms'] is None
    node.get_clock().set_ros_time_override(Time(seconds=100, nanoseconds=20_000_000,
                                                clock_type=ClockType.ROS_TIME))
    callback(serialize_message(message), None)
    assert probe.metrics()['header_age_p95_ms'] == 20.0


def test_collector_timers_progress_independently_of_ros_clock(probe_callback):
    node, callback = probe_callback
    assert node.get_clock().now().nanoseconds == 0
    timers = list(node.timers)
    assert len(timers) == 4
    assert all(timer.clock.clock_type == ClockType.STEADY_TIME for timer in timers)


def test_wall_time_probe_uses_system_ros_clock(probe_callback):
    node, callback = probe_callback
    node.set_parameters([Parameter('use_sim_time', value=False)])
    message = Odometry()
    message.header.stamp.sec = 100
    callback(serialize_message(message), None)
    assert node._probes['/clock_case'].metrics()['header_age_p95_ms'] > 1e9


def test_frozen_ros_clock_does_not_change_arrival_rate(probe_callback):
    node, callback = probe_callback
    node.get_clock().set_ros_time_override(Time(seconds=100, nanoseconds=20_000_000,
                                                clock_type=ClockType.ROS_TIME))
    message = Odometry()
    message.header.stamp.sec = 100
    raw = serialize_message(message)
    for stamp in (1_000_000_000, 1_020_000_000, 1_040_000_000):
        callback(raw, {'received_timestamp': stamp})
    metrics = node._probes['/clock_case'].metrics()
    assert metrics['rate_hz'] == 50.0
    assert metrics['header_age_p95_ms'] == 20.0


def test_agent_accepts_ros_parameters_without_mutating_arguments():
    argv = ['--profile', 'autoware', '--no-browser', '--port', '3962',
            '--ros-args', '-p', 'use_sim_time:=true', '-r', '__node:=clock_agent']
    original = list(argv)
    args = _parse_args(argv)
    assert args.profile == 'autoware' and args.no_browser and args.port == 3962
    assert argv == original


def test_ros_argument_separator_keeps_application_options():
    assert _parse_args(['--ros-args', '-p', 'use_sim_time:=true', '--',
                        '--profile', 'nav2']).profile == 'nav2'


def test_default_command_line_accepts_ros_arguments(monkeypatch):
    monkeypatch.setattr('sys.argv', ['agent', '--no-browser', '--ros-args',
                                    '-p', 'use_sim_time:=true'])
    assert _parse_args().no_browser


def test_unknown_application_arguments_are_still_rejected():
    with pytest.raises(SystemExit) as error:
        _parse_args(['--unknown-application-option'])
    assert error.value.code == 2


@pytest.mark.parametrize('argv', [None, ['--no-browser', '--ros-args', '-p',
                                       'use_sim_time:=true']])
def test_main_forwards_ros_arguments_to_context(monkeypatch, argv):
    class ContextInitReached(Exception):
        pass
    captured = []
    def capture(*, args):
        captured.append(args)
        raise ContextInitReached
    monkeypatch.setattr('sys.argv', ['agent', '--no-browser'])
    monkeypatch.setattr('ros_graph_debugger.agent.rclpy.init', capture)
    with pytest.raises(ContextInitReached):
        main(argv)
    assert captured == [None if argv is None else ['ros_graph_debugger', *argv]]
