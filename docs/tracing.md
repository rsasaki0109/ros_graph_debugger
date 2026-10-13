# Tier C: feeding real callback traces

`ros_graph_debugger` overlays per-callback execution time (count / mean / p95 /
max) on the graph and raises a `slow_callback` issue when a callback exceeds its
(stage-aware) budget. The `--demo` shows this with a synthetic source; this page
is how to feed **real** data from [`ros2_tracing`](https://github.com/ros2/ros2_tracing).

## The interchange format

The agent reads an **NDJSON** file — one JSON object per **callback
invocation**:

```json
{"node": "/perception/detector", "callback": "sub /image_raw", "topic": "/image_raw", "duration_ms": 18.4, "source": "trace"}
{"node": "/perception/detector", "callback": "sub /image_raw", "topic": "/image_raw", "duration_ms": 21.0, "source": "trace"}
```

- `node` (required) — fully-qualified node name.
- `duration_ms` (required) — duration for this observation in milliseconds;
  paired traces measure wall time between callback start and end.
- `topic` (optional) — the subscription topic; `callback` defaults to
  `sub <topic>` when omitted.
- `callback` (optional) — a human label (e.g. `timer`).
- `source` (optional) — `trace`, `application_report`, `synthetic`, or `null`
  (unknown). The CTF converter writes `trace` after pairing callback start/end
  events. Processing-time subscriptions and bag analysis write
  `application_report`; scripted demo callbacks write `synthetic`. Imported
  NDJSON source is declared metadata, not authenticated provenance. Old rows
  without it remain unknown; neither the filename nor a `processing_time`
  label establishes the source.

Load it with:

```bash
ros2 run ros_graph_debugger agent --profile autoware --trace-file run.ndjson
```

The agent aggregates rows by `(node, callback, topic, source)` into count/mean/p95/max
(`tracing.aggregate_callback_durations`) and the issue engine checks each p95
against the profile's `max_callback_ms` budget.

Snapshots retain all existing callback fields and add `source`. The existing
`slow_callback` kind, severity and budgets are unchanged; its evidence now
identifies the duration source. An application report follows the publisher's
definition of processing time and may cover a cycle or several callbacks. It
does not establish callback start/end time or explain downstream rate loss.

MCP `get_callbacks`, Markdown briefings and Markdown/HTML reports expose the
source. Reports keep different sources/topics in separate series, and `rgd diff`
only compares callback maxima with matching node, label, topic and source.
Changed source sets appear as `callback_incomparable` in the diff JSON and as
comparison limits in Markdown. Unknown-to-unknown comparisons retain the legacy
behavior but do not establish identical measurement methods.
Legacy recordings remain readable and their missing source stays unknown;
unrecognized source values are displayed as unknown. Pipeline numeric fields
`cb_p95_ms` are retained and accompanied by `cb_source` (a per-sample array in
timelines). The existing subscription-first, processing-time fallback applies.
For several subscription observations the largest p95 is selected, not summed;
equal maxima from different sources have unknown attribution. Independent p95s
are not an end-to-end execution time.

## Capturing a trace

```bash
# needs an LTTng-enabled tracetools build (ros2_tracing)
ros2 trace -s rgd_session        # default events include every ros2:* tracepoint
# ... now launch your system, exercise it, then stop the trace (Enter / Ctrl-C)
```

Start the trace **before** launching the nodes. Attribution comes from the
registration events emitted while nodes start up (`rcl_node_init`,
`rcl_subscription_init`, `rclcpp_subscription_init`,
`rclcpp_*_callback_added`, …); a node that was already running when tracing
began produces `callback_start`/`callback_end` pairs nobody can attribute. If
you narrow the events with `-e`, keep all of those plus `ros2:callback_start`
and `ros2:callback_end`.

## Converting — turn-key

```bash
sudo apt install python3-bt2      # babeltrace2 Python bindings (system python3)

# either convert once and load the NDJSON...
ros2 run ros_graph_debugger rgd trace convert ~/.ros/tracing/rgd_session --out run.ndjson
ros2 run ros_graph_debugger agent --profile autoware --trace-file run.ndjson

# ...or hand the session directory to the agent directly
ros2 run ros_graph_debugger agent --profile autoware --trace-file ~/.ros/tracing/rgd_session
```

`rgd trace convert` reads the CTF with babeltrace2 (`trace_convert.py`),
resolves every callback to its node and topic, pairs `callback_start` /
`callback_end` into durations, writes one NDJSON row per invocation, and prints
how much of the trace it could attribute plus the slowest callbacks, for
example:

```
wrote 18342 callback invocations to run.ndjson (41 callbacks resolved, 0 invocations without a resolvable owner)
slowest callbacks (p95):
     212.4 ms  /perception/detector  sub /sensing/camera/image_raw  (n=903; paired callback trace)
      14.9 ms  /control/controller  timer 33.3333 ms  (n=2701; paired callback trace)
```

What is resolved, per process (handles are pointers, so everything is keyed by
`(vpid, handle)`):

| callback | chain | label |
|---|---|---|
| subscription | `rcl_node_init` ← `rcl_subscription_init` ← `rclcpp_subscription_init` ← `rclcpp_subscription_callback_added` | `sub <topic>` |
| timer | `rcl_timer_init` + `rclcpp_timer_link_node` + `rclcpp_timer_callback_added` | `timer <period> ms` |
| service | `rcl_service_init` + `rclcpp_service_callback_added` | `srv <service>` |

Invocations whose chain is incomplete are counted as *without a resolvable
owner* and dropped, never guessed. These are the rclcpp tracepoints; callbacks
from client libraries that don't emit them are dropped the same way.

The pure half (owner resolution, pairing, aggregation) is in `tracing.py` and
unit-tested without babeltrace2 (`test/test_trace_convert.py`); the same test
file also writes a real CTF trace with babeltrace2's `sink.ctf.fs` and reads
it back through `rgd trace convert` when `python3-bt2` is installed (as in CI).

The separate `trace-integration` CI job also captures real LTTng events from
subscription, timer and service callbacks in a C++ fixture on Humble and Jazzy.
It checks attribution and both slow/healthy diagnosis through the CLI and live
agent, without synthetic CTF events. Humble needs a test overlay of its official
`tracetools` 4.1.2 source because the distro binary has no LTTng backend; Jazzy
uses the distro library. See [CONTRIBUTING.md](../CONTRIBUTING.md#real-lttng-integration)
for the commands and diagnostic artifacts. This does not validate callbacks
from rclpy or constitute an Autoware/Nav2 system test.

## Reported CTF data loss

`rgd trace convert` rejects a CTF trace when babeltrace2 reports discarded
**events or packets**, including a notification whose count is unknown. It
exits 2 before writing callback rows; an existing `--out` file is preserved,
so check the exit status before consuming that path as a new result.
`agent --trace-file <session>` also exits 2 before starting ROS or its HTTP
server. Incomplete callback populations can bias count/mean/p95/max and must
not become ordinary healthy-looking statistics.

The error identifies the first notification's unit and reported count. Try
larger LTTng sub-buffers or fewer enabled events, then make a fresh capture.
There is no option to ignore the notification. This does not establish that a
trace without notifications is complete: events outside the capture window,
unmatched start/end events, missing registration chains and already-exported
NDJSON have separate limits. NDJSON contains no original CTF loss metadata.

Humble and Jazzy CI exercise real rclcpp timer callbacks in counted normal
captures and both discard-mode event loss and overwrite-mode packet loss.
The packet test drains and verifies 500 warm callbacks before the burst, then
requires all 5,500 callbacks in its normal control and a real discarded-packet
notification in its loss capture. Both CLI conversion and agent startup must
reject that capture, preserving previous output. See the
[real LTTng integration commands](../CONTRIBUTING.md#real-lttng-integration).

## Converting — other tools

Anything that emits one `{"node", "topic", "callback"?, "duration_ms"}` object
per invocation works with `--trace-file`. With `tracetools_analysis`, for
example:

```python
from tracetools_analysis.loading import load_file
from tracetools_analysis.processor import Processor
from tracetools_analysis.processor.ros2 import Ros2Handler
from tracetools_analysis.utils.ros2 import Ros2DataModelUtil
import json

events = load_file('~/.ros/tracing/rgd_session')
handler = Ros2Handler()
Processor(handler).process(events)
util = Ros2DataModelUtil(handler.data)

with open('run.ndjson', 'w') as f:
    for cb_obj, df in util.get_callback_durations().items():   # symbol -> durations
        owner = util.get_callback_owner_info(cb_obj) or {}
        node, topic = owner.get('node_name', ''), owner.get('topic_name', '')
        for dur_ms in df['duration'] * 1e-6:                   # ns -> ms
            f.write(json.dumps({'node': node, 'topic': topic,
                                'duration_ms': round(float(dur_ms), 3)}) + '\n')
```

> API names vary across `tracetools_analysis` versions — adjust the owner /
> duration accessors to yours.
