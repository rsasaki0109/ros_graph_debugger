# HTTP API

The agent serves on `http://127.0.0.1:3939` by default. All payloads are JSON
unless noted. The same surface is served in replay mode (`rgd serve`), except
`/api/v1/config` returns `{}`, `/api/v1/replay/*` becomes active, and
`/api/v1/recording` exposes the saved recording header.

> This list is kept in sync with the code by `test/test_docs_api.py`, which
> fails if a route is added/removed without updating this file.

## REST

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/health` | liveness + version |
| GET | `/api/v1/recording` | `{mode, recording_header}`: original replay header, including header-age clock provenance; header is `null` in live mode |
| GET | `/api/v1/snapshot` | full `GraphSnapshot` (nodes, topics, edges, tf_edges, diagnostics, issues) |
| GET | `/api/v1/snapshot.md` | the same snapshot as an AI-friendly Markdown briefing (text/plain) |
| GET | `/api/v1/snapshot.md?focus=TARGET` | a briefing sliced to one node or topic + its 1-hop neighbourhood (`TARGET` = node id/name, topic name, or suffix) |
| GET | `/api/v1/graph` | nodes + topics + edges only |
| GET | `/api/v1/nodes` | node list with process metrics |
| GET | `/api/v1/topics` | topic list with rate/bandwidth/size/age/QoS |
| GET | `/api/v1/tf` | TF edges with staleness |
| GET | `/api/v1/diagnostics` | latest `/diagnostics` statuses |
| GET | `/api/v1/callbacks` | duration stats with `source` (paired trace, application report, synthetic or unknown; `[]` when none) |
| GET | `/api/v1/statistics` | rclcpp topic statistics (Tier B) per subscriber node and metric, with the topic each was attributed to (`[]` when none) |
| GET | `/api/v1/issues` | detected issues (sorted by severity) |
| GET | `/api/v1/summary` | one-line health rollup (`{verdict: ok\|degraded\|critical, counts, headline, issue_count}`) |
| GET | `/api/v1/path?target=TARGET` | a representative data-flow path through a node/topic (`{target, pivot, nodes, hops, bottleneck_topic, cb_bottleneck_node}`; each hop carries `rate_hz` + the consumer's `cb_p95_ms`; shared ROS control buses are excluded unless their topic is explicitly selected; 404 if no connected path) |
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

## Recording provenance

`GET /api/v1/recording` returns the recording header used by `rgd serve`.
For a bag analysis this includes, for example:

```json
{
  "mode": "replay",
  "recording_header": {
    "rgd_recording": "1",
    "started": 1690184194.0,
    "interval": 1.0,
    "profile": null,
    "header_age_clock": {
      "requested": "auto",
      "selected": "ros",
      "clock_messages": 8809,
      "unreadable_clock_messages": 0
    }
  }
}
```

`selected: "ros"` means header ages use recorded ROS clock values; `"bag"`
means bag receive time. Arrival windows and receipt freshness still use bag
receive time. The message counts cover the analysis of the entire recording,
not the current replay frame; `clock_messages` includes the unreadable messages
counted separately by `unreadable_clock_messages`. Seeking or pausing does not
change provenance.
This endpoint exposes the original header, preserving extension fields.

Old and headerless recordings may lack `header_age_clock`; that means **unknown**,
not a receive-time default. Reports and replay briefings say so explicitly.
Live mode returns `{"mode":"live","recording_header":null}`; this endpoint
does not infer the live node's clock. Snapshot and WebSocket fields are unchanged.

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
  "nodes":   [{ "id", "name", "publishers", "subscribers", "services",
                "service_clients", "cpu_percent",
                "rss_bytes", "process_mapping_confidence", "lifecycle_state",
                "status" }],
  "topics":  [{ "name", "type", "publisher_count", "subscriber_count",
                "qos_endpoints": [{ "node", "endpoint_type", "reliability",
                  "durability", "history", "depth", "liveliness",
                  "deadline_ms", "liveliness_lease_ms" }],   // ms; null = infinite
                "rate_hz", "bandwidth_bps", "p95_msg_size_bytes",
                "header_age_p95_ms", "rate_source", "rate_measurement_scope",
                "probe_callback_delay_p95_ms", "probe_callback_delay_samples",
                "probe_callback_delay_window_samples",
                "probe_source_receipt_gap_p95_ms", "probe_source_receipt_gap_samples",
                "probe_source_receipt_gap_window_samples", "qos_status", "status" }],
  "edges":   [{ "from_node", "to_node", "topic", "status" }],
  "tf_edges":[{ "parent", "child", "age_ms", "status" }],
  "callbacks":[{ "node", "callback", "topic", "count",
                 "mean_ms", "p95_ms", "max_ms", "source" }],
  "services":[{ "name", "servers", "clients" }],   // parameter services omitted
  "actions": [{ "name", "servers", "clients" }],   // from <action>/_action/send_goal
  "statistics":[{ "node", "source", "topic", "attribution", "metric", "unit",
                  "stats_topic", "avg", "min", "max", "stddev", "count",
                  "window_stop" }],
  "issues":  [{ "severity", "kind", "title", "explanation",
                "evidence", "suggested_actions",
                "related_nodes", "related_topics", "related_frames" }]
}
```

