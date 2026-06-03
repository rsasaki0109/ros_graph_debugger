# HTTP API

The agent serves on `http://127.0.0.1:3939` by default. All payloads are JSON
unless noted. The same surface is served in replay mode (`rgd serve`), except
`/api/v1/config` returns `{}` and `/api/v1/replay/*` becomes active.

> This list is kept in sync with the code by `test/test_docs_api.py`, which
> fails if a route is added/removed without updating this file.

## REST

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/health` | liveness + version |
| GET | `/api/v1/snapshot` | full `GraphSnapshot` (nodes, topics, edges, tf_edges, diagnostics, issues) |
| GET | `/api/v1/snapshot.md` | the same snapshot as an AI-friendly Markdown briefing (text/plain) |
| GET | `/api/v1/snapshot.md?focus=TARGET` | a briefing sliced to one node or topic + its 1-hop neighbourhood (`TARGET` = node id/name, topic name, or suffix) |
| GET | `/api/v1/graph` | nodes + topics + edges only |
| GET | `/api/v1/nodes` | node list with process metrics |
| GET | `/api/v1/topics` | topic list with rate/bandwidth/size/age/QoS |
| GET | `/api/v1/tf` | TF edges with staleness |
| GET | `/api/v1/diagnostics` | latest `/diagnostics` statuses |
| GET | `/api/v1/callbacks` | per-callback execution-time stats (Tier C tracing; `[]` when no trace source) |
| GET | `/api/v1/issues` | detected issues (sorted by severity) |
| GET | `/api/v1/summary` | one-line health rollup (`{verdict: ok\|degraded\|critical, counts, headline, issue_count}`) |
| GET | `/api/v1/path?target=TARGET` | the constraining source→sink pipeline path through a node/topic (`{target, pivot, nodes, hops, bottleneck_topic, cb_bottleneck_node}`; each hop carries `rate_hz` + the consumer's `cb_p95_ms`; 404 if no connected path) |
| GET | `/api/v1/profile` | active profile name + stage groups (UI grouping) |
| GET | `/api/v1/config` | current thresholds + expectations (live mode only; `{}` in replay) |
| POST | `/api/v1/config` | merge a partial config into the live thresholds |
| GET | `/api/v1/replay` | replay state `{mode, index, total, playing, loop}` (`{mode:"live"}` when not replaying) |
| POST | `/api/v1/replay/seek?index=N` | jump to a frame (pauses playback) |
| POST | `/api/v1/replay/play?playing=true|false` | resume/pause playback |

## WebSocket

| Path | Description |
|---|---|
| `/api/v1/stream` | pushes a full `GraphSnapshot` JSON every ~1s (every ~0.25s in replay) |

## POST /api/v1/config

Body is a partial object; only recognized keys are applied, the rest ignored.

```json
{
  "high_cpu_percent": 80,
  "high_bandwidth_bps": 50000000,
  "stale_topic_ms": 2000,
  "tf_stale_ms": 1000,
  "slow_callback_ms": 100,
  "expected_min_rate": { "/perception/.../objects": 10 },
  "expected_max_age_ms": { "/localization/kinematic_state": 100 },
  "expected_callback_ms": { "/control/command/control_cmd": 10 },
  "min_rate_patterns": [["^/control/.*", 10]],
  "max_age_patterns":  [["^/localization/.*", 200]],
  "callback_ms_patterns": [["^/control/.*", 15], ["^/planning/.*", 200]]
}
```

Returns `{"changed": { ... }}`. `expected_*` maps are merged; `*_patterns` are
replaced. Expectations are resolved exact-topic first, then first matching
pattern.

## Snapshot shape (abridged)

```jsonc
{
  "timestamp": 1234567890.1,
  "profile": "autoware",
  "nodes":   [{ "id", "name", "publishers", "subscribers", "cpu_percent",
                "rss_bytes", "process_mapping_confidence", "status" }],
  "topics":  [{ "name", "type", "publisher_count", "subscriber_count",
                "rate_hz", "bandwidth_bps", "p95_msg_size_bytes",
                "header_age_p95_ms", "qos_status", "status" }],
  "edges":   [{ "from_node", "to_node", "topic", "status" }],
  "tf_edges":[{ "parent", "child", "age_ms", "status" }],
  "callbacks":[{ "node", "callback", "topic", "count",
                 "mean_ms", "p95_ms", "max_ms" }],
  "issues":  [{ "severity", "kind", "title", "explanation",
                "evidence", "suggested_actions",
                "related_nodes", "related_topics", "related_frames" }]
}
```

## MCP

`python -m ros_graph_debugger.mcp_server` exposes the same data to AI assistants
as tools. Read tools: `get_runtime_briefing` (`/snapshot.md`),
`get_node_briefing(target)` (`/snapshot.md?focus=TARGET` — a focused briefing
for one node or topic in a large graph), `get_pipeline_path(target)`
(`/path?target=TARGET` — the constraining source→sink route), `get_issues`,
`get_graph`, `get_topics`,
`get_nodes`, `get_tf`, `get_diagnostics`, `get_callbacks`, `get_config`, plus
`health`. Write
tool: `set_expected_rate(topic, min_hz)`
posts to `/config` so an AI can set a topic's expected-rate floor at runtime.
It is a thin client of the REST API; point it at a non-default agent with
`RGD_BASE`.
