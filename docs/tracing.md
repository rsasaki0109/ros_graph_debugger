# Tier C: feeding real callback traces

`ros_graph_debugger` overlays per-callback execution time (count / mean / p95 /
max) on the graph and raises a `slow_callback` issue when a callback exceeds its
(stage-aware) budget. The `--demo` shows this with a synthetic source; this page
is how to feed **real** data from [`ros2_tracing`](https://github.com/ros2/ros2_tracing).

## The interchange format

The agent reads an **NDJSON** file — one JSON object per **callback
invocation**:

```json
{"node": "/perception/detector", "callback": "sub /image_raw", "topic": "/image_raw", "duration_ms": 18.4}
{"node": "/perception/detector", "callback": "sub /image_raw", "topic": "/image_raw", "duration_ms": 21.0}
```

- `node` (required) — fully-qualified node name.
- `duration_ms` (required) — wall time inside the callback for this invocation.
- `topic` (optional) — the subscription topic; `callback` defaults to
  `sub <topic>` when omitted.
- `callback` (optional) — a human label (e.g. `timer`).

Load it with:

```bash
ros2 run ros_graph_debugger agent --profile autoware --trace-file run.ndjson
```

The agent aggregates rows by `(node, callback, topic)` into count/mean/p95/max
(`tracing.aggregate_callback_durations`) and the issue engine checks each p95
against the profile's `max_callback_ms` budget.

## Capturing a trace

```bash
# 1. record (needs an LTTng-enabled tracetools build)
ros2 trace -s rgd_session -e ros2:callback_start ros2:callback_end \
  ros2:rclcpp_subscription_init ros2:rcl_node_init
# ... run your system, then Ctrl-C the trace ...
```

## Converting to NDJSON

**Recommended — `tracetools_analysis`** (it resolves the
callback→subscription→node/topic chain for you):

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
> duration accessors to yours. The point is to emit one `{node, topic,
> duration_ms}` per invocation.

**Lower level — raw babeltrace2.** If you read the CTF stream directly with the
`bt2` bindings, the pairing and attribution logic is already in the package and
unit-tested, so you only write the bt2 extraction:

```python
import bt2, json
from ros_graph_debugger.tracing import pair_callback_events, rows_with_owners

events, owners = [], {}   # owners: {callback_handle: {node, topic, callback}}
for msg in bt2.TraceCollectionMessageIterator('~/.ros/tracing/rgd_session'):
    if type(msg) is not bt2._EventMessageConst:
        continue
    ev, t_ns = msg.event, msg.default_clock_snapshot.ns_from_origin
    if ev.name == 'ros2:callback_start':
        events.append({'kind': 'start', 'handle': int(ev['callback']), 't_ns': t_ns})
    elif ev.name == 'ros2:callback_end':
        events.append({'kind': 'end', 'handle': int(ev['callback']), 't_ns': t_ns})
    # ... populate `owners` from rclcpp_subscription_callback_added /
    #     rcl_subscription_init / rcl_node_init (the callback->node/topic chain).

rows = rows_with_owners(pair_callback_events(events), owners)
with open('run.ndjson', 'w') as f:
    for r in rows:
        f.write(json.dumps(r) + '\n')
```

`pair_callback_events` matches `callback_start`/`callback_end` per handle (with a
stack for reentrancy) and `rows_with_owners` attaches `{node, callback, topic}`,
dropping handles with no resolved owner — see `test/test_tracing.py`.
