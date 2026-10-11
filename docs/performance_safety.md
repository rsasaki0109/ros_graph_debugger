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
- **Tier B (implemented)** — rclcpp topic statistics: subscriptions created
  with topic statistics enabled publish `statistics_msgs/MetricsMessage`
  (`message_period`, `message_age`, ms) once per window. The agent reads
  `/statistics` (or `--statistics-topic STATS[=TOPIC]`, repeatable;
  `--no-statistics` to disable) and overlays the rate (`1000 / avg period`, the
  slowest subscriber) and age (avg, p95 ≈ avg + 1.645·stddev capped at the
  window max — the message has no percentiles) on topics it does **not** probe.
  A window with no samples reads as 0 Hz, so a dead sensor still trips its
  expected rate.
  This costs the agent one small subscription and lets large sensor topics be
  measured without subscribing to them. Attribution is never guessed: the
  message names the subscriber node only, so a node with several data
  subscriptions stays unattributed until its stats topic is mapped. Stats older
  than 10 s are dropped. Nodes' own processing-time topics (e.g. Autoware's
  `~/debug/processing_time_ms`, matched by the profile's
  `processing_time_patterns` or `--processing-time-topic`) are subscribed
  best-effort — tiny messages, at most 300 topics — and read as the node's
  execution time.
- **Tier C (implemented for callback durations)** — `ros2_tracing` / LTTng
  traces, converted offline (`rgd trace convert`, or `agent --trace-file
  <session dir>`) into per-callback count/mean/p95/max. Recording is
  Linux-only and opt-in; reading needs `python3-bt2`. The agent never starts a
  trace itself. Publish→subscribe spans remain roadmap.

## Lifecycle state queries

The one place the agent talks *to* target nodes: for each lifecycle node
(detected passively from its `<node>/get_state` service) it calls the
standard, read-only `lifecycle_msgs/srv/GetState` every 2 s — asynchronously,
at most one request in flight per node, abandoned after 5 s. It never calls
`change_state`; the issue only *suggests* the `ros2 lifecycle set` command.
Disable with `agent --no-lifecycle`.

## Failure isolation

A failing collector never takes down the node: graph polls, probes, and the
issue engine are individually wrapped, log a warning, and continue with partial
data. Unintrospectable topic types are skipped, not fatal.
