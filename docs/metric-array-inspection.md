# Inspect public processing MetricArray dumps

`rgd metrics inspect` reads a MetricArray-shaped JSON object/list or a
multi-document ROS echo YAML dump and emits inspection JSON. It works without
ROS, a live agent or message bindings. JSON input uses only the standard
library; YAML needs PyYAML from the existing `web` extra, or a sourced ROS
development environment. No new required dependency is introduced.

```bash
rgd metrics inspect processing_time_metrics.txt \
  --topic /system/processing_time_checker/metrics --out metrics.inspect.json
# An existing dump in JSON needs no optional YAML dependency:
rgd metrics inspect metrics.json
```

Input JSON can be one message or a list of messages. YAML accepts consecutive
messages separated by `---`, including the trailing separator from `ros2 topic
echo`. Format selection is automatic for `.json` files and JSON-looking input;
use `--input-format yaml` for YAML flow syntax beginning with `{` or `[`. An
empty array is a message with no metric entries; an empty input is an error.

Each entry preserves its name, unit, original string value and position.
`processing_time/<module>` values with the exact unit `millisecond` become
`processing_time_ms` only when numeric, finite and non-negative. Zero is valid.
NaN, infinities, negative durations, non-string values, unsupported units and
other metric names remain visible with an `unusable_duration` status and a
specific reason, never a fabricated zero. Entries with duplicate names remain
separate and are marked `duplicate_name`; duplicate JSON/YAML object fields
are an error because they would silently overwrite evidence.

Counts describe input messages and metric **observations**, including repeated
values. They are not unique source cycles. Each message retains a valid
array-level `stamp` without assigning it to any original source measurement.
Missing/invalid array stamps are explicitly identified. Inspection keeps:

- `measurement_scope: cached_metric_observation`
- `source_freshness: unknown`, `node_ownership: unknown`
- `source_measurement_count: null`
- Per-entry `node`, `source_topic`, `measurement_stamp`: `null`

`--topic` is recorded as user-supplied context, not independently verified
publisher or source-topic identity. No callbacks, p95/mean statistics, graph
nodes, stage budgets, issues or health verdict are produced. Exit 0 means the
dump was inspected, including any unusable values; it does not mean a healthy
ROS system. Malformed/unreadable input or output failures exit 2. This inspection
format is separate from `.rgd.json` recordings and is not input to `rgd assert`.

## Why ownership and freshness remain unknown

The [public DLR v2 example](https://github.com/tier4/driving_log_replayer_v2/blob/38d08c178663a28a558158797fa323544de53602/sample/planning_control/processing_time_metrics.txt)
contains 44 metrics, all in milliseconds, with labels such as
`processing_time/lateral`. This is one text message, not a time series or a
fault-labelled bag. Its original producing executable/version/configuration
is not supplied by the example.

The separately pinned [Autoware checker source](https://github.com/autowarefoundation/autoware_universe/blob/1258c0c4afe3c2cd6978aa17e2f2bc4c0c4ae3c1/system/autoware_processing_time_checker/src/processing_time_checker.cpp)
shortens source topic paths to module basenames, stores the last received value,
and republishes those values with a new array stamp. Its [default configuration](https://github.com/autowarefoundation/autoware_universe/blob/1258c0c4afe3c2cd6978aa17e2f2bc4c0c4ae3c1/system/autoware_processing_time_checker/config/processing_time_checker.param.yaml)
publishes at 10 Hz and includes controller submodules `lateral` and
`longitudinal`. Module labels do not prove graph-node identity; shortened names
can collide. A fresh array stamp does not prove that an original measurement
has changed, and identical values cannot distinguish new cycles from cached
republication. This current source audit does not recover the public example's
original deployment.

Prefer original scalar processing-time topics and independently verified
publishers for live graph attribution. MetricArray inspection is a first step
for examining published evidence; it does not add live or bag MetricArray
ingestion, infer a mapping, or treat aggregated observations as fresh callbacks.
Some processing topics also carry DiagnosticStatus maps, not scalar `data` or
MetricArray. A topic regex alone does not establish its schema or unit.

For a new deployment, obtain its actual metric-to-source mapping and publisher
identity before attribution. Individual source timestamps/update identifiers
are needed to establish source freshness or unique cycle counts. Missing
information stays unknown.
