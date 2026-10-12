"""CPU samples follow process lifetime, not each graph poll or each ROS node."""
from types import SimpleNamespace

from ros_graph_debugger.process_metrics import ProcessMetricsSampler


class Process:
    def __init__(self, cpu=(0, 42, 73)):
        self.cpu = iter(cpu)
        self.alive = True
        self.cpu_calls = 0
        self.denied = False
        self.state = 'running'

    def is_running(self):
        return self.alive

    def status(self):
        return self.state

    def cpu_percent(self, interval):
        assert interval is None
        self.cpu_calls += 1
        return next(self.cpu)

    def memory_info(self):
        if self.denied:
            raise PermissionError('process inspection denied')
        return SimpleNamespace(rss=1234)

    def name(self):
        return 'worker'


def test_first_cpu_sample_unknown_then_reuses_nonblocking_baseline():
    process = Process()
    created = []
    def factory(pid):
        created.append(pid)
        return process
    sampler = ProcessMetricsSampler(factory)
    first = sampler.sample([17])[17]
    assert first == {'cpu_percent': None, 'rss_bytes': 1234, 'process_name': 'worker'}
    assert sampler.sample([17])[17]['cpu_percent'] == 42
    assert sampler.sample([17])[17]['cpu_percent'] == 73
    assert created == [17]


def test_nodes_sharing_a_pid_receive_the_same_single_sample():
    process = Process()
    sampler = ProcessMetricsSampler(lambda _: process)
    sampler.sample([17, 17])
    result = sampler.sample([17, 17])
    assert result[17]['cpu_percent'] == 42
    assert process.cpu_calls == 2


def test_pid_reuse_discards_old_cpu_and_warms_new_identity():
    old, new = Process(), Process(cpu=(0, 88))
    generations = iter([old, new])
    sampler = ProcessMetricsSampler(lambda _: next(generations))
    sampler.sample([17])
    assert sampler.sample([17])[17]['cpu_percent'] == 42
    old.alive = False  # psutil is_running rejects an exited/reused identity.
    assert sampler.sample([17]) == {}
    assert sampler.sample([17])[17]['cpu_percent'] is None
    assert sampler.sample([17])[17]['cpu_percent'] == 88


def test_unmapped_pid_is_pruned_before_it_reappears():
    processes = iter([Process(), Process(cpu=(0, 9))])
    sampler = ProcessMetricsSampler(lambda _: next(processes))
    sampler.sample([17])
    assert sampler.sample([]) == {}
    assert sampler.sample([17])[17]['cpu_percent'] is None
    assert sampler.sample([17])[17]['cpu_percent'] == 9


def test_failed_read_discards_baseline_instead_of_retaining_previous_cpu():
    old, new = Process(), Process(cpu=(0, 19))
    processes = iter([old, new])
    sampler = ProcessMetricsSampler(lambda _: next(processes))
    sampler.sample([17])
    assert sampler.sample([17])[17]['cpu_percent'] == 42
    old.denied = True
    assert sampler.sample([17]) == {}
    assert sampler.sample([17])[17]['cpu_percent'] is None
    assert sampler.sample([17])[17]['cpu_percent'] == 19


def test_process_exiting_during_read_does_not_publish_last_cpu():
    process = Process()
    states = iter([True, False])
    process.is_running = lambda: next(states)
    sampler = ProcessMetricsSampler(lambda _: process)
    assert sampler.sample([17]) == {}


def test_zombie_process_is_missing_even_while_pid_exists():
    process = Process()
    sampler = ProcessMetricsSampler(lambda _: process)
    sampler.sample([17])
    process.state = 'zombie'
    assert sampler.sample([17]) == {}


def test_factory_failure_does_not_drop_other_process_measurements():
    process = Process()
    def factory(pid):
        if pid == 17:
            raise ProcessLookupError(pid)
        return process
    sampler = ProcessMetricsSampler(factory)
    assert sampler.sample([17, 18])[18]['cpu_percent'] is None
    assert sampler.sample([17, 18])[18]['cpu_percent'] == 42
