import os
from glob import glob

from setuptools import find_packages, setup

package_name = 'ros_graph_debugger'
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()


def data_tree(install_root, source_root):
    """Recursively map a source dir into ament share/ install entries."""
    entries = []
    for path in glob(os.path.join(source_root, '**', '*'), recursive=True):
        if os.path.isfile(path):
            rel = os.path.relpath(os.path.dirname(path), source_root)
            dst = os.path.join(install_root, rel) if rel != '.' else install_root
            entries.append((dst, [path]))
    return entries


console_scripts = [
    'agent = ros_graph_debugger.agent:main',
    'rgd = ros_graph_debugger.cli:main',
    'demo_pipeline = ros_graph_debugger.examples.demo_pipeline:main',
]
# colcon scopes scripts to lib/<pkg>/ (setup.cfg), but a pip install puts them
# on the global PATH, where generic names like `agent` would collide. The PyPI
# release build (RGD_PYPI_BUILD=1) ships only `rgd`; the live agent needs ROS
# anyway and runs as `python -m ros_graph_debugger.agent`.
if os.environ.get('RGD_PYPI_BUILD'):
    console_scripts = [s for s in console_scripts if s.startswith('rgd ')]

data_files = [
    ('share/ament_index/resource_index/packages',
     ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name, glob('profiles/*.yaml')),
]
data_files += data_tree('share/' + package_name + '/web', 'ros_graph_debugger/web')

setup(
    name=package_name,
    version='0.5.0',
    packages=find_packages(exclude=['test']),
    data_files=data_files,
    # Kept minimal so colcon/rosdep builds stay unaffected: the offline CLI
    # (report, diff, assert, federate, mcp_server) is stdlib-only. Anything
    # that serves the web UI needs the `web` extra; the live agent needs ROS 2.
    install_requires=['setuptools'],
    extras_require={
        'web': ['fastapi', 'uvicorn', 'websockets', 'pydantic', 'pyyaml'],
        'image': ['cairosvg'],
        'tui': ['textual'],
    },
    python_requires='>=3.10',
    zip_safe=True,
    maintainer='Ryohei Sasaki',
    maintainer_email='rsasaki0109@gmail.com',
    description='Runtime DevTools for ROS 2 — graph + metrics + issues, AI-friendly web view.',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/rsasaki0109/ros_graph_debugger',
    project_urls={
        'Live demo': 'https://rsasaki0109.github.io/ros_graph_debugger/',
        'Changelog': 'https://github.com/rsasaki0109/ros_graph_debugger/blob/main/CHANGELOG.md',
    },
    license='Apache-2.0',
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: POSIX :: Linux',
        'Topic :: Scientific/Engineering :: Artificial Intelligence',
        'Topic :: Software Development :: Debuggers',
    ],
    tests_require=['pytest'],
    entry_points={'console_scripts': console_scripts},
)
