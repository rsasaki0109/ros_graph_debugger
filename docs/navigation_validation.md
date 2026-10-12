# Real Nav2 planner/controller fault and recovery

This test runs actual Nav2 map_server, NavFn planner, regulated pure pursuit
controller and both costmaps, with the official Nav2 loopback simulator.
It uses the real ComputePathToPose and FollowPath actions and observes the
resulting commands and robot transforms. A stopped simulator provides a known
TF and sensor-data fault, followed by resumed navigation and recovery checks.

The same run produces live recordings, a real SQLite bag and userspace LTTng
CTF. The test belongs to the `Nav2 Navigation Evidence` workflow and runs on
Jazzy. Humble has no official Debian `nav2_loopback_sim` package; existing
Humble unit and multiprocess checks continue to run. This scenario is never
skipped on its supported distribution.

## Run locally

Build and source the workspace as described in [CONTRIBUTING](../CONTRIBUTING.md),
then install the test dependencies:

```bash
sudo apt-get install python3-bt2 lttng-tools \
  ros-jazzy-nav2-map-server ros-jazzy-nav2-planner ros-jazzy-nav2-controller \
  ros-jazzy-nav2-navfn-planner ros-jazzy-nav2-regulated-pure-pursuit-controller \
  ros-jazzy-nav2-loopback-sim ros-jazzy-rosbag2-storage-default-plugins
ros2 run tracetools status
python3 test/integration/run_navigation_scenario.py --output /tmp/rgd-navigation
```

Use a new output directory. ROS_DOMAIN_ID is fixed to 94; run one scenario at a
time on that domain. Default RMW is used. The live agent listens on loopback
3956 and the trace-loading agent uses 3957; `--port` changes both. The runner
allows 150 seconds and the CI job allows five minutes including installation.
LTTng needs the userspace tracing backend, not kernel tracing or a privileged
container. Babeltrace2 bindings must match the system ROS Python.

Nav2 parameters are in `test/integration/nav2_navigation.yaml`. The generated
10 m by 6 m map has occupied boundaries and a free interior. Real Debian
package versions are logged by CI; native validation used Nav2 1.3.13.
These are test-only dependencies and helpers, not installed package contents.

## What must pass

1. Configure and activate map_server, start the official simulator, and send
   its initial pose. Configure and activate the actual planner/controller and
   their costmaps. Require known active states and real TF map → odom →
   base_footprint; absent state never qualifies as healthy.
2. Start a real recorder before movement. Ask NavFn to plan to (4, 1), require
   a successful action with a finite path starting at the observed robot pose
   and ending at the requested goal, then ask the controller to follow it.
   Observe six seconds with one second of settle time. Require at least
   5 Hz scan, 20 Hz odometry and 10 Hz velocity commands, and forbid stale
   TF/topics, rate drops, stale data, bottlenecks, inactive lifecycle, missing
   service/action servers and QoS mismatch. Require actual nonzero commands,
   FollowPath success and final position within the configured 0.15 m tolerance.
3. Start another real goal, confirm movement, and SIGSTOP the simulator's
   process group. Wait for both dynamic TF edges to exceed 1000 ms and both
   scan/odometry receipts to exceed 2000 ms. Run the same assertion; it must
   exit 1. Every settled snapshot must contain the frame-specific `tf_stale`
   and topic-specific `topic_stale` evidence while managed nodes remain active.
   Last receipt times must stay constant across the paused recording. The
   controller must abort with FAILED_TO_MAKE_PROGRESS, as its progress checker
   observes no further movement.
4. SIGCONT the simulator, submit a new goal, wait for measured rates and TF
   freshness, and run the same assertion. It must exit 0 in every settled
   sample. Require real controller success and arrival at (8, 1). Reports and
   structured diff must show both TF-frame and both sensor-topic issues resolved,
   an improved verdict and no new issue.
5. Close the bag cleanly. Require actual scan/odometry messages before and
   after the pause, and none in its central interval. Analyze with the Nav2
   profile: the paused interval must expose each sensor's rate drop, and the
   recovered moving interval must meet the same rate floors. The offline
   rate-drop assertion must exit 1. Retained map/static-TF delivery is required.
6. Stop the owned LTTng session, convert actual CTF through the CLI and verify
   its NDJSON matches conversion. Require C++ map_server/planner/controller
   registration with process IDs, and at least 60 controller odometry callback
   invocations attributed to `/controller_server` and `/odom`. Load those stats
   into an agent that still discovers the real controller and compare all stats.
   Reject any discarded-event or discarded-packet notification in the CTF.

Readiness polls actual conditions and checks child processes. It uses no fixed
startup sleep. Process groups, the ROS driver executor and owned session daemon
are stopped on success, failure, SIGTERM and budget expiry. A paused group is
resumed before cleanup. Partial recordings are rendered when possible.

## Evidence and limitations

CI uploads `navigation-jazzy`, including on failure:

| File | Evidence |
|---|---|
| Process `.log` files, `last-snapshot.json` | Startup, discovery, live issue evidence |
| `healthy/`, `paused/`, `recovered/` | Assertions, snapshot recordings and Markdown/HTML reports |
| `healthy-motion.json`, `fault-action.json`, `recovered-motion.json` | Actual action results, planned paths and observed final positions |
| `commands.json`, `timeline.json` | Real commanded velocities and pause/recovery times |
| `diff.json` | Structured issue changes, including `related_frames` |
| `bag/`, `bag.rgd.json`, `bag-assert.json` | Actual DDS data and offline rate diagnosis |
| `trace/`, `callbacks.ndjson`, `callback-stats.json`, `trace-snapshot.json` | Actual C++ trace and agent callback attribution |
| `trace-counts.json` | Attributed and unattributed invocation counts |
| `trace-quality.json` | Raw message counts; discarded events or packets fail the test |

The trace contains internal callbacks whose ownership chains the adapter cannot
resolve. Counts are preserved and printed. Callback statistics cover the
attributed subset; unresolved invocations are unknown, and the test does not
claim all callbacks are healthy. Raw CTF retains timestamps; the current NDJSON
summarizes durations without per-invocation timestamps.

The loopback model integrates commanded motion without physical dynamics or
localization error. Assertions cover moving goals; upstream odometry publication
stops when command traffic ends. This validates the planner/controller path and
fault recovery, not AMCL, BT NavigateToPose, physical sensing or a robot.
Observation overhead, investigation-time improvement and external adoption need
separate measurements. Bag analysis cannot reconstruct unrecorded lifecycle or
graph information.

The scenario exposed two fixes: live probe polling refreshed last receipt times
and hid stopped publishers; report/diff projections omitted related TF frames.
Receipt times now change only when a message arrives, and JSON comparisons retain
frame references. Existing snapshot fields and dependencies remain compatible.
