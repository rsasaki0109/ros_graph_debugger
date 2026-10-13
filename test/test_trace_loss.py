"""Known CTF loss must never become ordinary callback statistics."""
from types import SimpleNamespace

import pytest

from ros_graph_debugger.cli import main
from ros_graph_debugger.trace_convert import TraceDataLoss, convert_trace, read_ctf_events


@pytest.fixture
def ctf_reader(monkeypatch):
    class EventMessage:
        def __init__(self):
            self.event = SimpleNamespace(name='ros2:callback_start',
                common_context_field={'vpid': 42}, payload_field={'callback': 7})
            self.default_clock_snapshot = SimpleNamespace(ns_from_origin=100)

    class DiscardedEvents:
        def __init__(self, count):
            self.count = count

    # Separate types: packet loss must not be mislabeled as event loss.
    class Packets:
        def __init__(self, count):
            self.count = count

    messages = []
    module = SimpleNamespace(_EventMessageConst=EventMessage,
        _DiscardedEventsMessageConst=DiscardedEvents,
        _DiscardedPacketsMessageConst=Packets,
        TraceCollectionMessageIterator=lambda path: iter(messages))
    import ros_graph_debugger.trace_convert as converter
    monkeypatch.setattr(converter, '_import_bt2', lambda: module)
    return messages, EventMessage, {'events': DiscardedEvents, 'packets': Packets}


@pytest.mark.parametrize('unit', ['events', 'packets'])
@pytest.mark.parametrize('count', [17, None])
def test_reported_loss_after_an_event_rejects_conversion(ctf_reader, unit, count):
    messages, Event, types = ctf_reader
    messages.extend([Event(), types[unit](count)])
    events = read_ctf_events('session')
    assert next(events)['pid'] == 42
    with pytest.raises(TraceDataLoss, match=f'discarded {unit} .*{count if count is not None else "unknown"}'):
        next(events)
    with pytest.raises(TraceDataLoss):
        convert_trace('session')


@pytest.mark.parametrize('existing_output', [False, True])
def test_cli_does_not_publish_or_overwrite_partial_results(ctf_reader, tmp_path, capsys, existing_output):
    messages, Event, types = ctf_reader
    messages.extend([Event(), types['events'](9)])
    output = tmp_path / 'callbacks.ndjson'
    original = b'previous capture: preserve exactly\n'
    if existing_output:
        output.write_bytes(original)
    assert main(['trace', 'convert', 'session', '--out', str(output)]) == 2
    assert 'discarded events (count: 9)' in capsys.readouterr().err
    if existing_output:
        assert output.read_bytes() == original
    else:
        assert not output.exists()
