"""Replay a recording through the live web UI — no ROS required.

`ReplayStore` is a drop-in for `RuntimeGraphStore` from the web server's point
of view: it exposes `.snapshot()` returning the current recorded frame. A
background ticker (driven by the server) advances the cursor, so the existing
WebSocket render path animates a recording with zero changes. The cursor can be
paused and seeked for time-scrubbing.

`build_demo_recording()` returns a fully scripted camera->detector->...->control
scenario with a transient detector bottleneck, so `rgd serve --demo` shows the
whole product working in any environment (CI, a laptop with no ROS, a GIF
capture) without DDS.
"""

from __future__ import annotations

import threading

from .recording import make_header


class _Frame:
    """Minimal snapshot wrapper so create_app can call .to_dict() uniformly."""

    __slots__ = ('_d',)

    def __init__(self, d: dict):
        self._d = d

    def to_dict(self) -> dict:
        return self._d


_EMPTY = {'timestamp': 0.0, 'profile': None, 'nodes': [], 'topics': [],
          'edges': [], 'tf_edges': [], 'diagnostics': [], 'issues': []}


class ReplayStore:
    def __init__(self, snapshots: list[dict], loop: bool = True):
        self._snaps = snapshots or [_EMPTY]
        self._idx = 0
        self._loop = loop
        self._playing = True
        self._lock = threading.Lock()

    @property
    def total(self) -> int:
        return len(self._snaps)

    def state(self) -> dict:
        with self._lock:
            return {'mode': 'replay', 'index': self._idx, 'total': len(self._snaps),
                    'playing': self._playing, 'loop': self._loop}

    def advance(self) -> None:
        """Move one frame forward (called by the server ticker)."""
        with self._lock:
            if not self._playing:
                return
            nxt = self._idx + 1
            if nxt >= len(self._snaps):
                self._idx = 0 if self._loop else len(self._snaps) - 1
                if not self._loop:
                    self._playing = False
            else:
                self._idx = nxt

    def seek(self, index: int) -> dict:
        with self._lock:
            self._idx = max(0, min(len(self._snaps) - 1, int(index)))
            self._playing = False  # seeking implies manual control
            return {'index': self._idx, 'total': len(self._snaps),
                    'playing': self._playing}

    def set_playing(self, playing: bool) -> dict:
        with self._lock:
            self._playing = bool(playing)
            return {'index': self._idx, 'playing': self._playing}

    def snapshot(self) -> _Frame:
        with self._lock:
            return _Frame(self._snaps[self._idx])


# --------------------------------------------------------------------------- #
# Scripted demo scenario (no ROS).
# --------------------------------------------------------------------------- #
_DEMO_PROFILE = {
    'name': 'autoware',
    'groups': {
        'sensing': {'topic_patterns': ['^/sensing/.*']},
        'perception': {'topic_patterns': ['^/perception/.*']},
        'planning': {'topic_patterns': ['^/planning/.*']},
        'control': {'topic_patterns': ['^/control/.*']},
        'localization': {'topic_patterns': ['^/tf$', '^/localization/.*']},
    },
}

CAM = '/sensing/camera/image_raw'
OBJ = '/perception/object_recognition/objects'
TRK = '/perception/tracked_objects'
TRAJ = '/planning/scenario_planning/trajectory'
CMD = '/control/command/control_cmd'


def _topic(name, ttype, pubs, subs, rate, bw, status, size=None):
    return {'name': name, 'type': ttype, 'publisher_count': len(pubs),
            'subscriber_count': len(subs), 'publishers': pubs, 'subscribers': subs,
            'qos_endpoints': [], 'probed': rate is not None, 'rate_hz': rate,
            'bandwidth_bps': bw, 'avg_msg_size_bytes': size, 'p95_msg_size_bytes': size,
            'last_seen_time': None, 'age_ms': None, 'qos_status': 'ok', 'status': status}


def _node(nid, name, pubs, subs, cpu=None, rss=None, status='ok'):
    return {'id': nid, 'name': name, 'namespace': '/', 'publishers': pubs,
            'subscribers': subs, 'services': [], 'pid': None, 'process_name': None,
            'cpu_percent': cpu, 'rss_bytes': rss,
            'process_mapping_confidence': 'medium' if cpu is not None else 'none',
            'status': status}


