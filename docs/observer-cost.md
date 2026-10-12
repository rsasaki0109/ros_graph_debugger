# Measuring observer cost with public bag replay

The agent should measure a publisher without creating a rate regression in its
own subscriber. Public bag replay exposed high executor CPU and one such
regression. Collectors now dispatch directly on their background spin thread;
the HTTP server continues to run on the main thread.

## Dataset and scope

Use the `sample_dataset_v2.tar.zst` linked by the official
[Driving Log Replayer v2 setup documentation](https://github.com/tier4/driving_log_replayer_v2/blob/38d08c178663a28a558158797fa323544de53602/docs/quick_start/setup.en.md):
[public download](https://drive.google.com/file/d/1iCoykBBETI_rGfKEFYYb7LFydF-RJVkC/view).
The downloaded archive was 3,279,004,339 bytes. Its locally recorded SHA-256 was
`7f2cb72d21e331d4a911a2f44184fc4fd112ec535b26e12559be96fa24479d68`;
the distributor did not publish a checksum. This records the measured input,
without claiming an upstream integrity guarantee.

Extract `sample_dataset/input_bag` to a local directory. It contains one
`input_bag_0.db3` plus metadata, with 21,590 messages across 15 topics and about
69 seconds of recorded time. The benchmark specifically uses this SQLite bag
and its Odometry, concatenated PointCloud2, `/tf` and `/tf_static` topics. It
does not replay the other custom message types or run an Autoware stack.

The recorded metadata has no endpoint QoS profiles. Default playback offers
volatile `/tf_static`, which does not match the agent's transient-local
subscription; this warning is retained in the logs. The experiment does not
claim original endpoint QoS or complete live TF coverage. Every arm uses the
same playback configuration.

## Reproduce

From a built and sourced **Jazzy** checkout, with the runtime dependencies and
rosbag2 SQLite storage installed:

```bash
python3 test/benchmarks/run_observer.py \
  --bag /absolute/path/sample_dataset/input_bag \
  --output /tmp/rgd-observer-cost \
  --source-label "$(git rev-parse HEAD)"
```

Use a fresh output directory and no concurrent workload. The default ROS
domain is 96 and HTTP port is 3961; `--domain` and `--port` override them. The
benchmark makes no package dependency changes. It needs native message-info
callbacks from Jazzy for the independent receiver; Humble agent compatibility
is covered separately by the unit and mandatory integration suites.

Four arms run three times in a shuffled, reproducible order: player plus common
receiver, then the same with agent `--no-probe`, Odometry-only probes, and
Odometry plus an explicitly selected point-cloud probe. All agents use the
Autoware profile and `use_sim_time:=true`; playback uses `/clock` at 100 Hz and
normal speed. Readiness requires HTTP, clock, message flow and the selected
probes. Each measured interval is bag time 10–20 seconds, following startup.
Startup wall time is recorded separately. Child process groups are stopped in
`finally`; `cleanup.json` verifies that none survive.
`--no-probe` retains graph, TF, diagnostics, statistics and lifecycle collection.

`results.json`, each arm's receiver data, snapshots, resource samples, commands
and logs preserve the observations. `protocol.json` records source hashes,
RMW, Python, CPU affinity, selected topics and serialized bytes. The script
exits 1 if any receiver or probed-agent interval violates the unchanged minimum
30 Hz or maximum p95 header age of 100 ms. The independent receiver must also
receive every original Odometry stamp/count in the inner 10.05–19.95 second
interval, allowing for clock ticks at the boundaries. This check includes a
trailing drain; startup is not counted as successful delivery.

## Measured comparison

On 2026-10-12, the same benchmark compared baseline agent commit `2613e804`
with direct collector dispatch. Input, playback, SDK, RMW, CPU affinity,
selected topics, interval and shuffled arm order matched. Exact production-file
hashes and each run's derived metrics are in
[observer-cost-results.json](observer-cost-results.json). The CPU values below
are medians across three runs, with one core equal to 100%.

| Probes | Agent CPU before | Agent CPU after | Agent RSS before/after (MiB) |
|---|---:|---:|---:|
| Disabled | 90.87% | 7.92% | 127.33 / 126.54 |
| Odometry | 93.74% | 11.81% | 128.16 / 127.49 |
| Odometry + point cloud | 94.30% | 19.01% | 140.01 / 136.16 |

The baseline comparison completed all 12 measurements but exited 1: one
expected Odometry stamp was missing at the common receiver in a point-cloud
run. All baseline agent rate/age observations passed in this comparison. An
earlier exploratory run had an agent rate of 28.83 Hz and `rate_drop` evidence
while the common receiver measured about 50 Hz; this intermittent result is
distinct from the delivery miss above. After the change, all 12 measurements,
60 probed-agent observations and receiver delivery contracts passed; no expected
interior stamp was missing. Both comparisons verified process-group cleanup.

Profiling implicated repeated executor waitable/callback-group scans. A separate
diagnostic comparing pool dispatch with direct dispatch also reduced CPU. These
results support the observer change for this workload, without establishing
that every replay or deployment will have the same overhead or never drop data.
For an older-agent comparison, build/source that revision and copy the current
benchmark harness into its `test/benchmarks` directory, keeping the source
label and production hashes for each version.

## Interpretation

CPU is process-group CPU seconds divided by interval wall time, with one core
defined as 100%. RSS is the sum of resident pages across the process group and
can count shared pages more than once. Rate uses middleware receipt timestamps;
header age uses ROS time. Signed header offsets are retained because this bag
often has small negative offsets, which displayed age clamps to zero. Neither
quantity is a measurement of the original algorithm's execution time.

Interface counters omit DDS shared memory and may include HTTP polling or
unrelated traffic. Source serialized bytes are application data, not wire
bytes. These measures do not establish total DDS traffic. The benchmark does
not use LTTng and cannot establish callback execution-time impact.

Ten-second intervals repeated three times characterize this replay workload;
they do not establish an overhead budget for a whole Autoware deployment or
physical robot. Narrow probe scope first, and measure explicitly enabled large
topics against a common receiver before drawing conclusions about publishers.
