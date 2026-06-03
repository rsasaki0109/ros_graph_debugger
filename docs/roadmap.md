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

## v0.3 — explain it
- **Tier C tracing**: `ros2_tracing` adapter → callback-duration timeline and
  critical-path analysis.
- Tier B latency via `/statistics` and pipeline-latency topics.
- In-UI recording replay scrubbing improvements; snapshot export from the UI.
- Multi-host / federated agents.
- Dedicated TF tree and diagnostics views.
- Richer node→process attribution.

Scope is intentionally driven by "does this reduce a ROS 2 developer's daily
debugging time" rather than feature breadth.
