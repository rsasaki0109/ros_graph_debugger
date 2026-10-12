"""Even a full artifact disk must not strand benchmark child groups."""
import importlib.util
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest


def test_failed_diagnostics_reap_real_child_groups(tmp_path, monkeypatch):
    path = Path(__file__).parent / 'benchmarks/run_image_observer.py'
    spec = importlib.util.spec_from_file_location('image_benchmark_cleanup', path)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    start = benchmark.Processes.start
    children = []
    def idle_worker(owner, name, command):
        child = start(owner, name, [sys.executable, '-c', 'import signal; signal.pause()'])
        children.append((name, child))
        return child
    def unavailable_disk(*args):
        raise OSError('injected diagnostic write failure')
    monkeypatch.setattr(benchmark.Processes, 'start', idle_worker)
    monkeypatch.setattr(benchmark, 'save', unavailable_disk)
    args = SimpleNamespace(output=tmp_path, bag=tmp_path/'unused-bag',
                           port=3963, reliability='best_effort')
    try:
        with pytest.raises(OSError, match='injected diagnostic write failure'):
            benchmark.case(tmp_path/'case', 'baseline', args, [])
        assert len(children) == 2
        assert all(child.poll() is not None for _, child in children)
        groups = benchmark.group_stats({name: child.pid for name, child in children})
        assert all(not value['pids'] for value in groups.values())
    finally:
        # Keep the regression test itself safe when run against a broken runner.
        for _, child in children:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
