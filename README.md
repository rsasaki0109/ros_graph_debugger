# ROS Graph Debugger

[![CI](https://github.com/rsasaki0109/ros_graph_debugger/actions/workflows/ci.yml/badge.svg)](https://github.com/rsasaki0109/ros_graph_debugger/actions/workflows/ci.yml)
[![ROS 2](https://img.shields.io/badge/ROS%202-Humble%20%7C%20Jazzy-blue)](https://docs.ros.org)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

**Runtime DevTools for ROS 2.** A live, AI-friendly view of your running ROS 2
system: the graph, topic rate / bandwidth / message size, QoS, TF freshness,
diagnostics — and an issue panel that tells you *where to look next*.

Not a replacement for `rqt_graph`. It overlays runtime metrics and bottleneck
detection on the graph, and exposes everything as a web view, JSON, Markdown,
and an MCP server so AI assistants can debug your robot with you.

> Works with **Autoware** and **Nav2** via profile packs. Built for **ROS 2
> Jazzy / Humble** on Ubuntu 24.04 / 22.04.

![demo placeholder](docs/demo.gif)

---

## 60-second quick start

```bash
# build
cd ~/your_ws && colcon build --packages-select ros_graph_debugger
source install/setup.bash

# run the agent (opens http://localhost:3939)
ros2 run ros_graph_debugger agent

# in another terminal, run the demo pipeline
ros2 run ros_graph_debugger demo_pipeline
```

### See it instantly — no robot, no ROS graph required

```bash
ros2 run ros_graph_debugger rgd serve --demo     # → http://localhost:3939
```

Replays a scripted `camera → detector → … → controller` session in the real web
UI: the detector stalls in the middle, its output topic and node turn red, the
bottleneck issue appears, and `map → base_link` goes stale — then recovers. Use
the timeline at the bottom to scrub through it. This needs no DDS, so it's the
fastest way to try the tool (and to record a demo GIF). Replay any captured
session the same way: `rgd serve run.rgd.json`.

---

Open <http://localhost:3939>. With a live system you'll see:

```
camera → /sensing/camera/image_raw → detector → /perception/.../objects → tracker → planner → controller
```

The detector periodically enters a "slow" phase. Watch its output topic turn
**red**, the node turn red, and an issue appear:

> **[CRITICAL] Likely bottleneck: detector**
> detector output /perception/object_recognition/objects dropped below
> expectation while its inputs look healthy and it is CPU-bound.
> - Evidence: /perception/object_recognition/objects: 4.4 Hz (expected >= 10.0);
>   detector CPU: 95%; /sensing/camera/image_raw: 30.0 Hz

Run with the Autoware profile to get expectations and pipeline grouping:

```bash
ros2 run ros_graph_debugger agent --profile autoware
```

---

## Why

ROS 2 debugging is fragmented across `rqt_graph`, `ros2 topic hz`,
`ros2 topic bw`, `ros2 topic echo`, `ros2 doctor`, TF tools, `/diagnostics`,
and `htop`. Finding "why is my pipeline slow" means bouncing between all of them.

ROS Graph Debugger puts graph, metrics, QoS, TF, diagnostics, and bottleneck
detection into one live view — and one Markdown briefing you can hand to an AI.

## Features (v0.1)

- **Live ROS graph** with auto layout (pub → topic → sub).
- **Topic metrics**: rate, bandwidth, avg / p95 message size (opt-in probing).
- **QoS mismatch detection** — the classic "connected but no data flows" trap.
- **Node CPU / memory**, with honest node→process mapping confidence.
- **TF freshness** — stale transform detection.
- **/diagnostics** ingestion (WARN / ERROR become issues).
- **Issue panel**: each issue has a plain-English explanation, evidence, and
  suggested actions, ranked by severity.
- **Profiles**: `autoware`, `nav2`, `moveit` (grouping + expected rates).

## AI-friendly by design

The whole runtime state is available in three machine-friendly ways:

| What | Endpoint | Use |
|---|---|---|
| Structured JSON | `GET /api/v1/snapshot` | programmatic access |
| **Markdown briefing** | `GET /api/v1/snapshot.md` | paste into an LLM / agent |
| **MCP server** | `python -m ros_graph_debugger.mcp_server` | let Claude query the live graph |

```bash
# grab an AI-ready briefing from anywhere
curl http://localhost:3939/api/v1/snapshot.md

# or via the CLI
rgd markdown
```

Register the MCP server with Claude Code:

```bash
pip install "mcp[cli]"
claude mcp add ros-graph -- python -m ros_graph_debugger.mcp_server
```

Now an AI assistant can call `get_runtime_briefing`, `get_issues`, `get_graph`,
and reason about your robot's runtime.

## Safety: probing is opt-in and bounded

The graph, QoS, TF, and diagnostics are collected **passively** (no data
subscriptions). Message-rate probing uses lightweight raw subscriptions and is
deliberately conservative:

- Large sensor topics (`Image`, `CompressedImage`, `PointCloud2`, `LaserScan`)
  are **never** probed automatically.
- At most `--max-probe-topics` (default 12) topics are probed.
- Narrow the scope explicitly with `--probe-topic`, `--probe-regex`,
  `--probe-large-topics`, or disable entirely with `--no-probe`.

```bash
ros2 run ros_graph_debugger agent \
  --probe-regex '^/perception/.*' --max-probe-topics 20
```

## CLI

```bash
ros2 run ros_graph_debugger agent [--profile autoware] [--port 3939] ...

# one-shot queries (rgd talks to a running agent over REST)
ros2 run ros_graph_debugger rgd snapshot --out snap.json
ros2 run ros_graph_debugger rgd markdown        # AI briefing to stdout
ros2 run ros_graph_debugger rgd issues          # list current issues
ros2 run ros_graph_debugger rgd doctor          # is the agent up?
```

### Record & report

Capture a window of runtime and turn it into a shareable report — ideal for bag
replay analysis and CI bottleneck checks (no live ROS needed to read it back):

```bash
# record 30s of snapshots (streams NDJSON to disk)
ros2 run ros_graph_debugger rgd record --out run.rgd.json --duration 30

# self-contained HTML report + AI-friendly Markdown
ros2 run ros_graph_debugger rgd report run.rgd.json --html report.html --md report.md

# or replay the captured session in the web UI with a time-scrubber
ros2 run ros_graph_debugger rgd serve run.rgd.json
```

The report ranks bottlenecks by severity and frequency, summarizes per-topic
rate/bandwidth, lists stale transforms, draws an issue timeline, and (with a
profile) shows per-stage engage-readiness as a share of the recording.

## How it works

```
  Browser UI (Cytoscape, no build step)
        │ WebSocket / REST  :3939
  ┌─────┴───────────────────────────────┐
  │ ros_graph_debugger agent (rclpy)     │
  │  collectors: graph, QoS, metrics,    │
  │  TF, diagnostics, process            │
  │  analysis: issue engine + bottleneck │
  │  api: FastAPI REST + WS + Markdown   │
  └─────┬───────────────────────────────┘
        │ ROS 2 graph API / subscriptions
   ROS 2 runtime (Autoware / Nav2 / your nodes)
```

A single rclpy node spins all collectors on a background thread and writes into
a thread-safe store; FastAPI serves the UI and streams snapshots. No target
node is modified.

## Comparison

| Tool | Strength | ROS Graph Debugger |
|---|---|---|
| `rqt_graph` | graph view | graph **+ runtime metrics + issues** |
| `ros2 topic hz/bw` | accurate, per-topic | unified across the whole graph |
| Foxglove | rich data visualization | causality graph + bottleneck diagnosis |
| PlotJuggler | timeseries analysis | shows *which* series to look at |
| `ros2_tracing` | low-level traces | (roadmap) traces in a DevTools timeline |

## Roadmap

- **v0.1** — live graph, topic metrics, QoS, TF, diagnostics, issues, profiles,
  AI Markdown + MCP.
- **v0.2** *(current)* — pipeline-stage grouping (stage colours + legend), an
  engage-readiness bar (per-stage OK/WARN/ERROR) for Autoware / Nav2,
  `rgd record` / `rgd report` (HTML + Markdown), and `rgd serve` time-scrub
  replay (incl. a no-ROS `--demo`). *Next:* expected-rate config UI, richer
  process mapping.
- **v0.3** — `ros2_tracing` adapter, callback/critical-path timeline, multi-host.

## License

Apache-2.0
