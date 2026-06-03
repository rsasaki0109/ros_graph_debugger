"""Locate bundled assets whether running from an install or the source tree."""

from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _share_dir() -> str | None:
    try:
        from ament_index_python.packages import get_package_share_directory
        return get_package_share_directory('ros_graph_debugger')
    except Exception:
        return None


def find_web_dir() -> str:
    env = os.environ.get('RGD_WEB_DIR')
    if env and os.path.isdir(env):
        return env
    share = _share_dir()
    if share and os.path.isdir(os.path.join(share, 'web')):
        return os.path.join(share, 'web')
    # source tree: ros_graph_debugger/web
    return os.path.join(_HERE, 'web')


def find_profile(name: str) -> str | None:
    # Direct path?
    if os.path.isfile(name):
        return name
    base = name if name.endswith('.yaml') else f'{name}.yaml'
    candidates = []
    share = _share_dir()
    if share:
        candidates.append(os.path.join(share, base))
    # source tree: <repo>/profiles
    candidates.append(os.path.join(os.path.dirname(_HERE), 'profiles', base))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None
