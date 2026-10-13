"""`ros2 run ros_graph_debugger agent` — spin the collectors and serve the UI.

rclpy spins on a background thread with a single-threaded executor; uvicorn runs
the FastAPI app on the main thread. They communicate only through the
thread-safe RuntimeGraphStore.
"""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser

import rclpy
from rclpy.utilities import remove_ros_args

from .config import ProbeConfig, Thresholds
from .node import DebuggerNode
from .model import RuntimeGraphStore
from .paths import find_profile, find_web_dir
from .profile import apply_profile, load_profile
from .probe_executor import ProbeExecutor
from .server import create_app


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog='ros_graph_debugger',
        description='Runtime DevTools for ROS 2 — live graph + metrics + issues.')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=3939)
    p.add_argument('--profile', default=None,
                   help='profile pack: autoware | nav2 | moveit | path to yaml')
    p.add_argument('--no-probe', action='store_true',
                   help='disable all message-rate probing')
    p.add_argument('--probe-topic', action='append', default=[],
                   metavar='GLOB', help='probe only these topics (repeatable)')
    p.add_argument('--probe-regex', default='',
                   help='probe topics matching this regex')
    p.add_argument('--probe-large-topics', action='store_true',
                   help='also probe Image/PointCloud2/LaserScan (heavy!)')
    p.add_argument('--max-probe-topics', type=int, default=12)
    p.add_argument('--expect', action='append', default=[], metavar='TOPIC=HZ',
                   help='expected minimum rate, e.g. /objects=10 (repeatable)')
    p.add_argument('--no-browser', action='store_true',
                   help='do not open a browser automatically')
    p.add_argument('--statistics-topic', action='append', default=None,
                   metavar='STATS[=TOPIC]',
                   help='rclcpp topic-statistics topic to read (latency Tier B); '
                        'map it to the data topic it measures with =TOPIC. '
                        'Repeatable; replaces the default /statistics')
    p.add_argument('--no-statistics', action='store_true',
                   help='do not read topic statistics')
    p.add_argument('--processing-time-topic', action='append', default=[],
                   metavar='REGEX',
                   help='topics carrying a node\'s own processing time in ms '
                        '(numeric `data`), e.g. \'/debug/processing_time_ms$\'; '
                        'adds to the profile\'s processing_time_patterns')
    p.add_argument('--no-lifecycle', action='store_true',
                   help='do not query lifecycle (managed) node states')
    p.add_argument('--trace-file', default=None, metavar='PATH',
                   help='callback-duration trace (Tier C) to load as callback '
                        'stats: a `ros2 trace` session directory (needs '
                        'python3-bt2) or NDJSON from `rgd trace convert`')
    ros_argv = None if argv is None else ['ros_graph_debugger', *argv]
    return p.parse_args(remove_ros_args(args=ros_argv)[1:])


def main(argv=None) -> None:
    args = _parse_args(argv)

    probe = ProbeConfig(
        enabled=not args.no_probe,
        include_patterns=list(args.probe_topic),
        regex=args.probe_regex,
        allow_large=args.probe_large_topics,
        max_topics=args.max_probe_topics,
    )
    thresholds = Thresholds()

    profile_data = None
    profile_name = None
    if args.profile:
        path = find_profile(args.profile)
        if path:
            profile_data, profile_name = load_profile(path)
            apply_profile(thresholds, profile_data)
        else:
            print(f'[warn] profile not found: {args.profile}')

    for item in args.expect:
        if '=' in item:
            topic, hz = item.split('=', 1)
            try:
                thresholds.expected_min_rate[topic] = float(hz)
            except ValueError:
                print(f'[warn] bad --expect value: {item}')

    store = RuntimeGraphStore()

    if args.trace_file:
        from .trace_convert import TraceReaderUnavailable
        from .tracing import callbacks_from_trace_file
        try:
            store.set_callbacks(callbacks_from_trace_file(args.trace_file))
        except (OSError, TraceReaderUnavailable) as exc:
            print(f'[warn] could not read --trace-file: {exc}')

    from .statistics import parse_statistics_topics
    statistics_topics = {} if args.no_statistics \
        else parse_statistics_topics(args.statistics_topic)

    rclpy.init(args=None if argv is None else ['ros_graph_debugger', *argv])
    node = DebuggerNode(store, probe, thresholds, profile_name,
                        statistics_topics=statistics_topics,
                        lifecycle=not args.no_lifecycle,
                        processing_time_patterns=(
                            list((profile_data or {}).get('processing_time_patterns') or [])
                            + list(args.processing_time_topic)))
    executor = ProbeExecutor()
    executor.add_node(node)

    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    web_dir = find_web_dir()
    app = create_app(store, web_dir, profile_data=profile_data,
                     thresholds=thresholds)

    url = f'http://{args.host}:{args.port}'
    print(f'\n  ros_graph_debugger running at {url}')
    print(f'  AI snapshot:  {url}/api/v1/snapshot.md')
    print(f'  REST API:     {url}/api/v1/\n')

    if not args.no_browser and os.environ.get('DISPLAY'):
        try:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    import uvicorn
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level='warning')
    finally:
        executor.shutdown()
        # shutdown() can return while the spin loop still uses native handles.
        spin_thread.join()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
