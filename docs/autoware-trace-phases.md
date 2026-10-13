# Same-clock assertion envelopes in an actual Autoware trace

Whole-capture callback statistics mix setup, healthy input, omission and recovery.
This optional experiment adds a separate test-only LTTng marker process and
records the beginning and end of each existing `rgd assert` command in the same
CTF capture as the unchanged C++ components. It does not modify Autoware code or
infer phase boundaries from similar timestamps or message headers.

## Clock coordinates

Six `lttng_ust_tracef:event` markers contain a unique run ID, sequence 1–6 and
healthy/missing-IMU/recovered begin/end labels. The marker is a separate process
(captured vpid 83). It acknowledges a UNIX-socket request only after emitting the
event; readiness polls whether the provider is enabled. Capture begins before
the C++ constructors so ownership registration events are available.

All selected events declare clock UUID
`aa6f1a7b-967e-406d-89ca-bcc739ae56a6` and frequency 1 GHz. Metadata inspection
found **four** per-process epoch-offset values differing by 1–4 ns (three occur
in the events read for this audit). UUID equality alone is insufficient for
applying each stream's epoch conversion at an exact boundary. The final audit
therefore uses `default_clock_snapshot.value`: raw ticks of the common declared
1 GHz clock, expressed as nanoseconds with `ctf_raw_1ghz:<UUID>`. It rejects
unsupported frequencies, missing UUID/PID metadata, mixed clock domains, missing
or duplicate markers and incomplete callback pairs.

The initial `ns_from_origin` analysis is retained separately. Re-analysis of
the **same immutable capture** on raw coordinates preserves every phase's
invocation count, crossing count and duration statistics. No new C++ experiment
was substituted to hide the coordinate issue.

## Observed windows

The interval surrounds the assertion command, including its startup and settle
work. It is **not** the exact settled snapshot interval. Only invocations whose
start and end are wholly inside a window are aggregated. Boundary-crossing
invocations are counted separately; they are never clipped into a phase.

| Phase | Raw start ns | Raw end ns | Contained invocations | Crossing |
|---|---:|---:|---:|---:|
| Healthy | 101030183515186 | 101036309361892 | 1,534 | 0 |
| Missing IMU | 101039174885062 | 101045252311623 | 1,339 | 0 |
| Recovered | 101048220189542 | 101054347044244 | 1,528 | 0 |

| Actual subscription callback | Healthy | Missing IMU | Recovered |
|---|---:|---:|---:|
| Gyro: `/sensing/imu/tamagawa/imu_raw` | 184 | 0 | 183 |
| Gyro: `/sensing/vehicle_velocity_converter/twist_with_covariance` | 184 | 183 | 183 |
| Converter: `/vehicle/status/velocity_status` | 184 | 183 | 183 |

The two velocity callbacks continue during the controlled IMU omission. The
strict original scenario still passes/fails/passes (exit 0/1/0); independent
output receipts during assertions are 184/0/183. There are 472 valid outputs
over the complete run, associated with original input stamps, finite and in
`base_link`. Minimum output rate 20 Hz, at least 100 healthy/recovered assertion
receipts, zero fault receipts and diagnostic/reference gates are unchanged.
Recovery diff improves; the existing static-TF/global-readiness limitations
remain in the recordings.

Across the capture: 78,232 ROS events, six marker events, 87 resolved callback
handles and 6,047 attributed paired invocations, no unattributed pairs or discard
notifications. Raw CTF has 121 files / 2,061,968 bytes; a per-file SHA-256
manifest is retained outside Git. Mean/p95/max are still rounded duration
statistics; displayed zero is quantization, not proof of zero callback work.
Missing IMU has no duration sample to summarize, rather than a zero p95.

## Source and reproduction

The actual producer is clean validation checkout
`dbddd94b655183a5e539f518920cc76f72d94cb5` (tree
`6ba6f71c937d63b404c0085046a4f01e1be277ad`), containing pending changes rather
than released main. The unchanged official gyro/converter, normal DDS and pinned
Jazzy SDK use the original read-only public DLR input and calibrated static TF.
It is a new run, separate from the whole-capture experiment in
[PR #33](https://github.com/rsasaki0109/ros_graph_debugger/pull/33).
This validation branch must not be merged wholesale into main.

The test-only helper sources are `test/integration/phase_marker.c` and
`test/integration/trace_phases.py`. Build the marker in an environment with
`pkg-config` and `liblttng-ust-dev`:

```bash
cc -Wall -Wextra -Werror test/integration/phase_marker.c \
  $(pkg-config --cflags --libs lttng-ust) -o /tmp/rgd-phase-marker
```

Create an owned userspace LTTng session; enable
`ros2:*,lttng_ust_tracef:event` and the `vpid` context before constructing ROS
nodes. Use per-process buffers (four 64 KiB subbuffers) and start the marker
with a **fresh** UNIX-socket path. Each request is one newline-terminated JSON
object, e.g. `{"run_id":"unique-run","sequence":1,"phase":"healthy","boundary":"begin"}`;
require the complete `ACK\n` response before starting the command. Emit its end
marker after the command returns. Repeat for omission and recovery. Own and
reap the marker, session daemon, CPP nodes and command process groups; stop and
destroy only the session created by the test. Fixed sleeps cannot establish
provider or ROS readiness.

After capture, in system Python with the optional `python3-bt2` binding:

```bash
python3 test/integration/trace_phases.py --trace /absolute/path/trace \
  --run-id unique-run --marker-pid MARKER_VPID --out /tmp/fresh-phases.json
```

The cloud experiment's stable wrappers, exact executed source hashes, original
and corrected analyses, raw CTF, input audit, receipts, records, reports and
cleanup proof are at `/workspace/autoware-trace-phase-validation/`. The executed
scenario wrapper is preserved as `captured-marked_scenario.py`; the later helper
also rejects a truncated acknowledgment immediately. Existing `marked_capture.py`
uses fixed artifact paths: read it and use a fresh directory before reproduction.
The strict optional Autoware scenario exists on the producer checkout, not this
main-based helper branch. No bag/CTF/binary or required dependency is packaged.
The marker compiles with warnings as errors in both retained Humble and Jazzy
SDKs; the actual Autoware run is Jazzy. Pure tests cover malformed marker order,
PID/clock/frequency failures, epoch-offset differences, boundary crossing and
discard notifications.

Both SDKs decode the original Jazzy capture into the **same exact raw-clock
result**. Native full `pytest test/` passes 427 tests on Jazzy and 426 on Humble
with its existing optional MCAP skip; 27 new pure controls are included. The
initial local marker-tool checkpoint predates the raw-clock correction; exact
executed file hashes are retained alongside the final audit source. Derived
results are in [autoware-trace-phase-results.json](autoware-trace-phase-results.json).

## Limits

Same-capture raw-clock envelopes do not authenticate imported metadata, align
ROS `/clock` or message headers, establish exact settled-sample windows, prove
DDS delivery completeness or reveal per-message causal paths. The controlled
omission and independent output assertions provide separate scenario evidence;
callback duration alone does not explain downstream loss. No paired overhead,
CPU-time/end-to-end measurement, original whole-stack/localization accuracy,
current health, model reasoning or human-time/adoption result is claimed.
