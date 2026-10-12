# Nav2 map_server fault and recovery

This scenario uses the actual `nav2_map_server` executable from the selected
ROS distribution. It demonstrates lifecycle diagnosis and recovery for one
Nav2 component. Navigation actions, localization, planning, controllers, TF,
simulation and physical robots require separate validation.

## Reproduce

Follow [the development setup](../CONTRIBUTING.md#development-setup), then install
the test dependencies and run from the repository root:

```bash
sudo apt-get install ros-$ROS_DISTRO-nav2-map-server ros-$ROS_DISTRO-nav-msgs \
  ros-$ROS_DISTRO-rosbag2-storage-default-plugins
python3 test/integration/run_nav2_scenario.py --output /tmp/rgd-nav2
```

Use a new output directory for each run. The script fixes `ROS_DOMAIN_ID=93`,
uses the default RMW and loopback port 3952, and allows `--port` to override the
HTTP port. Do not run other systems or concurrent scenarios on this domain.
No Gazebo, robot, privileged container or kernel tracing is required.
The test helpers are not installed in the package and add no runtime dependency.

The `Nav2 Integration` workflow installs the distribution's current Debian
package and logs its exact version with `dpkg-query`. It runs on Humble and
Jazzy for pushes to main and pull requests. Local validation used Nav2 1.1.20
on Humble and 1.3.13 on Jazzy; CI's logged version is authoritative for each run.

## Contract

The map server, independent map subscriber and debugger agent run as separate
processes. A driver uses the standard `change_state` and `get_state` services.
The agent observes the system with the Nav2 profile and `--no-probe`: a static
map has no periodic publishing contract. Actual delivery is checked by the
independent subscriber, which requests reliable, transient-local QoS.

1. Configure the server and confirm its state is inactive. Wait for the agent
   to discover `/map_server`, its `/map` publisher and the waiting subscriber.
   Require `lifecycle_not_active`, critical severity, the inactive state and
   `/map` waiting evidence. The subscriber must receive no map.
2. Run `rgd assert` for six seconds with a one-second settle period, explicitly
   forbidding `lifecycle_not_active` and requiring the node and topic. It must
   exit 1. Every settled sample must retain the state, graph and issue evidence.
3. Activate the server. Require both its reported active state and delivery of
   the known OccupancyGrid: frame, dimensions, resolution, origin, orientation
   and all 12 cell values. Run the same assertion again; it must exit 0, retain
   the discovered node and topic, and show no related lifecycle issue in any
   settled sample. Unknown state never qualifies as recovery.
4. Generate Markdown/HTML reports and a structured `rgd diff`. Require an
   `improved` verdict, the map server's lifecycle issue resolved and no new issue.
5. Start `ros2 bag record` after recovery. Its explicit reliable,
   transient-local `/map` QoS must receive the retained map without another map
   publication. A separate heartbeat extends the bag to at least four seconds
   and 40 heartbeat messages. Require exactly one map and deserialize its full
   contents against the fixture. Analyze the bag with the Nav2 profile and
   forbid `rate_drop` and `topic_stale` in an offline assertion. Both the bag
   and subscriber must still contain exactly one map.

All readiness waits poll state, graph, message receipts or SQLite contents.
Observation durations are intentional measurement windows. Processes are
checked during polling, child process groups are stopped on success or failure,
and SIGTERM and the 120-second budget trigger cleanup. The CI job allows five
minutes including dependency installation. Missing DDS delivery fails the test;
there is no skip, xfail or weakened assertion.

## Diagnose failures

CI uploads the output directory as `nav2-humble` or `nav2-jazzy`, including on
failure. Start with these files:

| Artifact | Evidence |
|---|---|
| `agent.log`, `map-server.log`, `observer.log` | Process startup and collection errors |
| `last-snapshot.json` | Last observed lifecycle state, endpoints and issue evidence |
| `inactive/`, `recovered/` | Assertion JSON/stderr, snapshot recordings, Markdown/HTML reports |
| `map.pgm`, `map.yaml`, `map-receipt.json` | Input fixture and actual subscriber delivery |
| `diff.json` | Structured before/after issue comparison |
| `bag-record.log`, `bag-qos.yaml`, `bag/` | Late-recorder QoS, recorder log and real SQLite bag |
| `bag.rgd.json`, `bag-assert.json` | Offline analysis and assertion results |

If a recording exists when a phase fails, the runner attempts to render a
partial report during cleanup. A failure before collection may leave only logs
and the last snapshot. Raw bag contents prove data delivery; bag analysis does
not reconstruct lifecycle state or graph endpoints that were never recorded.
