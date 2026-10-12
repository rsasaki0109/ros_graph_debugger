"""Nonblocking process measurements with a baseline per process lifetime.

The caller supplies psutil.Process so this module adds no required dependency.
CPU is a process total (one core = 100%), shared by every node on that PID.
"""


class ProcessMetricsSampler:
    def __init__(self, process_factory):
        self._factory = process_factory
        self._processes = {}

    def clear(self):
        self._processes.clear()

    @staticmethod
    def _alive(proc):
        # is_running alone is true for an unreaped zombie on Linux.
        return proc.is_running() and proc.status() not in ('zombie', 'dead')

    def sample(self, pids):
        """Return available metrics; first CPU sample is unknown, not zero.

        psutil's is_running checks PID plus creation identity, including PID
        reuse. If an old identity disappears, skip this poll and warm a fresh
        handle on the next. Failed inspection also invalidates its baseline.
        """
        wanted = set(pids)
        self._processes = {pid: proc for pid, proc in self._processes.items()
                           if pid in wanted}
        result = {}
        for pid in wanted:
            proc = self._processes.get(pid)
            first = proc is None
            try:
                if first:
                    proc = self._factory(pid)
                if not self._alive(proc):
                    self._processes.pop(pid, None)
                    continue
                cpu = proc.cpu_percent(interval=None)
                rss = proc.memory_info().rss
                name = proc.name()
                # Do not publish a sample if the process exited during reads.
                if not self._alive(proc):
                    self._processes.pop(pid, None)
                    continue
            except Exception:
                # Process exit, reuse or denied access is missing evidence.
                self._processes.pop(pid, None)
                continue
            self._processes[pid] = proc
            result[pid] = {'cpu_percent': None if first else cpu,
                           'rss_bytes': rss, 'process_name': name}
        return result
