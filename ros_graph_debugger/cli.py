"""`rgd` — a tiny CLI client for a running ros_graph_debugger agent.

The agent does the ROS work; this just talks to its REST API so you can grab a
snapshot or an AI-ready Markdown briefing from a script or a CI job.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

DEFAULT_BASE = 'http://127.0.0.1:3939'


def _get(base: str, path: str) -> bytes:
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.read()


def _cmd_record(args) -> int:
    """Poll the agent at a fixed interval and stream snapshots to NDJSON."""
    from .recording import append_snapshot, make_header, write_header

    profile = None
    try:
        profile = json.loads(_get(args.base, '/api/v1/profile'))
        if not profile.get('groups'):
            profile = None
    except Exception:
        pass

    started = time.time()
    header = make_header(started=started, interval=args.interval, profile=profile)
    n = 0
    deadline = started + args.duration
    print(f'recording {args.duration}s @ {args.interval}s -> {args.out} '
          '(Ctrl-C to stop early)')
    with open(args.out, 'w') as fh:
        write_header(fh, header)
        try:
            while time.time() < deadline:
                tick = time.time()
                try:
                    snap = json.loads(_get(args.base, '/api/v1/snapshot'))
                    append_snapshot(fh, snap)
                    n += 1
                    print(f'\r  {n} samples, {len(snap.get("issues", []))} issues  ',
                          end='', flush=True)
                except Exception as exc:
                    print(f'\n[warn] sample failed: {exc}', file=sys.stderr)
                sleep = args.interval - (time.time() - tick)
                if sleep > 0:
                    time.sleep(sleep)
        except KeyboardInterrupt:
            print('\nstopped.')
    print(f'\nwrote {n} samples to {args.out}')
    return 0


def _cmd_report(args) -> int:
    from .recording import read_recording
    from .report import build_report, render_html, render_markdown

    header, snapshots = read_recording(args.file)
    if not snapshots:
        print(f'error: no snapshots in {args.file}', file=sys.stderr)
        return 1
    summary = build_report(header, snapshots)

    wrote_any = False
    if args.html:
        with open(args.html, 'w') as f:
            f.write(render_html(summary))
        print(f'wrote {args.html}')
        wrote_any = True
    if args.md:
        with open(args.md, 'w') as f:
            f.write(render_markdown(summary))
        print(f'wrote {args.md}')
        wrote_any = True
    if not wrote_any:
        # Default: Markdown briefing to stdout (AI-friendly).
        sys.stdout.write(render_markdown(summary))
    return 0


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

    rp = sub.add_parser('record', help='record snapshots to an NDJSON file')
    rp.add_argument('--out', required=True, help='output .rgd.json file')
    rp.add_argument('--duration', type=float, default=30.0, help='seconds')
    rp.add_argument('--interval', type=float, default=1.0, help='seconds between samples')

    rep = sub.add_parser('report', help='build an HTML / Markdown report from a recording')
    rep.add_argument('file', help='recording file from `rgd record`')
    rep.add_argument('--html', default=None, help='write a self-contained HTML report')
    rep.add_argument('--md', default=None, help='write a Markdown report')

    args = p.parse_args(argv)

    if args.cmd in (None, 'version'):
        print('ros_graph_debugger 0.1.0')
        return 0
    if args.cmd == 'record':
        return _cmd_record(args)
    if args.cmd == 'report':
        return _cmd_report(args)

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
