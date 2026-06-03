"""A fake Autoware-shaped pipeline to demo ros_graph_debugger.

    camera -> detector -> tracker -> planner -> controller

By default the detector periodically enters a "slow" phase (busy-loops in its
callback), which drops its output rate below the profile expectation. Watch the
graph turn red and an issue appear: "Likely bottleneck: detector".

Run as one command:
    ros2 run ros_graph_debugger demo_pipeline

It launches each stage as its own process (with __node remaps) so the agent can
also attribute CPU usage to the detector.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

CAMERA_TOPIC = '/sensing/camera/image_raw'
OBJECTS_TOPIC = '/perception/object_recognition/objects'
TRACKED_TOPIC = '/perception/tracked_objects'
TRAJ_TOPIC = '/planning/scenario_planning/trajectory'
CMD_TOPIC = '/control/command/control_cmd'


def _busy_wait(seconds: float) -> None:
    end = time.perf_counter() + seconds
    x = 0.0
    while time.perf_counter() < end:
        x += 1.0  # keep the CPU busy without sleeping
    return x


class Camera(Node):
    def __init__(self):
        super().__init__('camera')
        self.pub = self.create_publisher(String, CAMERA_TOPIC, 10)
        self.payload = 'x' * 2048  # ~2 KB "frame"
        self.create_timer(1.0 / 30.0, self._tick)  # 30 Hz

    def _tick(self):
        self.pub.publish(String(data=self.payload))


class Detector(Node):
    def __init__(self, mode: str):
        super().__init__('detector')
        self.mode = mode  # 'toggle' | 'slow' | 'healthy'
        self.sub = self.create_subscription(String, CAMERA_TOPIC, self._on_img, 10)
        self.pub = self.create_publisher(String, OBJECTS_TOPIC, 10)
        self.create_timer(0.1, self._tick)  # nominally 10 Hz
        self.last_img = None
        self.t0 = time.monotonic()

    def _on_img(self, msg):
        self.last_img = msg

    def _slow_now(self) -> bool:
        if self.mode == 'slow':
            return True
        if self.mode == 'healthy':
            return False
        # toggle: 12 s healthy, 12 s slow
        return int((time.monotonic() - self.t0) // 12) % 2 == 1

    def _tick(self):
        if self._slow_now():
            _busy_wait(0.22)  # heavy "inference": drops effective rate to ~4 Hz
        self.pub.publish(String(data='objects'))


class Relay(Node):
    """A simple stage that consumes one topic and publishes another on a timer."""

    def __init__(self, name, in_topic, out_topic, rate_hz):
        super().__init__(name)
        self.create_subscription(String, in_topic, lambda m: None, 10)
        self.pub = self.create_publisher(String, out_topic, 10)
        self.create_timer(1.0 / rate_hz, lambda: self.pub.publish(String(data=name)))


STAGES = {
    'camera': lambda mode: Camera(),
    'detector': lambda mode: Detector(mode),
    'tracker': lambda mode: Relay('tracker', OBJECTS_TOPIC, TRACKED_TOPIC, 10),
    'planner': lambda mode: Relay('planner', TRACKED_TOPIC, TRAJ_TOPIC, 10),
    'controller': lambda mode: Relay('controller', TRAJ_TOPIC, CMD_TOPIC, 30),
}


def _run_stage(stage: str, mode: str) -> None:
    rclpy.init()
    node = STAGES[stage](mode)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def _orchestrate(mode: str) -> None:
    procs = []
    print('Starting demo pipeline: camera -> detector -> tracker -> planner -> controller')
    print(f'detector mode: {mode}  (Ctrl-C to stop)\n')
    for stage in STAGES:
        cmd = [sys.executable, '-m', 'ros_graph_debugger.examples.demo_pipeline',
               '--stage', stage, '--mode', mode,
               '--ros-args', '-r', f'__node:={stage}']
        procs.append(subprocess.Popen(cmd))
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


def main(argv=None) -> None:
    # ros2 run passes through args after the executable; strip ROS args we don't own.
    parser = argparse.ArgumentParser(description='ros_graph_debugger demo pipeline')
    parser.add_argument('--stage', choices=list(STAGES.keys()), default=None)
    parser.add_argument('--mode', choices=['toggle', 'slow', 'healthy'],
                        default='toggle',
                        help='detector behaviour (default: toggle every 12s)')
    parser.add_argument('--slow', action='store_true', help='alias for --mode slow')
    parser.add_argument('--healthy', action='store_true', help='alias for --mode healthy')
    args, _ = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    mode = args.mode
    if args.slow:
        mode = 'slow'
    if args.healthy:
        mode = 'healthy'

    if args.stage:
        _run_stage(args.stage, mode)
    else:
        _orchestrate(mode)


if __name__ == '__main__':
    main()
