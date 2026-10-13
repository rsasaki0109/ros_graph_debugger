# Imported trace execution-window validation

The optional public-input Autoware capture documented in
[PR #33](https://github.com/rsasaki0109/ros_graph_debugger/pull/33) exposed a
conversion limitation: callback durations survived import, but their exact
start/end times did not. This change retains those boundaries and the declared
CTF clock UUID without treating a later snapshot timestamp as a new measurement.
See [tracing.md](tracing.md) for the interchange and unknown-window rules.

## Reused actual capture

The producer was validation branch commit
`dbddd94b655183a5e539f518920cc76f72d94cb5`, using the unchanged official C++
`gyro_odometer` and `vehicle_velocity_converter` with public DLR sample input.
It was a validation checkout containing pending changes, not a released main
build. This experiment re-read its existing capture; it did not run Autoware
again or create a new bag. All 110 CTF files (2,155,880 bytes) matched their
original SHA-256 hashes.

An independent per-process/handle/clock start/end stack was compared with
conversion output before millisecond rounding:

- 83,762 ROS trace events; 6,590 paired invocations; 12 executed groups.
- All 6,590 converted start/end boundaries matched the raw events exactly.
- Zero unmatched starts, unmatched ends or unattributed invocations.
- Every group's minimum start and maximum end matched its raw invocations.
- All original count/mean/p95/max/source values remained unchanged.

The declared clock was
`ctf_ns_from_origin:aa6f1a7b-967e-406d-89ca-bcc739ae56a6`.
Three actual Autoware subscription groups had these bounds:

| Node / callback | Invocations | Start ns | End ns |
|---|---:|---:|---:|
| `/gyro_odometer` / `sub /sensing/imu/tamagawa/imu_raw` | 488 | 1791864081507749018 | 1791864110603065973 |
| `/gyro_odometer` / `sub /sensing/vehicle_velocity_converter/twist_with_covariance` | 735 | 1791864081508102781 | 1791864110603170203 |
| `/vehicle_velocity_converter` / `sub /vehicle/status/velocity_status` | 735 | 1791864081507887030 | 1791864110603191087 |

## Import and transport

An otherwise idle Jazzy agent loaded the CTF directly with `--trace-file` after
the Autoware components had stopped. All 12 groups matched through HTTP snapshot
and callbacks endpoints, CLI snapshot and three recorded snapshots. The
recording generated Markdown and standalone HTML reports with the historical
window limits. Chromium rendered the HTML inline with zero network requests.

A replay of an original recorded frame preserved the complete snapshot,
callbacks, original recording header and rendered briefing through actual stdio
MCP calls. No model was invoked. Agent and replay process groups were reaped.

Pure tests cover nanosecond integers beyond floating-point precision, nested
callbacks, different-clock pairing, incomplete/mixed/invalid bounds, zero bounds,
other duration sources, immutable legacy recordings and escaped HTML labels.
The existing real LTTng integration scenario also requires each subscription,
timer and service aggregate to match its raw invocation bounds and retain the
unchanged healthy/slow budgets on Humble and Jazzy.

## Limits

The bounds cover accepted invocations in a group, not the complete recording
interval. This capture includes healthy, fault and recovery phases together;
no phase alignment is claimed. A clock UUID does not authenticate imported
metadata or establish synchronization with ROS `/clock`, headers or another
host. Complete pairing does not prove DDS delivery completeness. The imported
stats do not establish current node activity, tracing overhead, CPU time,
end-to-end latency, whole-stack correctness or model reasoning accuracy.

Raw CTF, manifests, audit, recordings, logs, reports, screenshot and cleanup
results are retained in the development workspace at
`/workspace/trace-measurement-window-validation/`; raw captures are not packaged
or published by this change. Direct CTF checks use the SDK's system Python with
`python3-bt2`; the host virtual environment has no `bt2` binding.
