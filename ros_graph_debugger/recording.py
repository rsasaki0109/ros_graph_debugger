"""Recording format for ros_graph_debugger snapshots.

A recording is newline-delimited JSON (NDJSON):

    line 0   header   {"rgd_recording": "1", "started": <epoch>, "interval": ...,
                       "profile": {<groups>} | null}
    line 1.. snapshot  one GraphSnapshot dict per line, in capture order

NDJSON is append-friendly (a long capture streams to disk without holding
everything in memory) and trivially greppable. Both reader and writer tolerate
blank/garbage lines so a truncated capture (Ctrl-C) still loads.
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator, TextIO

RECORDING_VERSION = '1'


def make_header(started: float, interval: float,
                profile: dict | None = None) -> dict:
    return {
        'rgd_recording': RECORDING_VERSION,
        'started': started,
        'interval': interval,
        'profile': profile,
    }


def write_header(fh: TextIO, header: dict) -> None:
    fh.write(json.dumps(header) + '\n')
    fh.flush()


def append_snapshot(fh: TextIO, snapshot: dict) -> None:
    fh.write(json.dumps(snapshot, separators=(',', ':')) + '\n')
    fh.flush()


def write_recording(path: str, header: dict,
                    snapshots: Iterable[dict]) -> int:
    n = 0
    with open(path, 'w') as fh:
        write_header(fh, header)
        for snap in snapshots:
            append_snapshot(fh, snap)
            n += 1
    return n


def read_recording(path: str) -> tuple[dict, list[dict]]:
    header: dict = {}
    snapshots: list[dict] = []
    with open(path, 'r') as fh:
        for i, line in enumerate(_iter_json_lines(fh)):
            if i == 0 and line.get('rgd_recording'):
                header = line
            else:
                snapshots.append(line)
    if not header:
        # Headerless file: treat every line as a snapshot.
        header = make_header(started=0.0, interval=0.0)
    return header, snapshots


def _iter_json_lines(fh: TextIO) -> Iterator[dict]:
    for raw in fh:
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue
