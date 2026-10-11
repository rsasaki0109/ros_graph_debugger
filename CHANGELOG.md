# Changelog

All notable changes to **ros_graph_debugger**. This project follows a
roadmap-driven 0.x line where each minor version is a coherent feature set
(see [docs/roadmap.md](docs/roadmap.md)).

## [Unreleased]

### Added
- **Services & actions** — nodes now carry the services they call
  (`service_clients`) as well as serve; the snapshot gains `services` and
  `actions` (`{name, servers, clients}`, actions found via
  `<action>/_action/send_goal`, parameter services omitted). A client with no
  server among the graph's nodes raises `no_service_server` /
  `no_action_server`. Shown in the node Inspector
  and as a "Clients without a server" briefing section.
- **Pipeline path over time** — `rgd report` follows one pipeline path across
  the recording: for each hop, the topic rate and the consuming node's
  callback p95 per sample, with the samples where the focus was blamed marked
  (sparklines in Markdown, small-multiple SVGs in HTML). The focus defaults to
  the node most often named by bottleneck / slow-callback issues;
  `rgd report --path TARGET` picks another. The path is traced once and held
  fixed so the series compare like with like.
- **Offline rosbag2 / MCAP analysis** — `rgd bag analyze BAG --out run.rgd.json`
  turns a bag (directory, `.mcap` or `.db3`) into a snapshot recording, so
  `rgd report` / `diff` / `assert` / `serve` work on it with no live system.
  Windows of bag time (`--interval`, `--window`) get topic rate, bandwidth,
  size and header-stamp age (read from the CDR bytes without deserializing),
  TF freshness and diagnostics, and run through the live issue engine on the
  bag's clock with the profile / `--expect` expectations. One-shot topics
  (a static TF, a latched map) are never reported stale.

### Changed
- `analysis.analyze()` takes an optional `now` (the analysis clock), and the
  profile → thresholds merge is shared as `profile.apply_profile()`.

## [0.5.0] — measure it — 2026-10-11

### Added
- **Lifecycle (managed) node states** — nodes serving `<node>/get_state` are
  detected from the graph's service list and their state is read with a
  non-blocking `lifecycle_msgs/srv/GetState` query every 2 s
  (`agent --no-lifecycle` to disable). A new `lifecycle_not_active` issue fires
  for `unconfigured` / `inactive` / `finalized` / `errorprocessing` nodes —
  critical when the node's outputs have subscribers waiting or a transition
  failed — with the `ros2 lifecycle set` command to fix it. Exposed as
  `nodes[].lifecycle_state`, a dashed graph node with the state, an Inspector
  row, and a "Lifecycle nodes" briefing section.
- **Turn-key Tier C tracing** — `rgd trace convert <session> --out run.ndjson`
  reads a `ros2 trace` CTF session with babeltrace2 (`python3-bt2`, optional,
  imported lazily), resolves each callback to its node and topic per process
  from the registration events (subscription / timer / service chains), pairs
  `callback_start`/`callback_end` into durations, and reports how many
  invocations it could attribute plus the slowest callbacks.
  `agent --trace-file` now also takes the session directory directly. CI
  installs `python3-bt2` and round-trips a real CTF trace written by
  babeltrace2's `sink.ctf.fs`.
- **`rgd assert`** — a CI health gate that needs no baseline recording: observe
  a live agent (or read a recording / the demo) and check a contract per
  snapshot — no critical (or degraded) verdict, `--expect TOPIC=HZ` rates,
  `--forbid` issue kinds, `--require-node` / `--require-topic` — with
  `--settle` warm-up, a `--max-breach` tolerance, Markdown / `--json` /
  `--junit` output, `--record` to keep the window, and exit codes
  0 pass / 1 fail / 2 no samples.
- **Distribution** — PyPI packaging (`pip install "ros_graph_debugger[web]"`
  for the offline CLI and web replay; only `rgd` goes on the PATH), a
  `Dockerfile` for the live agent (`docker run --net=host --pid=host
  ghcr.io/rsasaki0109/ros_graph_debugger`), a tag-triggered release workflow
  (PyPI trusted publishing + GHCR jazzy/humble images), CI jobs that build and
  smoke-test the wheel and the image, and [docs/releasing.md](docs/releasing.md).

- **Topic statistics (latency Tier B)** — the agent reads rclcpp topic
  statistics (`statistics_msgs/MetricsMessage`, `/statistics` by default;
  `--statistics-topic STATS[=TOPIC]`, `--no-statistics`) and overlays the
  subscriber-observed rate and message age on topics it does not probe, so
  rate/freshness rules (and the bottleneck inference) also cover large sensor
  topics. Node→topic attribution is explicit (`single_sub` / `explicit`, else
  left unattributed), shown as `stats` in the UI and briefing, and exposed via
  `topics[].rate_source`, snapshot `statistics`, `GET /api/v1/statistics`, and
  the `get_statistics` MCP tool.

### Changed
- `rate_drop`, `stale_data` and the bottleneck inference now consider any
  measured topic (`rate_source` `probe` or `statistics`), not only probed ones.
- Bundled web UI and profiles are also found under the pip install prefix
  (`<prefix>/share/ros_graph_debugger`) when there is no ament index.

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
