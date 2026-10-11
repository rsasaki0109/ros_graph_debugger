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
