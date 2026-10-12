"""Test-only Nav2 map subscriber and bag heartbeat, in a separate process."""

import argparse
import json
from pathlib import Path

import rclpy
from nav_msgs.msg import OccupancyGrid
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Float64


def map_fields(message):
    origin = message.info.origin
    return {'frame_id': message.header.frame_id, 'width': message.info.width,
            'height': message.info.height, 'resolution': message.info.resolution,
            'origin': [origin.position.x, origin.position.y, origin.position.z],
            'orientation': [origin.orientation.x, origin.orientation.y,
                            origin.orientation.z, origin.orientation.w],
            'data': list(message.data)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', required=True, type=Path)
    args = parser.parse_args()
    rclpy.init()
    node = Node('nav2_map_observer')
    count = 0

    def received(message):
        nonlocal count
        count += 1
        result = {'count': count, 'map': map_fields(message)}
        temporary = args.receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, indent=2))
        temporary.replace(args.receipt)

    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)
    node.create_subscription(OccupancyGrid, '/map', received, qos)
    heartbeat = node.create_publisher(Float64, '/integration/nav2/heartbeat', 10)
    node.create_timer(0.1, lambda: heartbeat.publish(Float64(data=1.0)))
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
