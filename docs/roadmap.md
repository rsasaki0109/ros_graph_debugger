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
- **Tier C tracing** *(in progress)*: per-callback execution-time stats
  (`/api/v1/callbacks`, `get_callbacks`, node Inspector + briefing) and a
  `slow_callback` issue when a callback blows its budget. The data shape and a
  synthetic source ship now (`tracing.synthesize_callbacks`, demonstrated by
  `--demo`); the live `ros2_tracing`/LTTng adapter that emits the same shape,
  plus a callback-duration timeline along the pipeline path, are next.
- Tier B latency via `/statistics` and pipeline-latency topics.
- In-UI recording replay scrubbing improvements; snapshot export from the UI.
- Multi-host / federated agents.
- Richer node→process attribution — *done*: layered confidence (remap / exe
  name / token) with component-container capping (`procmap.py`). Live `/proc`
  coverage for anonymous and lifecycle nodes remains.

Scope is intentionally driven by "does this reduce a ROS 2 developer's daily
debugging time" rather than feature breadth.
