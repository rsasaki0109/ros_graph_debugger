"""Keep real DDS subscriptions busy while the agent is stopped repeatedly."""
import argparse
import json
from pathlib import Path
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import Float64


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph-out', required=True, type=Path)
    args = parser.parse_args()
    rclpy.init()
    node = Node('agent_shutdown_publisher')
    publisher = node.create_publisher(Float64, '/integration/shutdown_stream', 10)
    counter = [0]

    def publish():
        counter[0] += 1
        publisher.publish(Float64(data=float(counter[0])))

    def graph():
        endpoints = node.get_subscriptions_info_by_topic('/integration/shutdown_stream')
        subscribers = [e.node_namespace.rstrip('/') + '/' + e.node_name for e in endpoints]
        data = {'monotonic_time': time.monotonic(), 'subscriber_nodes': subscribers}
        temporary = args.graph_out.with_suffix('.tmp')
        temporary.write_text(json.dumps(data) + '\n')
        temporary.replace(args.graph_out)

    node.create_timer(.01, publish)
    node.create_timer(.1, graph)
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
