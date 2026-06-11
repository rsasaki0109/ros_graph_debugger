import os
from glob import glob

from setuptools import find_packages, setup

package_name = 'ros_graph_debugger'


def data_tree(install_root, source_root):
    """Recursively map a source dir into ament share/ install entries."""
    entries = []
    for path in glob(os.path.join(source_root, '**', '*'), recursive=True):
        if os.path.isfile(path):
            rel = os.path.relpath(os.path.dirname(path), source_root)
            dst = os.path.join(install_root, rel) if rel != '.' else install_root
            entries.append((dst, [path]))
    return entries


data_files = [
    ('share/ament_index/resource_index/packages',
     ['resource/' + package_name]),
    ('share/' + package_name, ['package.xml']),
    ('share/' + package_name, glob('profiles/*.yaml')),
]
data_files += data_tree('share/' + package_name + '/web', 'ros_graph_debugger/web')

setup(
    name=package_name,
    version='0.4.0',
    packages=find_packages(exclude=['test']),
    data_files=data_files,
    install_requires=['setuptools'],
    extras_require={
        'image': ['cairosvg'],
        'tui': ['textual'],
    },
    zip_safe=True,
    maintainer='Ryohei Sasaki',
    maintainer_email='rsasaki0109@gmail.com',
    description='Runtime DevTools for ROS 2 — graph + metrics + issues, AI-friendly web view.',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'agent = ros_graph_debugger.agent:main',
            'rgd = ros_graph_debugger.cli:main',
            'demo_pipeline = ros_graph_debugger.examples.demo_pipeline:main',
        ],
    },
)
