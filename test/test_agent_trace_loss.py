"""A requested lossy trace must not silently start an agent without its stats."""
import pytest

from ros_graph_debugger import agent
from ros_graph_debugger.trace_convert import TraceDataLoss


def test_agent_rejects_trace_loss_before_ros_startup(monkeypatch, capsys):
    import ros_graph_debugger.tracing as tracing

    def lossy_trace(path):
        assert path == 'lossy-session'
        raise TraceDataLoss('CTF trace reports discarded events (count: unknown)')

    def forbidden_start(*args, **kwargs):
        pytest.fail('agent initialized ROS after its requested trace was rejected')

    monkeypatch.setattr(tracing, 'callbacks_from_trace_file', lossy_trace)
    monkeypatch.setattr(agent.rclpy, 'init', forbidden_start)
    with pytest.raises(SystemExit) as exc:
        agent.main(['--no-browser', '--trace-file', 'lossy-session'])
    assert exc.value.code == 2
    assert 'discarded events (count: unknown)' in capsys.readouterr().err
