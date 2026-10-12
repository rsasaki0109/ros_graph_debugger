"""Separate real ROS node with service-controlled CPU work."""
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import Empty
from std_srvs.srv import SetBool


class Worker(Node):
    def __init__(self):
        super().__init__('cpu_worker', namespace='/integration')
        self.busy = False
        self.output = self.create_publisher(Empty, '~/output', 10)
        self.create_timer(0.1, lambda: self.output.publish(Empty()))
        self.create_service(SetBool, '~/set_busy', self.set_busy)
        self.create_timer(0.01, self.work)

    def set_busy(self, request, response):
        self.busy = request.data
        response.success = True
        return response

    def work(self):
        if self.busy:
            # CPU workload, not a readiness wait. Finite chunks let the service
            # and shutdown interrupt the load without any worker grandchildren.
            deadline = time.monotonic() + 0.04
            while time.monotonic() < deadline:
                sum(range(1000))


def main():
    rclpy.init()
    node = Worker()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
