"""Version is declared once and mirrored everywhere — this pins them together.

If you bump the version, change ros_graph_debugger/__init__.py and the others
to match (and add a CHANGELOG entry), or this fails.
"""

import os
import re

from ros_graph_debugger import __version__

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))


def _read(rel):
    with open(os.path.join(_ROOT, rel)) as f:
        return f.read()


def test_setup_py_version_matches():
    m = re.search(r"version='([^']+)'", _read('setup.py'))
    assert m and m.group(1) == __version__


def test_package_xml_version_matches():
    m = re.search(r'<version>([^<]+)</version>', _read('package.xml'))
    assert m and m.group(1) == __version__


def test_changelog_top_entry_matches():
    # The first "## [x.y.z]" heading must be the current version.
    m = re.search(r'^## \[([0-9]+\.[0-9]+\.[0-9]+)\]', _read('CHANGELOG.md'),
                  re.MULTILINE)
    assert m and m.group(1) == __version__


def test_cli_and_server_use_the_package_version():
    # Neither file should hard-code a version string anymore.
    assert "'0.1.0'" not in _read('ros_graph_debugger/server.py')
    assert '0.1.0' not in _read('ros_graph_debugger/cli.py')
    assert '__version__' in _read('ros_graph_debugger/server.py')
