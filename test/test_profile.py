"""Profile loading + the /api/v1/profile contract the web UI depends on."""

import re

from ros_graph_debugger.paths import find_profile
from ros_graph_debugger.profile import load_profile


def test_autoware_profile_loads():
    path = find_profile('autoware')
    assert path, 'autoware profile should be discoverable from the source tree'
    data, name = load_profile(path)
    assert name == 'autoware'

    # Groups carry topic_patterns that are valid regexes (the UI compiles them).
    groups = data['groups']
    assert {'sensing', 'localization', 'perception', 'planning', 'control'} <= set(groups)
    for g in groups.values():
        for pat in g.get('topic_patterns', []):
            re.compile(pat)  # raises if invalid

    # Expectations are parsed into the engine-facing maps.
    assert data['_expected_min_rate']['/perception/object_recognition/objects'] == 10
    assert '/control/command/control_cmd' in data['_expected_min_rate']


def test_stage_patterns_match_expected_topics():
    """Sanity: the demo/Autoware topic names actually fall into their stages."""
    data, _ = load_profile(find_profile('autoware'))
    groups = data['groups']

    def stage_of(name):
        for k, g in groups.items():
            for pat in g.get('topic_patterns', []):
                if re.search(pat, name):
                    return k
        return None

    assert stage_of('/sensing/camera/image_raw') == 'sensing'
    assert stage_of('/perception/object_recognition/objects') == 'perception'
    assert stage_of('/planning/scenario_planning/trajectory') == 'planning'
    assert stage_of('/control/command/control_cmd') == 'control'


def test_nav2_profile_loads():
    data, name = load_profile(find_profile('nav2'))
    assert name == 'nav2'
    assert 'control' in data['groups']
    assert data['_expected_min_rate']['/cmd_vel'] == 10
