# Snapshot and recording-header collection baseline

`rgd metadata` returns the existing recording-provenance endpoint as JSON:

```bash
rgd --base http://127.0.0.1:3939 snapshot
rgd --base http://127.0.0.1:3939 metadata
```

In replay mode, metadata contains the complete original recording header,
including extensions and declared header-age clock information. In live mode
it returns `{"mode":"live","recording_header":null}`. Missing clock metadata
stays unknown. Read it alongside the snapshot; header clock-message counts
describe the recording, not the selected frame. These are two requests, not an
atomic or synchronized live measurement. Existing snapshot fields are unchanged.
Connection failure returns exit 1 and stderr, without inventing a header.

## Compare the same evidence

The test-only `measure_collection.py` compares two CLI commands (`snapshot`,
`metadata`) with two MCP tools (`get_runtime_snapshot`,
`get_recording_metadata`) on the same single frozen frame and original header.
It requires complete JSON value/type agreement, including unknown values,
duration sources and integer nanoseconds. It does not grade diagnosis or model
answers. The original [Nav2/CPU comparison](evidence-collection.md) remains a
separate historical experiment.

Prepare a bundle using [the investigation exporter](investigation-cases.md),
with the optional web stack and test-only `mcp==1.30.0` installed, then run:

```bash
python3 test/benchmarks/measure_collection.py \
  --bundle /absolute/path/investigation-bundle \
  --output /tmp/collection-baseline --repeats 5 --port 3987
python3 -m pytest test/test_collection_baseline.py test/test_recording_provenance.py -q
```

Output directories must be fresh, repeats positive and case IDs unique. Each
participant `input.rgd.json` must contain exactly the snapshot and header in
`evidence.json`. Separate sidecars, rubric files and full source recordings are
not fetched or timed. The benchmark does not read an evaluator rubric for this
transport comparison. Run without concurrent local validation work; keep port
3987 free or change it. Readiness uses polling. Owned CLI/replay groups and MCP
sessions close on success and failure; failed CLI stdout/stderr and completed
cases are retained. Input files remain unchanged.

The first collection is reported separately from five subsequent connected
session repetitions. Order alternates by case and iteration. Each CLI command
starts a new Python process; MCP reuses one stdio session per case. MCP process
startup/initialization, tool discovery and first collection have separate times;
their sum is also reported. Discovery is one additional control request,
outside the two evidence tool calls. Readiness and replay startup are excluded.

`total_ns` is the sum of two timed operation intervals, **not full collection
wall time**. CLI intervals include process creation and completion; MCP intervals
span each tool call. JSON decoding, response-file writes, sidecar reads, scoring,
reasoning and teardown are excluded. Application text bytes exclude protocol
envelopes and are not wire traffic. Per-case min/median/max and raw repetitions
remain available; there is no speed threshold or assertion that one client must
be faster.

## Current-instance observations

[Results](recording-collection-results.json) cover 16 source-task entries,
representing 14 unique snapshot/header pairs. They reuse actual ROS integration
and C++ LTTng recordings, plus public-input Autoware gyro/converter phase
recordings and whole-capture trace imports. The older trace retains missing
source fields; the newer import retains exact declared nanosecond coordinates.
No synthetic control or new ROS/Autoware experiment is included in this timing
run. Duplicate task views are not counted as independent source observations.

On the current Linux/Python 3.12 environment, all first collections and five
repeats per entry matched the complete snapshot/header. Original source hashes
and owned-process cleanup passed. Observed ranges across per-case medians:

| Measurement | Observed range |
|---|---:|
| Two CLI processes | 118.4–149.5 ms |
| Two calls in a connected MCP session | 9.4–14.2 ms |
| MCP initialization + discovery + first collection | 413.9–583.9 ms |

The connected-session advantage includes avoiding repeated process startup.
These values are transport measurements on this machine, not a general speed
guarantee, human investigation-time reduction or blind AI accuracy. A first
MCP session has a substantial setup cost. Frozen consistency also cannot prove
that two requests to a changing live system belong to one observation window.
Historical callback statistics do not establish current component health.
File hashes bind input identity; they do not authenticate capture provenance.

The existing downstream Humble/Jazzy investigation CI jobs measure their ten
original cases with three connected-session repeats. Exact evidence and cleanup
are required, while timing values are informational. `collection-validation/`
is uploaded with the existing `investigation-<distro>` artifact even on failure.
The original multiprocess integration job keeps its five-minute limit and gates.
