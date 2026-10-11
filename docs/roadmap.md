# Roadmap

## v0.1 — see it
Live ROS graph with auto layout, topic rate/bandwidth/size (opt-in probing),
QoS mismatch detection, TF staleness, `/diagnostics` ingestion, a rule-based
issue panel with evidence, Autoware/Nav2/MoveIt profiles, an AI Markdown
briefing + MCP server, and a demo pipeline.

## v0.2 — use it every day
- Pipeline-stage grouping (stage colours + legend) and an engage-readiness bar
  (per-stage OK/WARN/ERROR).
- `rgd record` / `rgd report` — NDJSON capture → self-contained HTML + Markdown.
- `rgd serve` — replay a recording in the UI with a time-scrubber, incl. a
  no-ROS `--demo`.
- Live tuning — Settings tab + `POST /api/v1/config`, with regex-pattern
  expectations that cover whole stages.
- Topic Network view — a sortable/filterable DevTools-style table.
- `rgd diff` — regression diff between two recordings (rate/callback/issue/health),
  with `--fail-on-regression` for CI gating.
- Message latency Tier A — `header.stamp` age + freshness issues.
- Dedicated TF tree view (live `/tf` forest) and Diagnostics view.
- MCP server with full endpoint coverage + a `set_expected_rate` write tool.
- Focused per-node/-topic AI briefing (`/snapshot.md?focus=TARGET`,
  `get_node_briefing`, and "Copy AI briefing" buttons on the node Inspector and
  every issue card).
- Pipeline-path tracer (`/api/v1/path?target=TARGET`) — the constraining
  source→sink route through a node/topic, lit up on the graph and shown in the
  Inspector and briefing.

## v0.3 — explain it
- **Tier C tracing** *(done for callback durations)*: per-callback execution-time stats
  (`/api/v1/callbacks`, `get_callbacks`, node Inspector + briefing) and a
  `slow_callback` issue when a callback blows its (stage-aware) budget. Real
  traces load via `agent --trace-file run.ndjson` —
  `tracing.aggregate_callback_durations` turns per-invocation durations into
  count/mean/p95/max — and the synthetic source drives `--demo`. The capture →
  NDJSON workflow (with the unit-tested `pair_callback_events` /
  `rows_with_owners` building blocks) is documented in
  [tracing.md](tracing.md). The turn-key converter is *done*:
  `rgd trace convert <session>` (babeltrace2, `trace_convert.py`) resolves
  subscription / timer / service callbacks per process, and
  `agent --trace-file` takes the session directory directly. Remaining: a
  callback-duration timeline along the pipeline path.
- Tier B latency — *done for topic statistics*: rclcpp `/statistics`
  (`MetricsMessage`) feeds rate/age for unprobed topics with explicit
  node→topic attribution (`statistics.py`, `/api/v1/statistics`,
  `get_statistics`). App-specific pipeline-latency topics remain.
- In-UI recording replay scrubbing improvements; snapshot export from the UI.
- Multi-host / federated agents — *done*: `federation.merge_snapshots` stitches
  several agents' snapshots into one host-namespaced view, served by
  `FederatedStore` (background poller). `rgd federate robot1=URL robot2=URL`
  prints a fleet briefing; `--serve` shows the whole fleet live in the web UI.
- Lifecycle (managed) node states — *done*: `GetState` per node serving
  `<node>/get_state`, a `lifecycle_not_active` issue (critical when outputs
  have waiting subscribers), dashed nodes in the graph, and a briefing section.
- Richer node→process attribution — *done*: layered confidence (remap / exe
  name / token) with component-container capping (`procmap.py`). Live `/proc`
  coverage for anonymous and lifecycle nodes remains.

## v0.4 — show it
- EKG sparklines on graph edges, Incident Theater replay with WebM export.
- `rgd diff --image` and a composite GitHub Action for PR graph-diff comments.
- `rgd top` terminal dashboard and the Fleet wall (`rgd serve --demo --fleet N`).

## v0.5 — measure it
- `rgd assert` — a CI health gate over a window of snapshots, no baseline
  recording needed (Markdown / JSON / JUnit, exit codes for CI).
- Distribution — PyPI packaging, a Docker image for the live agent, and a
  tag-triggered release workflow ([releasing.md](releasing.md)).
- Tier B latency from rclcpp topic statistics, with explicit node→topic
  attribution.
- Turn-key Tier C: `rgd trace convert` / `agent --trace-file <session dir>`.
- Lifecycle (managed) node states and a `lifecycle_not_active` issue.

## Next
- *Done, unreleased:* offline rosbag2 / MCAP analysis (`rgd bag analyze` →
  `report` / `diff` / `assert` / `serve` without a live system); the pipeline
  path over time (rate + callback p95 per hop) in `rgd report`; services and
  actions with `no_service_server` / `no_action_server` issues.
- QoS events (deadline missed, liveliness lost, message lost).
- App-specific pipeline-latency topics (the rest of Tier B).

Scope is intentionally driven by "does this reduce a ROS 2 developer's daily
debugging time" rather than feature breadth.
