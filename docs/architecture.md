# Architecture

ROS Graph Debugger is one rclpy node that collects runtime state into a
thread-safe store, plus a FastAPI app that serves that store to a no-build web
UI and to AI tools. Nothing in the target system is modified.

```
  Browser UI (Cytoscape, no build step)        AI tools (curl / MCP)
        │ WebSocket / REST  :3939                     │
        ▼                                             ▼
  ┌─────────────────────────── FastAPI app (server.py) ───────────────────────┐
  │  REST + WebSocket + Markdown briefing + live config + replay control       │
  └───────────────▲───────────────────────────────────────────────────────────┘
                  │ reads snapshots / mutates thresholds
        ┌─────────┴──────────── RuntimeGraphStore (model.py) ─────────────────┐
        │  thread-safe: nodes, topics, tf, diagnostics, issues                │
        └─────────▲───────────────────────────────────────────────────────────┘
                  │ writes (spin thread)
  ┌───────────────┴──────────── DebuggerNode (node.py) ───────────────────────┐
  │  timers: graph poll · process poll · metrics+analyze                       │
  │  subs:   /tf · /tf_static · /diagnostics · opt-in topic probes (raw)       │
  │  collectors → store ; analysis.analyze() → issues                          │
  └───────────────▲───────────────────────────────────────────────────────────┘
                  │ ROS 2 graph API / subscriptions
            ROS 2 runtime (Autoware / Nav2 / your nodes)
```

## Threading model

- **Spin thread**: a `MultiThreadedExecutor` runs `DebuggerNode`. All collectors
  and the issue engine run here, on timers and subscription callbacks.
- **Main thread**: `uvicorn` runs the FastAPI app (asyncio).
- The two communicate **only** through `RuntimeGraphStore`, whose every accessor
  takes a lock. Writers replace whole fields; readers get a consistent snapshot.
- `Thresholds` (config) is shared by reference: `POST /api/v1/config` mutates it
  on the asyncio thread; the issue engine reads it on the spin thread. Writes
  replace whole values (GIL-atomic), never mutate mid-read.

## Modules

| Module | Responsibility |
|---|---|
| `model.py` | dataclasses + `RuntimeGraphStore` (thread-safe) |
| `node.py` | the rclpy node: collectors, probing, timers |
| `graph_build.py` | reconstruct nodes/topics from **topic endpoint info** (robust to node-discovery lag) |
| `qos_utils.py` | QoS enum→string + mismatch detection |
| `msgutil.py` | `header.stamp` age helper (latency Tier A) |
| `analysis.py` | rule-based issue engine + bottleneck inference |
| `pipeline.py` | focus resolution + constraining source→sink path tracer (rclpy-free) |
| `tracing.py` | Tier C callback-duration shape + synthetic source (live LTTng adapter is future) |
| `health.py` | one-line system verdict (ok/degraded/critical) from the issue list |
| `procmap.py` | node→process attribution with layered confidence (rclpy-free, unit-tested) |
| `config.py` | `ProbeConfig` / `Thresholds` (rclpy-free), pattern expectations, live apply |
| `profile.py` / `paths.py` | profile loading + asset discovery |
| `server.py` | FastAPI REST/WS, Markdown, config, replay |
| `markdown.py` | AI-friendly snapshot briefing |
| `recording.py` / `report.py` | NDJSON capture + HTML/Markdown reports |
| `replay.py` | `ReplayStore` + scripted demo (drop-in for the store) |
| `agent.py` / `cli.py` | entry points (`agent`, `rgd`) |

## Why nodes are built from endpoints

`get_node_names_and_namespaces()` can lag or come up empty while topics are
already visible (DDS propagates topic endpoint info more reliably than the
node-name graph; Autoware/Nav2 hit this routinely). `graph_build.build_graph()`
therefore reconstructs the node set from `get_publishers_info_by_topic()` /
`get_subscriptions_info_by_topic()`, which also carry per-endpoint QoS.

## Data flow per cycle

1. Graph poll (1 Hz) → `build_graph()` → `store.set_graph()`; start probes for
   newly-seen, in-policy topics.
2. Probe callbacks record size + (best-effort) `header.stamp` age.
3. Process poll (0.5 Hz) maps nodes→PIDs best-effort (honest confidence).
4. Metrics+analyze (1 Hz): fold probe metrics in, snapshot TF/diagnostics, run
   `analyze()` → `store.set_issues()`.
5. The web server streams `store.snapshot()` over the WebSocket.

See [api.md](api.md) for the HTTP surface and [performance_safety.md](performance_safety.md)
for the probing policy.
