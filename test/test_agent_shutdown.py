"""A collector still using its node must finish before node/context teardown."""
import threading

import pytest
import uvicorn

from ros_graph_debugger import agent


@pytest.mark.parametrize('server_fails', [False, True])
def test_agent_waits_for_collector_before_teardown(monkeypatch, server_fails):
    started = threading.Event()
    finish = threading.Event()
    progress = threading.Event()
    destroyed = threading.Event()
    context_stopped = threading.Event()
    worker_finished = threading.Event()
    real_thread = threading.Thread
    workers = []
    failures = []

    class CollectorThread(real_thread):
        def join(self, *args, **kwargs):
            # Wake the test when cleanup waits, while this worker still owns
            # the node. The old path wakes it from destroy_node instead.
            progress.set()
            return super().join(*args, **kwargs)

    def make_thread(*args, **kwargs):
        worker = CollectorThread(*args, **kwargs)
        workers.append(worker)
        return worker

    class Node:
        def __init__(self, *args, **kwargs):
            pass

        def destroy_node(self):
            destroyed.set()
            progress.set()

    class Executor:
        def add_node(self, node):
            pass

        def spin(self):
            started.set()
            if not finish.wait(timeout=5):
                failures.append(AssertionError('test collector was not released'))
            worker_finished.set()

        def shutdown(self):
            # Shutdown requests exit but does not mean the spin loop exited.
            pass

    def serve(*args, **kwargs):
        assert started.wait(timeout=5)
        if server_fails:
            raise RuntimeError('test server failure')

    args = agent._parse_args(['--no-browser'])
    monkeypatch.setattr(agent, '_parse_args', lambda *_: args)
    monkeypatch.setattr(agent, 'DebuggerNode', Node)
    monkeypatch.setattr(agent, 'ProbeExecutor', Executor)
    monkeypatch.setattr(agent, 'create_app', lambda *args, **kwargs: object())
    monkeypatch.setattr(agent.threading, 'Thread', make_thread)
    monkeypatch.setattr(agent.rclpy, 'init', lambda **kwargs: None)
    monkeypatch.setattr(agent.rclpy, 'ok', lambda: True)
    monkeypatch.setattr(agent.rclpy, 'shutdown', context_stopped.set)
    monkeypatch.setattr(uvicorn, 'run', serve)

    def run():
        try:
            agent.main()
        except Exception as exc:
            failures.append(exc)

    caller = real_thread(target=run)
    caller.start()
    try:
        assert progress.wait(timeout=5), 'cleanup made no progress'
        assert not destroyed.is_set(), 'node destroyed while collector was running'
        assert not context_stopped.is_set(), 'ROS context stopped before collector'
    finally:
        finish.set()
        caller.join(timeout=5)
        for worker in workers:
            real_thread.join(worker, timeout=5)

    assert not caller.is_alive() and all(not worker.is_alive() for worker in workers)
    assert worker_finished.is_set() and destroyed.is_set() and context_stopped.is_set()
    assert [str(exc) for exc in failures] == (['test server failure'] if server_fails else [])
