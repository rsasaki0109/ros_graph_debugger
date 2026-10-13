# Contributing

Thanks for your interest in ROS Graph Debugger! Bug reports, feature ideas, and
PRs are all welcome.

> 🌐 New here? Click through the [live demo](https://rsasaki0109.github.io/ros_graph_debugger/)
> first — it's the real UI playing the bundled bottleneck scenario, no install.

## Development setup

Requires ROS 2 (Humble or Jazzy) on Linux and Node (for the web syntax check).

```bash
# in a colcon workspace with this package under src/ (or at the root)
source /opt/ros/$ROS_DISTRO/setup.bash
pip install fastapi uvicorn websockets pydantic psutil pyyaml pytest

colcon build --packages-select ros_graph_debugger
source install/setup.bash
```

## Running

```bash
ros2 run ros_graph_debugger agent            # live, opens http://localhost:3939
ros2 run ros_graph_debugger rgd serve --demo # no-ROS demo replay
```

## Tests

The suite is designed to run without a live ROS graph (no DDS required) — pure
logic, in-process rclpy endpoint checks, and a uvicorn-in-thread server.

The separate [real Nav2 planner/controller scenario](docs/navigation_validation.md)
checks moving goals, stopped DDS, recovery, an actual bag and real C++ tracing
on Jazzy. Run complete suites in shared-network containers sequentially: the
HTTP tests use fixed ports.

```bash
python3 -m pytest test/ -v
node --check ros_graph_debugger/web/app.js
```

CI runs the same on Humble and Jazzy (`.github/workflows/ci.yml`).

The multiprocess job also checks `/clock` startup and message-age recovery:

```bash
python3 test/integration/run_clock_scenario.py --output /tmp/rgd-clock
```

It uses domain 91 and port 3959, so run it sequentially with other integration
cases. The publisher and agent run separately. Before `/clock` starts, header
age must be unavailable; 20 ms stamps pass the unchanged Autoware contract,
300 ms stamps produce `stale_data` and exit 1, and recovery passes again.
The fault/recovery checks repeat with `/clock` stopped and message delivery
continuing, proving analysis keeps updating independently of simulation time.
The same run records a real bag containing `/clock` and Odometry, then checks
offline header ages through all five phases with the same 30 Hz / 100 ms
contract. The offline clock basis is recorded in `header_age_clock`; arrival
and freshness windows still use bag receive time.
Recordings, JSON evidence and reports are saved under the output directory;
CI includes them in `integration-humble` / `integration-jazzy` artifacts.
Readiness is polled, and child process groups are stopped on completion/failure.

For a separate public-bag observer-cost comparison, see
[the benchmark protocol and reproduction steps](docs/observer-cost.md). That
experiment records agent/player/receiver CPU and RSS and checks delivery and
the unchanged 30 Hz / 100 ms contract. The separate
[public camera/recorded-clock comparison](docs/observer-image-cost.md) uses an
exact source-frame delivery gate with explicit QoS conditions. These optional
data-heavy experiments are separate from the mandatory Humble/Jazzy integration
tests.

The same integration job checks process CPU sampling against a separate real
ROS worker:

```bash
python3 test/integration/run_cpu_scenario.py --output /tmp/rgd-cpu
```

It defaults to domain 103 and port 3962, preserving an existing `ROS_DOMAIN_ID`
(the CI job keeps domain 91 throughout). The initial CPU sample must be unknown; a
service-controlled CPU workload must exceed 90% of one core and produce
`high_cpu` evidence for the actual worker PID. The live assertion must fail
during load and pass after recovery; stopped-process metrics must clear.
Independent psutil measurements, snapshots and assertion recordings are saved
with the agent/worker logs. Readiness is polled and child process groups are
stopped on completion or failure. Unit checks cover PID reuse, denied access,
and multiple nodes sharing one process (the CPU total is not split per node).

### Real Nav2 lifecycle recovery

The `Nav2 Integration` workflow runs the distribution's actual Nav2 map_server
on Humble and Jazzy. With the workspace built and sourced as above:

```bash
sudo apt-get install ros-$ROS_DISTRO-nav2-map-server ros-$ROS_DISTRO-nav-msgs \
  ros-$ROS_DISTRO-rosbag2-storage-default-plugins
python3 test/integration/run_nav2_scenario.py --output /tmp/rgd-nav2
```

Use a fresh output directory. This scenario fixes `ROS_DOMAIN_ID=93` and uses
loopback port 3952 (`--port` overrides it); run one scenario at a time on this
domain. It verifies an inactive server's diagnosis, actual map delivery after
activation, the same assertion passing after recovery, and a recorder receiving
the retained map after activation. See [the validation contract and artifact
guide](docs/nav2_validation.md). It runs separately from `pytest test/`, polls
readiness, stops its child process groups, and has a 120-second runner budget
within the five-minute CI job. CI saves logs, recordings, reports, the structured
diff and real bag even on failure as `nav2-humble` / `nav2-jazzy`.

### Multiprocess ROS integration

The separate `integration` CI job runs real DDS discovery and message delivery
on both Humble and Jazzy, without a robot. With the workspace built and sourced
as above, run the same scenario locally:

```bash
python3 test/integration/run_scenario.py --output /tmp/rgd-integration
```

Use a fresh output directory for each run. In addition to the runtime
dependencies above, install `ros-$ROS_DISTRO-std-srvs` and
`ros-$ROS_DISTRO-rosbag2-storage-default-plugins`. The script fixes
`ROS_DOMAIN_ID=91`, uses the distribution's default RMW, and listens on loopback
port 3950 (`--port` overrides it). Do not run concurrent scenarios on this domain.

Each pipeline stage, lifecycle node, waiting subscriber, missing-service/action
client, processing-time publisher, incompatible-deadline publisher/subscriber,
and agent runs in a separate process. Readiness is polled from the live graph,
including lifecycle state, measured rates, processing samples, and bag-recorder
subscriptions; unavailable DDS is a failure, never a skip.

Both slow and healthy runs observe 20 seconds with a 5-second assertion warm-up.
The slow run must fail `rgd assert` and expose all six issue categories through
JSON violation counts. Recorded snapshots also verify the deadline evidence and
processing-time attribution. The healthy run must have no detector-related
rate-drop, bottleneck, or slow-callback issues; helper-node failures remain
intentional. Each run records a real SQLite rosbag, analyzes it with an 8 Hz
objects-topic contract, checks the offline assertion, and generates a pipeline
path report. Offline bag assertions use the same 5-second warm-up so the initial
DDS subscription-matching window does not count as steady-state delivery.
These helpers are test-only and are not installed in the package.

The healthy live contract remains at least 10 Hz, as specified by the Autoware
profile. The healthy detector targets 12.5 Hz to provide scheduling headroom;
slow and toggle keep their original cadence. Live rates use DDS receive
timestamps so work in the observer does not appear as a slow publisher. If a
healthy run fails, inspect the recording's numeric `rate_hz` and the bag
timestamps. The scenario preserves genuine failures
without adding tolerance, lowering the contract, or skipping the assertion.

Child process groups are stopped on completion, failure, or termination. Logs,
assertion JSON, snapshots, recordings, bags, and Markdown/HTML reports are left
under the output directory. CI uploads them even on failure as
`integration-humble` / `integration-jazzy`; inspect `agent.log` and
`last-snapshot.json` first for discovery or collection problems. The integration
job has a five-minute timeout; the runner allows 180 seconds for the scenarios
and then cleans up. It does not run as part of ordinary `pytest test/`.

### Real LTTng integration

The `trace-integration` CI job records actual rclcpp subscription, timer and
service callbacks on Humble and Jazzy using userspace LTTng. It needs no kernel
tracing or privileged container. Install `build-essential`, `cmake`,
`pkg-config`, `lttng-tools`, `liblttng-ust-dev`, `python3-bt2` and
`ros-$ROS_DISTRO-std-srvs`, in addition to the agent's dependencies above.

Humble's released `tracetools` library has tracing wrapper symbols but no
LTTng backend. For this test, build an overlay of the same official 4.1.2
source, pinned to commit `3c159b382d2d565e26eaa91e39c9ec06a5c6fe88`:

```bash
# Humble only; stop if the installed tracetools version differs from 4.1.2.
git clone https://github.com/ros2/ros2_tracing.git /tmp/rgd-tracetools-src
git -C /tmp/rgd-tracetools-src checkout 3c159b382d2d565e26eaa91e39c9ec06a5c6fe88
colcon build --base-paths /tmp/rgd-tracetools-src/tracetools \
  --build-base /tmp/rgd-tracetools-build --install-base /tmp/rgd-tracetools \
  --packages-select tracetools --executor sequential --cmake-args -DBUILD_TESTING=OFF
source /tmp/rgd-tracetools/setup.bash
```

After building and sourcing the ROS workspace, run:

```bash
ros2 run tracetools status
cmake -S test/integration/trace_fixture -B /tmp/rgd-trace-fixture
cmake --build /tmp/rgd-trace-fixture --parallel 2
python3 test/integration/run_trace_scenario.py \
  --output /tmp/rgd-trace-test --fixture /tmp/rgd-trace-fixture/trace_fixture
python3 test/integration/run_trace_loss_scenario.py \
  --output /tmp/rgd-trace-loss-test --fixture /tmp/rgd-trace-fixture/trace_loss_fixture
python3 test/integration/run_trace_loss_scenario.py --loss-unit packets \
  --output /tmp/rgd-trace-packet-test --fixture /tmp/rgd-trace-fixture/trace_loss_fixture
```

Use a fresh output directory and a container without another session daemon.
The loss scenario fixes domain 95, has a 90-second budget, and checks 500 real
timer callbacks as a normal control. It deliberately suspends only its owned
LTTng consumers while 5,000 callbacks overflow a small per-CPU buffer. Consumer
PID/start-time/group/executable identities are checked before signaling, and
all suspended consumers are resumed before cleanup. Raw discard notifications,
CLI/agent errors, previous-output preservation and owned-group cleanup are
saved under `trace-artifacts/loss-validation` in both CI distributions. This is
a test of rejecting reported loss, not a performance benchmark or proof that
traces without notifications are complete.

The packet variant uses domain 94 and the same 90-second budget. It verifies
500 warm callbacks without loss before stopping/draining and restarting the
session. Both captures execute 5,500 callbacks; only the loss capture suspends
the owned consumers while a small overwrite buffer loses middle packets.
The normal capture must retain all 5,500 invocations; the loss capture must
report actual discarded-packet messages and make CLI/agent loading exit 2
without overwriting existing output. Warm checks, raw CTF, packet counts and
cleanup evidence are saved under `trace-artifacts/packet-loss-validation`.
Run these scenarios sequentially, including against other local domain-94 tests.

The subscription/timer/service runner owns its foreground `lttng-sessiond`, sessions and child process
groups, and polls daemon readiness and completed callback counts. It fixes
domain 92 and loopback port 3951 (`--port` overrides it), with a 150-second
runner budget inside a five-minute CI job. Do not run concurrently on this
domain. Infrastructure parameter events are completed before the fixture
starts; the fixture disables its own parameter notifications so the recorded
invocations cover exactly the three controlled callback types.

Each callback must have at least eight real invocations. The slow fixture does
150 ms of intentional work in each callback; the healthy fixture does none.
Both use the unchanged default 100 ms callback budget. The test verifies CTF
process IDs, node/topic attribution, zero unattributed invocations, CLI output,
and the agent's direct-CTF and NDJSON input paths. `rgd assert` must fail for
slow callbacks and pass for healthy callbacks, with the recorded `kind` and
numeric evidence checked as well. Missing tracing support is a failure.

CI always uploads `trace-humble` / `trace-jazzy`, including raw CTF, callback
NDJSON, conversion counts, agent/fixture/daemon logs, assertion JSON, the
recording and reports. These fixtures and tools are test-only and do not add
installed-package dependencies. rclpy does not emit the rclcpp callback
tracepoints; this test validates C++ callback tracing specifically.

## Guidelines

- **Keep the agent safe by default**: no auto-subscribing to large topics, bound
  memory, never modify the target system. See
  [docs/performance_safety.md](docs/performance_safety.md).
- **Be honest about uncertainty** (e.g. node→process mapping confidence).
- **Match the surrounding style**: standard library first, minimal dependencies,
  comments that explain *why*.
- If you add or change an HTTP route, update [docs/api.md](docs/api.md) — a test
  (`test/test_docs_api.py`) enforces that code and docs agree.
- Add a test for new analysis rules or data transforms; they should be testable
  without DDS (inject into `RuntimeGraphStore` or pass duck-typed objects).

## Pull requests

Keep PRs focused, describe the user-facing change, and make sure
`pytest test/` and the web check pass. Be kind — see
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

For interpreting low-rate failures, see [rate measurement and delivery evidence](docs/probe-reception-evidence.md).

The integration job also checks [probe receipt-to-callback gaps](docs/probe-reception-evidence.md#probe-receipt-to-callback-gap) with an independent publisher, a condition-controlled observer stall and uninitialized simulation time.

The same scenario checks [DDS source-to-receipt timestamp differences](docs/probe-reception-evidence.md#dds-source-to-receipt-timestamp-difference) against each publisher call on the shared host clock, retaining every sequence and checking raw-pair p95 coverage. Remote clock synchronization remains unknown; these differences add no rate or latency budget.

For reproducible investigation tasks, see [investigation cases](docs/investigation-cases.md). The downstream Humble/Jazzy `investigation` jobs consume integration and LTTng artifacts and validate ten original cases, including duration values, declared measurement sources, same-row citations and 82 deliberately incorrect answers per distro. Legacy unknown provenance and explicitly synthetic controls can be exercised locally. This is rubric calibration, not a model-accuracy benchmark.
