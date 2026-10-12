"""A real ROS-clock publisher, kept outside the installed package."""
import time

import rclpy
from rclpy.node import Node
from nav_msgs.msg import Odometry
from rosgraph_msgs.msg import Clock
from std_srvs.srv import SetBool, Trigger


class ClockPublisher(Node):
    def __init__(self):
        super().__init__('clock_fixture', namespace='/integration')
        self.started = False
        self.paused = False
        self.last_clock_ns = None
        self.lag_ns = 20_000_000
        self.origin = time.monotonic_ns()
        self.clock_pub = self.create_publisher(Clock, '/clock', 10)
        self.pose_pub = self.create_publisher(Odometry, '/localization/kinematic_state', 10)
        self.create_service(Trigger, '~/start', self.start)
        self.create_service(SetBool, '~/set_lag', self.set_lag)
        self.create_service(SetBool, '~/pause_clock', self.pause_clock)
        self.create_timer(0.02, self.tick)

    def start(self, request, response):
        self.started = True
        response.success = True
        return response

    def set_lag(self, request, response):
        self.lag_ns = 300_000_000 if request.data else 20_000_000
        response.success = True
        return response

    def pause_clock(self, request, response):
        self.paused = request.data
        response.success = True
        return response

    def tick(self):
        now = 1_600_000_000_000_000_000 + time.monotonic_ns() - self.origin
        if self.started and self.paused:
            now = self.last_clock_ns
        if self.started and not self.paused:
            self.last_clock_ns = now
            clock = Clock()
            clock.clock.sec, clock.clock.nanosec = divmod(now, 1_000_000_000)
            self.clock_pub.publish(clock)
        message = Odometry()
        message.header.stamp.sec, message.header.stamp.nanosec = divmod(now - self.lag_ns, 1_000_000_000)
        message.header.frame_id = 'map'
        message.child_frame_id = 'base_link'
        self.pose_pub.publish(message)


def main():
    rclpy.init()
    node = ClockPublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
