"""Real ROS fixtures, each launched in its own process by run_scenario.py."""

import argparse

import rclpy
from rclpy.action import ActionClient
from rclpy.duration import Duration
from rclpy.lifecycle import LifecycleNode
from rclpy.node import Node
from rclpy.qos import QoSProfile
from std_msgs.msg import Float64, String
from std_srvs.srv import Trigger
from tf2_msgs.action import LookupTransform


def make_node(role):
    if role == 'lifecycle':
        node = LifecycleNode('managed', namespace='/integration')
        node.output = node.create_lifecycle_publisher(
            String, '/integration/managed/output', 10)
        node.trigger_configure()
        return node
    namespace = '/control/integration' if role == 'processing' else '/integration'
    node = Node(role, namespace=namespace)
    if role == 'listener':
        node.subscription = node.create_subscription(
            String, '/integration/managed/output', lambda _: None, 10)
    elif role == 'clients':
        node.client = node.create_client(Trigger, '/integration/missing_service')
        node.action = ActionClient(node, LookupTransform, '/integration/missing_action')
    elif role == 'processing':
        # A control-stage debug topic selects Autoware's 15 ms stage budget.
        node.output = node.create_publisher(Float64, '~/debug/processing_time_ms', 10)
        node.timer = node.create_timer(
            0.05, lambda: node.output.publish(Float64(data=120.0)))
    elif role == 'deadline_pub':
        qos = QoSProfile(depth=10, deadline=Duration(nanoseconds=200_000_000))
        node.output = node.create_publisher(String, '/integration/deadline', qos)
        node.timer = node.create_timer(
            0.05, lambda: node.output.publish(String(data='deadline fixture')))
    elif role == 'deadline_sub':
        qos = QoSProfile(depth=10, deadline=Duration(nanoseconds=100_000_000))
        node.subscription = node.create_subscription(
            String, '/integration/deadline', lambda _: None, qos)
    return node


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('role', choices=['lifecycle', 'listener', 'clients',
                                        'processing', 'deadline_pub', 'deadline_sub'])
    role = parser.parse_args().role
    rclpy.init()
    node = make_node(role)
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
