# Changelog

All notable changes to **ros_graph_debugger**. This project follows a
roadmap-driven 0.x line where each minor version is a coherent feature set
(see [docs/roadmap.md](docs/roadmap.md)).

## [Unreleased]

### Added
- Nothing yet.

## [0.4.0] — show it — 2026-06-11

### Added
- **EKG sparklines** on graph edges — recent topic-rate history is drawn on
  the live graph, turning red when a rate/bottleneck issue touches that topic.
- **Incident Theater** — replay/cinema mode pans to the first critical issue,
  slows the timeline, captions evidence, lights the pipeline path, and can
  export a browser-recorded WebM.
- **Diff image output and PR action** — `rgd diff --image out.svg` renders a
  before/after regression graph, with an included composite GitHub Action for
  PR artifact/comment workflows.
- **Terminal top view** — `rgd top` provides an SSH-friendly dashboard with
  readiness, topic/node rows, sparklines, CPU/callback p95, and issue context
  through the optional `ros_graph_debugger[tui]` extra.
- **Fleet wall** — federated snapshots with multiple hosts show per-robot
  health tiles, mini graph thumbnails, red pulsing critical hosts, and
  click-through drill-down. `rgd serve --demo --fleet N` demos this without DDS.
- **Animated data flow** on the graph — dashes march pub→sub at a rate-scaled
  speed, bottleneck nodes pulse a red halo, with a **✨ Flow** toggle and a
  perf guard for large graphs.
- **Regression diff** (`diff.diff_reports`, `rgd diff before after`) — compares
  two recordings and reports what got worse: topic-rate drops, slower callbacks,
  new issues, and a health-verdict change, as a Markdown / JSON briefing.
  `--fail-on-regression` exits non-zero to gate CI.

## [0.3.0] — explain it — 2026-06-03

### Added
- **Tier C tracing (synthetic source)** — per-callback execution-time stats
  (`CallbackStat`, `/api/v1/callbacks`, `get_callbacks`), a `slow_callback`
  issue, and **stage-aware callback budgets** resolved from profile/pattern
  expectations (`max_callback_ms`). Real traces load via `agent --trace-file`
  (`aggregate_callback_durations` turns per-invocation durations into
  count/mean/p95/max); automating the `ros2_tracing`/LTTng → NDJSON export is
  the remaining work.
- **Pipeline-path tracer** — the constraining source→sink route through a
  node/topic, following the lowest-rate link, annotated with each hop's
  callback p95, so the rate bottleneck *and* the execution bottleneck read in
  one line. Lit up on the graph and shown in the Inspector, the focused AI
  briefing, `GET /api/v1/path`, and the `get_pipeline_path` MCP tool.
- **System health verdict** — a one-line `ok`/`degraded`/`critical` rollup in
  the web header, the briefing lead line, and `GET /api/v1/summary`.
- **Focused per-node/-topic AI briefings** — `GET /api/v1/snapshot.md?focus=`,
  the `get_node_briefing` MCP tool, and "Copy AI briefing" buttons on the node
  Inspector and every issue card.
- **Inspector cross-links** — related issues, callback p95 list, and the
  pipeline path for the selected node; one-click **JSON / MD export** buttons.
- **Recording report** — a system-health rollup and a slowest-callbacks table;
  `slow_callback` now ranks among bottlenecks.
- **Layered node→process attribution** (`procmap.py`) — confidence from the
  matching evidence (`__node:=` remap → high, executable name → medium, bare
  token → low) with component containers capped at low.
- **Fleet federation** (`federation.merge_snapshots`, `rgd federate`) — merge
  several agents' snapshots into one host-namespaced view with per-host health
  and a fleet-wide AI briefing; `rgd federate --serve` (a `FederatedStore`
  background poller) shows the whole fleet live in the web UI.
- **Showcase** — a regenerable [docs/example_briefing.md](docs/example_briefing.md)
  ("what an AI sees") and a Mermaid pipeline diagram in the README.

### Changed
- Enriched the Autoware / Nav2 / MoveIt profiles with realistic, stage-aware
  callback budgets (control tight, planning loose).

## [0.2.0] — use it every day

### Added
- Pipeline-stage grouping (stage colours + legend) and a per-stage
  engage-readiness bar.
- `rgd record` / `rgd report` (NDJSON capture → self-contained HTML + Markdown)
  and `rgd serve` replay with a time-scrubber, including a no-ROS `--demo`.
- Live config tuning (Settings tab + `POST /api/v1/config`) with regex-pattern
  expectations that cover whole stages.
- Topic Network view, dedicated TF tree and Diagnostics views.
- Message latency Tier A (`header.stamp` age + freshness issues).
- MCP server with full endpoint coverage + a `set_expected_rate` write tool.

## [0.1.0] — see it

### Added
- Live ROS graph with auto layout, topic rate/bandwidth/size (opt-in probing),
  QoS mismatch detection, TF staleness, `/diagnostics` ingestion, a rule-based
  issue engine with evidence, Autoware/Nav2/MoveIt profiles, an AI Markdown
  briefing + MCP server, and a demo pipeline.
