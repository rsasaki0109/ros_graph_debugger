# Collecting diagnostic evidence through CLI and MCP

`get_runtime_snapshot` reads the existing `/api/v1/snapshot` endpoint once. It
returns the timestamp, nodes, topics, TF, diagnostics, callbacks and issues as
JSON, including issue kinds/evidence, mapping confidence and unavailable
metrics as null. This matches `rgd snapshot` and avoids combining data from
several HTTP requests when the live system may change between requests.
It represents the current collected state, not a synchronized measurement of
every sensor or callback.

## Reproducible evaluation

The experiment uses frozen snapshots from actual separate-process ROS runs:

- Official Jazzy Nav2 map server, NavFn planner, regulated pure pursuit
  controller and loopback simulator: healthy goal, simulator stopped with
  SIGSTOP, then resumed and recovered. The checked evidence is lifecycle,
  `/scan` and `/odom` delivery, their publisher, map→odom→base_footprint TF
  freshness, and `topic_stale` / `tf_stale` kinds and evidence.
- A real ROS CPU worker: initial unknown CPU, service-controlled load above
  90% of one core, and recovery below 20%. The checked evidence is the actual
  PID, high mapping confidence, CPU value and `high_cpu` kind/evidence.

Nav2 recordings were produced by `run_navigation_scenario.py` from
[PR #12](https://github.com/rsasaki0109/ros_graph_debugger/pull/12).
CPU snapshots were produced by `run_cpu_scenario.py` from
[PR #15](https://github.com/rsasaki0109/ros_graph_debugger/pull/15).
Historical Nav2 CPU values predate the CPU baseline fix and are not evidence
of an idle process. No CPU-health claim is scored from those recordings.

Install the existing optional MCP SDK (`pip install "mcp[cli]"`), build/source
the relevant scenario checkout, generate the recordings, then run from this
checkout with its CLI/web dependencies installed:

```bash
python3 test/benchmarks/evaluate_evidence.py \
  --nav-recordings /absolute/path/nav2-scenario-output \
  --cpu-snapshots /absolute/path/cpu-scenario-output \
  --output /tmp/evidence-bundle --mcp-mode bundle
python3 test/benchmarks/evaluate_evidence.py \
  --nav-recordings /absolute/path/nav2-scenario-output \
  --cpu-snapshots /absolute/path/cpu-scenario-output \
  --output /tmp/evidence-snapshot --mcp-mode snapshot
```

The Nav2 directory must contain healthy/paused/recovered `run.rgd.json` files;
the CPU directory must contain warmup/loaded/recovered JSON snapshots. Use
fresh output directories, run the comparisons sequentially without concurrent
local work, and keep port 3971 free (`--port` overrides it). Each case selects
one original frame, preserves its provenance/hash and replays it unchanged.
Readiness is polled; replay process groups and MCP sessions are stopped on
completion/failure. Raw inputs, each response, selected facts and timing values
remain in the output directory.

## Observed result and limits

On 2026-10-12, all six cases agreed with the source facts through both CLI and
MCP, including five warm repeats per case. Source hashes and derived timings
are in [evidence-collection-results.json](evidence-collection-results.json).

| Evidence task | Existing MCP reads | Complete snapshot | CLI commands |
|---|---:|---:|---:|
| Nav2 lifecycle, input delivery and TF | 3 | 1 | 1 |
| CPU PID, value and issue evidence | 2 | 1 | 1 |

The bundle uses graph/TF/issues for Nav2 and nodes/issues for CPU. CLI uses a
single `rgd snapshot` process in either experiment. MCP reuses a connected
stdio session; its initialization/discovery and first collection are recorded
separately. Warm collection timings include response transport, not model
reasoning or a developer's work. Full snapshots can contain more irrelevant
data than targeted tools; select the smaller tool when only one group is needed.

This is a scripted evidence-fidelity and transport comparison. It does not
establish human investigation-time savings, blind AI diagnosis accuracy,
external adoption, complete trace attribution or physical-robot behavior.
The simulator-stop ground truth and CPU control are known to the evaluator.
Lifecycle remaining active and a publisher remaining discoverable do not prove
that messages are arriving; CPU alone does not prove successful processing.