def build_demo_recording(frames: int = 40):
    """A scripted pipeline where the detector stalls for a middle window."""
    snapshots = []
    for i in range(frames):
        stalled = 12 <= i < 26  # bottleneck window
        obj_rate = 4.1 if stalled else 10.0
        det_cpu = 96.0 if stalled else 38.0
        obj_status = 'critical' if stalled else 'ok'
        det_status = 'warning' if stalled else 'ok'

        topics = [
            _topic(CAM, 'sensor_msgs/msg/Image', ['/camera'], ['/detector'],
                   30.0, 420_000, 'ok', size=14_000),
            _topic(OBJ, 'autoware_perception_msgs/msg/DetectedObjects',
                   ['/detector'], ['/tracker'], obj_rate, 120_000, obj_status,
                   size=12_000),
            _topic(TRK, 'autoware_perception_msgs/msg/TrackedObjects',
                   ['/tracker'], ['/planner'], 10.0, 90_000, 'ok', size=9_000),
            _topic(TRAJ, 'autoware_planning_msgs/msg/Trajectory',
                   ['/planner'], ['/controller'], 10.0, 40_000, 'ok', size=4_000),
            _topic(CMD, 'autoware_control_msgs/msg/Control',
                   ['/controller'], [], 30.0, 6_000, 'ok', size=200),
        ]
        nodes = [
            _node('/camera', 'camera', [CAM], []),
            _node('/detector', 'detector', [OBJ], [CAM], cpu=det_cpu,
                  rss=1_800_000_000, status=det_status),
            _node('/tracker', 'tracker', [TRK], [OBJ]),
            _node('/planner', 'planner', [TRAJ], [TRK]),
            _node('/controller', 'controller', [CMD], [TRAJ]),
        ]
        tf_edges = [{
            'parent': 'map', 'child': 'base_link', 'last_update_time': None,
            'age_ms': 420.0 if stalled else 18.0, 'static': False,
            'status': 'critical' if stalled else 'ok'}]
        issues = []
        if stalled:
            issues = [{
                'id': f'demo_bn_{i}', 'severity': 'critical', 'kind': 'bottleneck',
                'title': 'Likely bottleneck: detector',
                'explanation': 'detector output /perception/object_recognition/objects '
                               'dropped below expectation while its inputs look healthy '
                               'and it is CPU-bound.',
                'evidence': [f'{OBJ}: {obj_rate:.1f} Hz (expected >= 10.0)',
                             f'detector CPU: {det_cpu:.0f}%',
                             f'{CAM}: 30.0 Hz'],
                'suggested_actions': ["Inspect this node's callback duration",
                                      'Check CPU/GPU utilization of its process'],
                'related_nodes': ['/detector'], 'related_topics': [OBJ],
                'related_frames': []},
                {
                'id': f'demo_tf_{i}', 'severity': 'critical', 'kind': 'tf_stale',
                'title': 'TF map -> base_link is stale',
                'explanation': 'The transform has not been updated recently.',
                'evidence': ['age: 420 ms'],
                'suggested_actions': ['Inspect the broadcaster for this transform'],
                'related_nodes': [], 'related_topics': [], 'related_frames': ['map', 'base_link']}]

        snapshots.append({
            'timestamp': float(i), 'profile': 'autoware', 'nodes': nodes,
            'topics': topics, 'edges': _edges(topics), 'tf_edges': tf_edges,
            'diagnostics': [], 'issues': issues})

    header = make_header(started=0.0, interval=0.5, profile=_DEMO_PROFILE)
    return header, snapshots


def _edges(topics):
    edges = []
    for t in topics:
        for p in t['publishers']:
            for s in t['subscribers']:
                edges.append({'from_node': p, 'to_node': s, 'topic': t['name'],
                              'type': t['type'], 'rate_hz': t['rate_hz'],
                              'bandwidth_bps': t['bandwidth_bps'], 'status': t['status']})
    return edges
