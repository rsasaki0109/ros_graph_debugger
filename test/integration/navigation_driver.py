"""Test-only driver for actual Nav2 lifecycle services and navigation actions."""

from collections import deque
import math
import threading
import time

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, TransformStamped, Twist
from lifecycle_msgs.srv import ChangeState, GetState
from nav2_msgs.action import ComputePathToPose, FollowPath
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from tf2_ros import Buffer, StaticTransformBroadcaster, TransformException, TransformListener


class Driver:
    def __init__(self, poll):
        self.poll = poll
        self.node = Node('navigation_scenario_driver')
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.failure = None
        self.thread = threading.Thread(target=self.spin, daemon=True)
        self.commands = deque(maxlen=4096)
        self.moving_commands = 0
        self.tf = Buffer()
        self.listener = TransformListener(self.tf, self.node)
        self.static = StaticTransformBroadcaster(self.node)
        transform = TransformStamped()
        transform.header.stamp = self.node.get_clock().now().to_msg()
        transform.header.frame_id = 'base_footprint'
        transform.child_frame_id = 'base_scan'
        transform.transform.rotation.w = 1.0
        self.static.sendTransform(transform)
        self.pose_pub = self.node.create_publisher(PoseWithCovarianceStamped, '/initialpose', 10)
        self.plan = ActionClient(self.node, ComputePathToPose, '/compute_path_to_pose')
        self.follow = ActionClient(self.node, FollowPath, '/follow_path')
        self.node.create_subscription(Twist, '/cmd_vel', self.command, 10)
        self.thread.start()

    def spin(self):
        try:
            self.executor.spin()
        except Exception as exc:
            self.failure = exc

    def check(self):
        assert self.thread.is_alive() and self.failure is None, self.failure

    def command(self, message):
        self.commands.append({'time': time.time(), 'linear_x': message.linear.x,
                              'angular_z': message.angular.z})
        if abs(message.linear.x) > 0.01:
            self.moving_commands += 1

    @staticmethod
    def ready(value):
        assert value, 'not ready'
        return value

    def future(self, future):
        self.poll(lambda: self.ready(future.done()), 'ROS response')
        return future.result()

    def transition(self, name, transition, state):
        client = self.node.create_client(ChangeState, name + '/change_state')
        getter = self.node.create_client(GetState, name + '/get_state')
        try:
            self.poll(lambda: self.ready(client.service_is_ready() and getter.service_is_ready()),
                      name + ' lifecycle services')
            request = ChangeState.Request()
            request.transition.id = transition
            assert self.future(client.call_async(request)).success, name
            assert self.future(getter.call_async(GetState.Request())).current_state.id == state, name
        finally:
            self.node.destroy_client(client)
            self.node.destroy_client(getter)

    def pose(self, x):
        pose = PoseStamped()
        pose.header.frame_id = 'map'
        pose.header.stamp = self.node.get_clock().now().to_msg()
        pose.pose.position.x, pose.pose.position.y = x, 1.0
        pose.pose.orientation.w = 1.0
        return pose

    def position(self):
        try:
            transform = self.tf.lookup_transform('map', 'base_footprint', rclpy.time.Time())
        except TransformException as exc:
            raise AssertionError(str(exc)) from exc
        return [transform.transform.translation.x, transform.transform.translation.y]

    def initialize_pose(self):
        self.poll(lambda: self.ready(self.pose_pub.get_subscription_count() > 0),
                  'simulator initialpose subscriber')
        initial = PoseWithCovarianceStamped()
        pose = self.pose(1.0)
        initial.header, initial.pose.pose = pose.header, pose.pose
        self.pose_pub.publish(initial)
        self.poll(self.position, 'initial robot transform')
        assert math.dist(self.position(), [1.0, 1.0]) < 0.01

    def start_goal(self, x):
        self.poll(lambda: self.ready(self.plan.server_is_ready() and self.follow.server_is_ready()),
                  'real Nav2 action servers')
        start = self.position()
        request = ComputePathToPose.Goal()
        request.goal, request.planner_id = self.pose(x), 'GridBased'
        goal = self.future(self.plan.send_goal_async(request))
        assert goal.accepted
        result = self.future(goal.get_result_async())
        assert result.status == GoalStatus.STATUS_SUCCEEDED and result.result.error_code == 0, result
        path = result.result.path
        assert path.header.frame_id == 'map' and len(path.poses) >= 10
        positions = [[p.pose.position.x, p.pose.position.y] for p in path.poses]
        assert all(math.isfinite(v) for point in positions for v in point)
        assert math.dist(positions[0], start) < 0.2, (positions[0], start)
        assert math.dist(positions[-1], [x, 1.0]) < 0.1, positions[-1]
        request = FollowPath.Goal()
        request.path, request.controller_id = path, 'FollowPath'
        request.goal_checker_id, request.progress_checker_id = 'goal_checker', 'progress_checker'
        goal = self.future(self.follow.send_goal_async(request))
        assert goal.accepted
        truth = {'start': start, 'goal': [x, 1.0], 'path': positions,
                 'started': time.time(), 'commands_before': self.moving_commands}
        return goal.get_result_async(), truth

    def finish_goal(self, future, truth, success):
        result = self.future(future)
        truth.update(status=result.status, error_code=result.result.error_code,
                     error_msg=result.result.error_msg, finished=time.time(), final=self.position(),
                     moving_commands=self.moving_commands - truth['commands_before'])
        if success:
            assert result.status == GoalStatus.STATUS_SUCCEEDED and result.result.error_code == 0, truth
            assert math.dist(truth['final'], truth['goal']) <= 0.15, truth
            assert truth['moving_commands'] >= 60, truth
        else:
            assert result.status == GoalStatus.STATUS_ABORTED, truth
            assert result.result.error_code == FollowPath.Result.FAILED_TO_MAKE_PROGRESS, truth
        return truth

    def close(self):
        self.executor.shutdown(timeout_sec=5)
        self.thread.join(timeout=5)
        self.plan.destroy()
        self.follow.destroy()
        self.node.destroy_node()
