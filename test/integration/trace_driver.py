"""Exercise the C++ fixture from another process over real DDS."""

import sys

import rclpy
from rclpy.node import Node
from std_msgs.msg import Float64
from std_srvs.srv import Trigger


def main():
    mode = sys.argv[1]
    rclpy.init()
    node = Node('trace_driver_' + mode)
    publisher = node.create_publisher(Float64, f'/trace_{mode}/input', 10)
    client = node.create_client(Trigger, f'/trace_{mode}/reset')
    pending = None

    def tick():
        nonlocal pending
        publisher.publish(Float64(data=1.0))
        if client.service_is_ready() and (pending is None or pending.done()):
            if pending is not None:
                assert pending.result().success
            pending = client.call_async(Trigger.Request())

    node.create_timer(0.1, tick)
    print('RGD_DRIVER_READY', flush=True)
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
