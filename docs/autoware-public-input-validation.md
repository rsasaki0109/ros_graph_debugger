# Actual Autoware components with public inputs

The standalone scenario runs the unchanged C++ `autoware_vehicle_velocity_converter`
and `autoware_gyro_odometer` 1.9.0 nodes in separate processes, with a separate
RGD agent and a rosbag2 player. It replays the IMU, vehicle velocity and static
transforms from the public DLR **AR-tag input bag**, rather than synthesizing
sensor messages or assuming the original recorded system's graph.

The [public archive audit](public-bag-validation.md) documents the distribution
link, size and local fingerprint. The required SQLite file is
`sample_bag/ar_tag_based_localizer/input_bag/input_bag_0.db3`, with SHA-256
`224683a07c61e21a0ae416d9fec80e558cffde76534f340b96492f6772a28797`.
This fingerprint is an independently recorded local check, not an upstream
published checksum. The scenario rejects another or modified input file.
There are 1,735 IMU and 1,735 velocity messages at approximately 30 Hz, and two
static TF messages. IMU uses `tamagawa/imu_link`; vehicle velocity uses `base_link`.

## Build the optional upstream components

Use a ROS 2 Jazzy development environment with colcon, vcs and rosdep. The
five official repository revisions are pinned in
[autoware_sources.repos](../test/integration/autoware_sources.repos): core 1.9.0,
utils 1.11.0, cmake 1.4.0, messages 1.13.0 and internal messages 1.16.0.
Build only the two target nodes and their source dependencies:

```bash
source /opt/ros/jazzy/setup.bash
export ENABLE_AGNOCAST=0   # upstream's normal DDS executor option
export MAKEFLAGS='-j2 -l2'
RGD_CHECKOUT=/path/to/ros_graph_debugger
mkdir -p /tmp/rgd-autoware/src
cd /tmp/rgd-autoware
vcs import src < "$RGD_CHECKOUT/test/integration/autoware_sources.repos"
mapfile -t dependencies < <(colcon list --packages-up-to \
  autoware_gyro_odometer autoware_vehicle_velocity_converter --paths-only)
rosdep install --from-paths "${dependencies[@]}" --ignore-src --rosdistro jazzy \
  --dependency-types=build,buildtool,build_export,exec -y
colcon build --packages-up-to autoware_gyro_odometer autoware_vehicle_velocity_converter \
  --executor sequential --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release
source install/setup.bash
ros2 pkg executables autoware_gyro_odometer
ros2 pkg executables autoware_vehicle_velocity_converter
```

The validated build compiled the same pinned, unmodified source contents
downloaded as HTTPS commit archives, selecting 16 packages. It used the usual
DDS executor, not Agnocast. Required system dependencies include
`diagnostic_updater`, Boost, Eigen, fmt and glog; missing diagnostic and Boost
headers caused initial builds to fail, and those logs were retained.
`BUILD_TESTING=OFF` applies to the upstream dependency build; this experiment
does not certify Autoware's own test suite. RGD's full test suite remains required.

These dependencies are optional test setup, not new RGD installation requirements.
Build/source RGD normally with its web dependencies as described in
[CONTRIBUTING](../CONTRIBUTING.md). Source both workspaces in the same terminal.
The build and input download are separate from the scenario's 180-second budget.

## Run and inspect evidence

```bash
cd /path/to/ros_graph_debugger
python3 test/integration/run_autoware_scenario.py \
  --bag /path/to/sample_bag/ar_tag_based_localizer/input_bag \
  --output /tmp/rgd-autoware-public-run
```

The output directory must be fresh. Domain 97 and port 3975 are fixed by default;
run this sequentially with other ROS scenarios. Readiness is polled from DDS
subscriptions, the player's resume service, actual diagnostics and measured
topic delivery. Each player starts paused until the required consumers exist.
Subsequent phases continue from the preceding replay clock position; replay
does not loop or reset sensor time to the start of the bag. Static calibration
received in the first phase remains cached by the same running nodes.
Owned child process groups and the observer executor are stopped on success,
failure, timeout and termination. No missing prerequisite becomes a skip.

The same nodes remain alive across three phases:

| Phase | Replayed inputs | Required result |
|---|---|---|
| Healthy | IMU, velocity, static TF | diagnostic OK, output at least 20 Hz, `assert` exit 0 |
| Missing IMU | velocity, static TF | IMU timeout ERROR, attributed `no_publisher` and stale output, `assert` exit 1 |
| Recovered | IMU, velocity, static TF | diagnostic OK, output at least 20 Hz, `assert` exit 0; improved diff |

