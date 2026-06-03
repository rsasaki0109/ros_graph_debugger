# Performance & safety

ROS Graph Debugger is meant to run *against* a live robot without destabilizing
it. The defaults are conservative; everything heavier is opt-in.

## Passive by default

The graph, QoS, TF, and diagnostics are collected from the ROS graph API and a
few small infrastructure subscriptions (`/tf`, `/tf_static`, `/diagnostics`).
No application data topics are subscribed unless probing is enabled.

## Topic probing is opt-in and bounded

Rate / bandwidth / size / latency come from lightweight **raw** subscriptions
(`raw=True`) — no message type is deserialized for size, and `header.stamp` is
read best-effort only for typed-headered messages (the probe learns headerless
types and stops deserializing them).

Guards:

- **Large sensor types are never auto-probed**: `sensor_msgs/Image`,
  `CompressedImage`, `PointCloud2`, `LaserScan`. Enable with
  `--probe-large-topics` or target them explicitly with `--probe-topic`.
- **At most `--max-probe-topics` (default 12)** topics are probed at once.
- **Bounded memory**: each probe keeps a fixed-size ring buffer of samples.
- Narrow scope with `--probe-topic GLOB`, `--probe-regex RE`, or disable
  entirely with `--no-probe`.
- `/rosout` and `/parameter_events` are never probed.

## Honest about uncertainty

Node→process mapping in ROS 2 is inherently partial (composition containers,
anonymous nodes, remaps). The agent only maps processes launched with an
explicit `__node:=` remap and reports a `process_mapping_confidence` of
`none | low | medium | high`. CPU/RSS for a shared container is reported as
process-level, not per-node. The UI shows the confidence; it never claims
exact per-node CPU it cannot prove.

## Latency tiers

End-to-end latency is approximated in tiers, from cheap to accurate:

- **Tier A (implemented)** — `header.stamp` age (p50/p95) of probed messages.
  Cheap, approximate, only meaningful for messages with a populated header.
- **Tier B (roadmap)** — `/statistics` (ROS 2 topic statistics) and app-specific
  pipeline-latency topics.
- **Tier C (roadmap)** — `ros2_tracing` / LTTng for true callback and
  publish→subscribe spans. Linux-only, opt-in recording.

## Failure isolation

A failing collector never takes down the node: graph polls, probes, and the
issue engine are individually wrapped, log a warning, and continue with partial
data. Unintrospectable topic types are skipped, not fatal.
