# Observer cost on a public camera recording

This extends the [point-cloud comparison](observer-cost.md) to the official
Autoware real-world AR tag sample. It replays original `/clock` and
`/sensing/camera/front/image` messages in separate processes. It measures the
player, a common receiver and RGD; it does not run the original Autoware stack.

## Source and delivery conditions

The fixed official source and local-only archive hash are documented in
[public-bag-validation.md](public-bag-validation.md). The recording has 539
images with unique header stamps. Each serialized image is about 7.37 MB.
The inner 10.05–19.95 second receive-time interval has 70 images and
516,099,640 serialized bytes. These are application bytes, not DDS wire traffic.

The recorded image QoS and actual default playback offer BEST_EFFORT. In this
environment, the exact-delivery gate **failed** at the common BEST_EFFORT
receiver: 38 of 70 frames were missing without an agent, and 27 were missing
with the agent's probes disabled. CPU was low. This establishes a delivery
limitation without implicating RGD; it does not identify the packet-loss mechanism.
Requesting RELIABLE from that BEST_EFFORT publisher received no images because
the QoS policies are incompatible. These failures are retained, not skipped.

A separate comparison explicitly offers RELIABLE for images and requests
RELIABLE at the common receiver. The RMW remains the distribution default.
The exact-delivery rule is unchanged: every original interior image stamp must
arrive once, with its serialized size intact. This is a controlled reliability
configuration, not a software fix or proof that the original default is lossless.
RGD's probes retain their normal BEST_EFFORT request; their reception is a
separate observation from the common receiver's reliable delivery.

## Reproduce

Use a built and sourced Linux/Jazzy checkout with SQLite rosbag2, standard
Image/Clock messages and the player Resume service. Extract the original sample
bag locally; do not add it to the repository. From the repository directory:

```bash
python3 test/benchmarks/run_image_observer.py \
  --bag /absolute/path/ar_tag_based_localizer_sample_bag \
  --output /tmp/rgd-image-observer-default \
  --modes baseline --source-label "$(git rev-parse HEAD)"

# Separate compatible RELIABLE delivery comparison; does not erase default loss.
python3 test/benchmarks/run_image_observer.py \
  --bag /absolute/path/ar_tag_based_localizer_sample_bag \
  --output /tmp/rgd-image-observer-reliable \
  --reliability reliable --repetitions 3 \
  --source-label "$(git rev-parse HEAD)"
```

Always use fresh output directories and avoid concurrent workloads. Domain 97
is fixed; the default HTTP port is 3963 (`--port` overrides it). An optional
`--source-metadata` JSON file records acquisition provenance. No mandatory
package dependency or CI download is added.

Four arms run three times in a reproducibly shuffled order: player/common
receiver, then the same with agent `--no-probe`, explicit `/clock` probe, and
explicit Clock plus Image probes. Agents use `use_sim_time:=true`. Playback
publishes the original recorded `/clock`; no generated `--clock` publisher is
added. The player starts paused, and polling checks DDS publishers and HTTP
before issuing Resume. Source image stamps map receipts to original bag offsets.
Resource sampling covers offsets 10–20 seconds and delivery includes a drain
through offset 22. Startup is excluded from the measured resource interval.

The script exits nonzero for any missing or duplicate interior frame, wrong
serialized size, unavailable middleware timestamps, incomplete interval or
inactive image probe. It writes receiver rows, resource samples, snapshots,
commands and result JSON before cleanup; `cleanup.json` verifies that no owned
process group remains. The default BEST_EFFORT experiment above may fail: retain
its evidence and investigate rather than allowing loss to make it pass.

## Measurements

On 2026-10-12, main `9f48a998` completed all 12 controlled RELIABLE runs with
70/70 frames and zero duplicates at the common receiver. Raw runs remain local;
the source, production-file hashes, protocol and derived summaries are in
[observer-image-cost-results.json](observer-image-cost-results.json).
Values below are medians across three runs; one CPU core is 100%.
RSS is the median of each run's peak process-group RSS.

| Agent probes | Agent CPU | Agent peak RSS (MiB) | Receiver DDS source-to-receipt p95 (ms) |
|---|---:|---:|---:|
| No agent | — | — | 13.47 |
| Disabled | 4.26% | 127.43 | 14.30 |
| Clock | 4.71% | 127.27 | 21.56 |
| Clock + image | 8.24% | 159.55 | 33.08 |

The image probe adds about 3.53 percentage points of one core and 32.29 MiB
relative to the Clock-only arm in this workload. The common receiver's p95
DDS timestamp gap is larger with probes. These small repeated samples do not
establish statistical significance or a whole-stack overhead budget.

Image probe rate ranges were 3.20–6.56, 2.29–3.98 and 3.19–5.78 Hz across
the three runs. Reliable delivery of all 70 frames at the common receiver does
not prove that RGD's BEST_EFFORT probe received every frame. Treat probe rates
as observer receipts, not evidence by themselves of a slow publishing algorithm.

RSS sums resident pages across each process group and may double-count shared
pages. The DDS timestamp gap is a local middleware source-to-receipt measure,
not camera exposure latency or callback execution time. Recorded ROS header
ages remain about 1.3 seconds in these intervals; they are not a healthy
100 ms camera contract. No camera age budget was invented or weakened.
Interface counters and DDS shared-memory traffic are not measured here.
Original node CPU, processing times, localization accuracy and physical robot
behavior remain outside the scope of this experiment.
