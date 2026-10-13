# Pipeline paths and shared ROS buses

The path API, Markdown briefings, MCP tool and recording reports select a
representative application data-flow branch through the requested node or
topic. The lowest observed rate selects each ordinary branch. Querying a
specific topic requires a connected hop through that topic; an unrelated
lower-rate output cannot replace the requested branch.

Shared ROS buses can connect most nodes in a graph without establishing that
one node's application output feeds another. Default paths exclude standard
ParameterEvent, Log, Clock, DiagnosticArray, MetricsMessage and TFMessage types.
This also handles remapped and federated topics. Legacy missing-type snapshots
use canonical control topic names, including recorded federation host prefixes;
custom declared types and unidentified noncanonical streams stay eligible.

```bash
# Default application path through a node.
curl 'http://localhost:3939/api/v1/path?target=%2Fgyro_odometer'

# Explicitly inspect the clock bus.
curl 'http://localhost:3939/api/v1/path?target=%2Fclock'
```

The full graph, topic observations and issue evidence are preserved. A node
connected only to control buses has no default data path; the API returns 404
when the requested node/topic has no connected eligible path. Explicit topic
queries can still inspect control buses. No route or existing snapshot field
was removed or renamed.

## Observed failure and correction

Actual Jazzy Autoware gyro/vehicle conversion recordings with the public DLR
input bag exposed an incorrect path containing `/clock` and `/parameter_events`,
repeating the observer and player and ending at a TF listener. Those edges
describe real ROS graph connections, but treating them as application data
dependencies produced a misleading pipeline route.

Reanalysis of the unchanged normal, intentionally missing-IMU and recovered
recordings selected these representative paths:

| Recording | Default path |
|---|---|
| Normal input | player → gyro → independent observer, through the IMU topic |
| Missing IMU | player → vehicle converter → gyro → independent observer |
| Recovered | player → gyro → independent observer, through the IMU topic |

The missing-input recording still contains `diagnostics_error`, `no_publisher`
and `topic_stale` evidence. Normal/recovered recordings retain their static-only
TF listener's factual `/tf` missing-publisher warning. No snapshot issue is
suppressed to make the selected route appear healthy.

All three frames were served through the existing replay server. Native HTTP
and actual stdio MCP `get_pipeline_path` results matched the same path exactly;
`get_runtime_snapshot` matched the original frame, including its issues.
Explicit `/clock` inspection retained a clock hop, and CLI reports matched
the timeline renderer. Original recording hashes and checks are recorded in
[pipeline-control-results.json](pipeline-control-results.json). Raw recordings,
responses and logs are retained outside the repository. This is transport and
path-selection validation, with no model invoked.

## Interpretation limits

A selected branch does not enumerate every required input. The public gyro
node requires both IMU and vehicle velocity; choosing its IMU branch does not
make the vehicle input optional. Likewise, graph edges do not prove callback
ordering, delivery completeness or causal delay. `bottleneck_topic` retains its
existing name and identifies the lowest observed rate on the selected path.
During silence, a live probe can retain its earlier receipt-window rate; inspect
staleness, diagnostics and receipt evidence before inferring fresh throughput.

Control types define the default selection policy, not the authenticity or
correctness of imported metadata. Explicitly select a control topic when that
bus itself is the object of the investigation.