## MCP

Pipeline paths follow application topics by default. Standard ParameterEvent,
Log, Clock, DiagnosticArray, MetricsMessage and TFMessage types identify shared
control/telemetry buses, including remapped and federated names. Legacy topics
without a declared type use canonical `/parameter_events`, `/rosout`, `/clock`,
`/diagnostics`, `/diagnostics_agg`, `/statistics`, `/tf` and `/tf_static` names
(with a recorded federation host prefix removed). A custom declared type on a
standard-looking name remains eligible application data.

Selecting a control topic explicitly, such as `target=/clock`, includes that
topic and requires a connected hop through it. A node connected only to control
buses has no default data path. All topics, endpoints, metrics and issues remain
in the full graph and snapshot. Paths choose a representative branch using
observed rates; they are not complete input dependency graphs or proof of causal
delay. A stale topic can retain its previous receipt-window rate.

`python -m ros_graph_debugger.mcp_server` exposes the same data to AI assistants
as tools. Read tools: `get_recording_metadata` (`/recording` — read alongside
`get_runtime_snapshot` to identify the recording's header-age clock),
`get_runtime_snapshot` (`/snapshot`), `get_runtime_briefing` (`/snapshot.md`),
`get_node_briefing(target)` (`/snapshot.md?focus=TARGET` — a focused briefing
for one node or topic in a large graph), `get_pipeline_path(target)`
(`/path?target=TARGET` — the constraining source→sink route), `get_issues`,
`get_graph`, `get_topics`,
`get_nodes`, `get_tf`, `get_diagnostics`, `get_callbacks`, `get_statistics`,
`get_config`, plus
`health`. Write
tool: `set_expected_rate(topic, min_hz)`
posts to `/config` so an AI can set a topic's expected-rate floor at runtime.
It is a thin client of the REST API; point it at a non-default agent with
`RGD_BASE`.


`callbacks[].source` is `trace` (paired start/end callback wall time),
`application_report` (publisher-defined processing time), `synthetic`, or `null`
(unknown). Imported NDJSON source is declared metadata, not authenticated
provenance. Missing legacy fields and unrecognized values stay unknown; a label
such as `processing_time` does not establish provenance. Existing duration
budgets and the `slow_callback` kind are retained. Pipeline hops add `cb_source`
beside the existing `cb_p95_ms`; timeline hops carry matching per-sample arrays.
See [tracing.md](tracing.md) for collection and reporting semantics.

`topics[].rate_measurement_scope` identifies where rate samples were observed:
`probe_receipt` for the agent's live subscription, `subscriber_statistics` for
rclcpp topic statistics, and `bag_recording` for messages stored in a bag.
`null` means unknown, including recordings created before this field existed.
The existing `rate_source` field remains compatible; bag analysis historically
sets it to `probe`. Use the new scope to distinguish a bag from a live probe.

These rates describe observed messages. Delivery completeness and publisher-side
throughput remain unknown: a low rate can reflect slow publishing, receiver
loss, queue pressure or observer work. `rate_drop` and `bottleneck` evidence
states the measurement scope and this limitation; their rate thresholds and
severity rules are unchanged. See [probe reception evidence](probe-reception-evidence.md).


Live probes also expose `probe_callback_delay_p95_ms`, the local system-time gap
from the middleware receive timestamp to Python callback entry. The bounded
window has `probe_callback_delay_window_samples` receipts, of which
`probe_callback_delay_samples` have usable timestamp pairs. The p95 is `null`
when the window is empty or any pair is missing, nonpositive, or future-dated.
A zero gap is a valid observation. These fields survive graph polling; bag and
subscriber-statistics measurements have no probe wait measurement.

This includes work before callback entry, such as taking/copying the message
and executor waiting, and excludes the probe callback body. It is independent
of ROS/simulation time, affected by system clock adjustments, and does not
measure publisher callback time or certify delivery. See
[probe reception evidence](probe-reception-evidence.md).

`probe_source_receipt_gap_p95_ms` is the p95 of the signed middleware receive
timestamp minus its source timestamp, in milliseconds. The associated
`probe_source_receipt_gap_samples` and `probe_source_receipt_gap_window_samples`
count usable pairs and receipts in the same bounded probe window. The p95 is
`null` for an empty window or any missing/nonpositive timestamp. Negative and
zero differences are valid observations. Source and receive clocks may belong
to different hosts: clock synchronization is unknown, and clock skew or clock
adjustments can affect either sign. This measures neither network latency nor
publisher callback execution. No delay threshold or health rule is added.
Older recordings, bag analysis and subscriber statistics do not supply this
probe measurement. Both probe gap measurements survive graph polling.