The 20 Hz floor is the explicit contract for this experiment, with approximately
30 Hz source inputs. It does not change the production Autoware profile or the
existing demo's minimum 10 Hz contract. Every settled snapshot must meet its
phase's requirements. An independent subscription must receive at least 100
outputs during each healthy assertion and **zero** during the missing-input
assertion. Issue checks use `kind`, related nodes/topics and evidence. Recovery
must resolve all three fault kinds without new issues. Full diagnostics with
upstream key/value evidence are captured separately from RGD's snapshot fields.

Diagnostic task labels are not ROS node IDs. The scenario requires the exact
`gyro_odometer: gyro_odometer_status` reference in `related_diagnostics`,
diagnostic level 2, the IMU timeout message, and explicit unknown node attribution.
It rejects that label in `related_nodes` and also rejects guessing
`/gyro_odometer` from the label, hardware ID or currently sole diagnostic publisher.
The independent missing-IMU and stale-output issues still require their real
`/gyro_odometer` node/topic references. Diagnostic kind, severity, all rate
thresholds and zero-delivery checks are retained.

Diagnostic references receive the same federation prefix as `diagnostics[].name`.
The full snapshot/briefing retains unknown-origin diagnostics; a node-focused
briefing does not infer which node produced them. Use the full diagnostic
collection alongside the focused graph evidence. Existing recordings, including
the original experiment's diagnostic label in `related_nodes`, stay unchanged.

Markdown and HTML recording reports include diagnostic issues separately from
bottlenecks, preserving severity, frequency, full representative evidence and
diagnostic-task references. Missing references in legacy records or summaries
remain unknown. Recorded observations and the health rollup are unchanged.

Measured reference/transport/report results and source fingerprints are in
[diagnostic-reference-results.json](diagnostic-reference-results.json). These
checks retain the original failed reference as evidence and repeat the strict
public CPP healthy/omission/recovery scenario with the corrected references.

The bag has no offered QoS recorded. Default rosbag2 playback offered volatile
`/tf_static`, which did not match the standard transient-local TF listener.
The scenario explicitly sets reliable/transient-local/keep-all for replayed
static TF. This fixes replay setup; it does not establish the original
publisher's QoS. Sensor payloads, calibration transforms and message timestamps
are unchanged. The player generates `/clock` from bag time, and the gyro and
agent use simulation time. This clock is replay control, not a recorded clock
from the original runtime.

Artifacts include agent/component/player logs, the input hash/count audit,
raw output receipts, raw diagnostics, per-phase `assert.json`, `run.rgd.json`
and Markdown pipeline reports, and `recovery-diff.json`. Partial recordings are
rendered after a failed run when possible. Initial setup failures remain distinct
from the successful run.

The [measured result fingerprints](autoware-public-input-results.json) cover
seven recorded snapshots and five settled checks per phase. The final run
received 184 outputs during the healthy assertion, zero during IMU omission,
and 182 after recovery; healthy/recovered output rates were 30.00–30.02 Hz.
All 470 receipts, including readiness periods, had finite values, `base_link`
frames and nondecreasing stamps present in the unchanged public sensor inputs.
This verifies source association and delivery, not estimation accuracy.

During omission, stored rates still show the last received sample window near
30 Hz. They do not mean new messages arrived during the fault. `topic_stale`,
the upstream IMU timeout, the missing input publisher and the independent
zero-receipt count establish the interruption. Recovery improves the graph
verdict from critical to **degraded**, because the unused `/tf` warning remains.
Middleware/parameter-event edges can also appear in rendered pipeline paths;
these are graph suggestions, not proven causal processing paths.

## Scope of the result

This verifies actual Autoware component execution and evidence-based detection
of an intentionally omitted public input, followed by recovery. The public DLR
sample includes AWSIM reference topics; it is not the separate real-world AR-tag
sample that lacks IMU. The newly observed graph is the replay experiment's graph.
The standard TF listener subscribes to both `/tf` and `/tf_static`; this static-only
experiment has no dynamic `/tf` publisher. Its factual `no_publisher` warning stays
in every recording and report. The validator requires the recorded static
`base_link → sensor_kit_base_link → tamagawa/imu_link` path and allows only this
specific unused listener warning. Missing publishers on required input topics
still fail. Thus a healthy **target flow** does not certify a warning-free graph.
It does not reconstruct the original full stack, validate localization accuracy,
measure whole-stack observer overhead, or demonstrate model diagnostic accuracy.

The scenario is opt-in because it needs a 1.29 GB public archive and an upstream
C++ build. Normal Humble/Jazzy CI exercises its evidence validators, including
negative cases; it does not download and replay this public bag on every push.
The actual component run is verified on Jazzy. Humble execution and a fresh
standalone install remain separate checks.
