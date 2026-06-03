"""`rgd` — a tiny CLI client for a running ros_graph_debugger agent.

The agent does the ROS work; this just talks to its REST API so you can grab a
snapshot or an AI-ready Markdown briefing from a script or a CI job.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

DEFAULT_BASE = 'http://127.0.0.1:3939'


def _get(base: str, path: str) -> bytes:
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.read()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='rgd', description='ros_graph_debugger CLI')
    p.add_argument('--base', default=DEFAULT_BASE, help='agent base URL')
    sub = p.add_subparsers(dest='cmd')

    sp = sub.add_parser('snapshot', help='fetch a JSON snapshot')
    sp.add_argument('--out', default=None, help='write to file instead of stdout')

    sub.add_parser('markdown', help='fetch the AI-friendly Markdown briefing')
    sub.add_parser('issues', help='list current issues')
    sub.add_parser('doctor', help='check that the agent is reachable')
    sub.add_parser('version', help='print version')

    args = p.parse_args(argv)

    if args.cmd in (None, 'version'):
        print('ros_graph_debugger 0.1.0')
        return 0

    try:
        if args.cmd == 'snapshot':
            data = _get(args.base, '/api/v1/snapshot')
            if args.out:
                with open(args.out, 'wb') as f:
                    f.write(data)
                print(f'wrote {args.out}')
            else:
                sys.stdout.write(data.decode())
        elif args.cmd == 'markdown':
            sys.stdout.write(_get(args.base, '/api/v1/snapshot.md').decode())
        elif args.cmd == 'issues':
            issues = json.loads(_get(args.base, '/api/v1/issues'))
            if not issues:
                print('No issues.')
            for i in issues:
                print(f'[{i["severity"].upper()}] {i["title"]}')
                for e in i.get('evidence', []):
                    print(f'    - {e}')
        elif args.cmd == 'doctor':
            health = json.loads(_get(args.base, '/api/v1/health'))
            print(f'agent ok: {health}')
    except Exception as exc:
        print(f'error: cannot reach agent at {args.base} ({exc})', file=sys.stderr)
        print('Is the agent running?  ros2 run ros_graph_debugger agent',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
