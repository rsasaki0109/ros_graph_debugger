"""Layered node->process attribution with honest confidence levels."""

from ros_graph_debugger.procmap import match_nodes_to_processes


def _p(pid, *argv):
    return {'pid': pid, 'cmdline': list(argv)}


def test_explicit_node_remap_is_high_confidence():
    procs = [_p(100, '/opt/ros/lib/pkg/exe', '--ros-args', '-r', '__node:=detector')]
    m = match_nodes_to_processes(['/detector'], procs)
    assert m['/detector'] == {'pid': 100, 'confidence': 'high'}


def test_remap_respects_namespace():
    procs = [_p(101, 'exe', '--ros-args', '-r', '__node:=ndt', '-r', '__ns:=/localization')]
    m = match_nodes_to_processes(['/localization/ndt'], procs)
    assert m['/localization/ndt']['confidence'] == 'high'
    assert m['/localization/ndt']['pid'] == 101


def test_executable_name_match_is_medium():
    # `ros2 run pkg detector` -> the executable basename is the node name.
    procs = [_p(102, '/opt/ros/install/pkg/lib/pkg/detector', '--ros-args')]
    m = match_nodes_to_processes(['/detector'], procs)
    assert m['/detector'] == {'pid': 102, 'confidence': 'medium'}


def test_python_script_basename_strips_py():
    procs = [_p(103, '/usr/bin/python3', '/ws/install/pkg/lib/pkg/planner.py')]
    m = match_nodes_to_processes(['/planner'], procs)
    assert m['/planner'] == {'pid': 103, 'confidence': 'medium'}


def test_bare_token_match_is_low():
    procs = [_p(104, '/usr/bin/some_launcher', 'tracker', '--config', 'x.yaml')]
    m = match_nodes_to_processes(['/tracker'], procs)
    assert m['/tracker'] == {'pid': 104, 'confidence': 'low'}


def test_component_container_caps_confidence_to_low():
    # One container process hosts several nodes via __node:= remaps; per-node
    # CPU can't be separated, so each match is capped at low.
    procs = [_p(200, 'component_container', '--ros-args',
                '-r', '__node:=front_camera', '-r', '__node:=rear_camera')]
    m = match_nodes_to_processes(['/front_camera', '/rear_camera'], procs)
    assert m['/front_camera']['pid'] == 200 and m['/rear_camera']['pid'] == 200
    assert m['/front_camera']['confidence'] == 'low'
    assert m['/rear_camera']['confidence'] == 'low'


def test_unmatched_nodes_are_absent():
    procs = [_p(300, '/opt/ros/lib/pkg/detector')]
    m = match_nodes_to_processes(['/detector', '/anonymous_node_xyz'], procs)
    assert '/detector' in m
    assert '/anonymous_node_xyz' not in m  # honest: no evidence, no guess


def test_highest_confidence_evidence_wins():
    # Two processes could match; the explicit remap should beat the exe match.
    procs = [_p(401, '/opt/ros/lib/pkg/detector'),               # medium
             _p(402, 'other', '--ros-args', '-r', '__node:=detector')]  # high
    m = match_nodes_to_processes(['/detector'], procs)
    assert m['/detector'] == {'pid': 402, 'confidence': 'high'}
