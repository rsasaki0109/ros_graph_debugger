# Actual Autoware callback trace with public inputs

This experiment records an LTTng/ros2_tracing CTF trace while two **unchanged
official C++ Autoware components** process the public DLR AR-tag input bag.
It adds actual callback start/end evidence to the previously verified input
omission/recovery run. It does not reconstruct the original full Autoware stack
or validate localization accuracy.

## Producer and inputs

The gyro odometer and vehicle velocity converter use Autoware Core 1.9.0,
commit [`f25f83c632c1984ec276c894c41857d4abc0dad8`](https://github.com/autowarefoundation/autoware_core/tree/f25f83c632c1984ec276c894c41857d4abc0dad8).
The pinned dependency sources and local fingerprints are in
[autoware-callback-trace-results.json](autoware-callback-trace-results.json).
The verified Jazzy SDK uses ordinary DDS/SingleThreadedExecutor with
`ENABLE_AGNOCAST=0`; upstream source files are unchanged. The two components
have no `processing_time_ms` publisher, so these measurements are paired
callback traces, not application-reported cycle times.

The unmodified public DLR input SQLite hash is
`224683a07c61e21a0ae416d9fec80e558cffde76534f340b96492f6772a28797`.
It contains 1,735 IMU and 1,735 vehicle-velocity messages at about 30 Hz plus
two static transforms. It is the AWSIM-reference-containing DLR bag, distinct
from the physical AR-tag example without IMU. The experiment explicitly supplies
static-TF replay QoS and generates a 50 Hz replay clock; these controls do not
recover the original offered QoS or an original recorded clock.

The exact RGD producer is the pushed validation-only commit
`dbddd94b655183a5e539f518920cc76f72d94cb5`, tree
`6ba6f71c937d63b404c0085046a4f01e1be277ad`. It combines pending PRs #28–#31;
it is not main or an aggregate main-merge proposal. This documentation branch
is independent from current main #27. Its unchanged trace conversion modules
match the producer byte-for-byte; it does not claim to contain that optional
scenario or those production features.

## Capture and strict checks

The session daemon is started in a fresh `LTTNG_HOME`, with userspace tracing
only. Capture starts **before** constructing the C++ nodes, so registration
chains are present. The channel uses per-process buffers, four 65,536-byte
subbuffers and `ros2:*` events with `vpid` context. The SDK runner keeps the
original bag read-only and uses a fixed ROS domain (97).

The existing strict scenario keeps both components alive through normal input,
deliberate IMU omission and recovery. Readiness and transitions are polled.
The same 20 Hz output floor and independent delivery contracts are retained:
assertions exit 0/1/0, assertion-period receipts are 184/0/183, and all 482
total observed output stamps belong to the original inputs, are nondecreasing,
finite and in `base_link`. The recovery diff is improved. The static-only `/tf`
warning and recovered whole-graph DEGRADED state remain.

Capture stops after the scenario has shut down its nodes. Conversion uses the
existing `rgd trace convert`, followed by separate raw start/end accounting:

| Check | Observed |
|---|---:|
| `ros2:*` events | 83,762 |
| callback starts / ends / paired invocations | 6,590 / 6,590 / 6,590 |
| unresolved pairs / negative intervals | 0 / 0 |
| attributed / unattributed invocations | 6,590 / 0 |
| resolved registered callback handles | 87 |
| executed callback groups | 12 |
| discarded-event or discarded-packet notifications | none |
| CTF files / total bytes | 110 / 2,155,880 |

The three target subscription callbacks are required to have at least 100
invocations and `source = trace`. Node ownership follows the original
PID-scoped rcl/rclcpp subscription registration chains, rather than topic-name
guessing or a sole graph publisher. Component registration has namespace `/`,
gyro vpid 112 and converter vpid 144 within the producer container.

| Node / input | Count | Mean ms | p95 ms | Max ms |
|---|---:|---:|---:|---:|
| gyro / IMU | 488 | 0.1 | 0.2 | 1.3 |
| gyro / converted vehicle velocity | 735 | 0.0 | 0.0 | 0.1 |
| converter / vehicle velocity | 735 | 0.1 | 0.1 | 1.6 |

Per-invocation rows round to 0.001 ms and aggregate statistics to 0.1 ms.
Displayed zero means quantization, not zero work: no raw paired interval was
zero nanoseconds. These elapsed callback intervals can include scheduling or
waiting; they are not CPU time, application cycle time or end-to-end latency.
Counts mix all three phases; no per-phase clock alignment or causal attribution
is inferred. Complete CTF pair accounting does not prove DDS delivery completeness.

## Reproduce in the prepared environment

The retained cloud SDK and helpers make this experiment repeatable without
adding RGD dependencies. Preserve user changes and use a clean checkout before
switching to the exact producer. Never edit a running helper or overwrite an
existing experiment directory. This is a tested current-environment workflow,
not proof of a fresh OS/cloud restore or a public prebuilt image.

```bash
git fetch origin test/pending-pr-integration
rgd_trace_branch=$(git branch --show-current)
git switch --detach dbddd94b655183a5e539f518920cc76f72d94cb5
mkdir /workspace/autoware-callback-trace-reproduction
cp /workspace/autoware-callback-trace-validation/capture.py \
  /workspace/autoware-callback-trace-reproduction/capture.py
bash /workspace/ros-graph-debugger-setup/autoware-source-run.sh \
  /workspace/autoware-callback-trace-reproduction python3 /artifacts/capture.py
git switch "$rgd_trace_branch"
```

The retained `capture.py` has a 240-second guard and destroys its owned session
in `finally`; the nested original scenario has its own 180-second guard and
reaps its process groups/observer. Local scripts, their hashes, commands, raw
logs, the CTF file manifest and input audit remain under
`/workspace/autoware-callback-trace-validation`. They are also referenced by the
saved environment startup instructions. Raw bags and CTF files are outside Git
and are not uploaded or released by this change.

LTTng initially creates owner-only output. In this prepared environment the
container-created **public generated trace directory only** was made readable
before host inspection. The host runtime Python 3.12 does not import `bt2`;
conversion, pair audit and direct agent import were verified using the cached
Jazzy SDK's system Python. A bare host `python3.12` is not a verified CTF reader.
Missing tools or source gates are failures, not skips.

## Import and evidence transport

After capture, a separate otherwise-idle agent imports the CTF using
`--no-probe --profile autoware --trace-file <trace>`. Its callbacks equal all
12 converted groups exactly. HTTP and CLI snapshots, three recorded snapshots,
and actual stdio MCP `get_callbacks`, full snapshot, recording metadata and full
Markdown preserve the statistics exactly. Owned agent/replay groups are reaped.
The trace has already completed and the Autoware components are not running
during import. Import/recording timestamps therefore do not make these fresh
live measurements or validate current component health.

No model was invoked. There is no paired trace-off control, measured tracing
overhead, localization accuracy, original full-stack validation, per-phase
duration comparison, model accuracy, human investigation-time or adoption
measurement. Local file/archive hashes bind retained inputs; they are not
upstream-published checksums. See the derived JSON for the exact provenance and
the limits of each check.
