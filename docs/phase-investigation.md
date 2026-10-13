# Investigating callback activity across trace phases

The optional test-only exporter freezes a phase-audit JSON sidecar alongside
three original recordings. It grades declared wholly contained callback counts,
durations and raw-clock command boundaries with exact JSON Pointer citations.
It never copies trace statistics into snapshot callback fields or converts raw
CTF ticks to ROS/header time. Original snapshot extensions and recording headers
remain unchanged. Evaluator file hashes bind inputs, not authenticated origin.

The audit contract is the output of `test/integration/trace_phases.py` in
[PR #36](https://github.com/rsasaki0109/ros_graph_debugger/pull/36): six ordered
markers, healthy/missing-imu/recovered command envelopes, a canonical
`ctf_raw_1ghz:<UUID>` clock and complete attributed, wholly contained callback
groups. This exporter can run independently on its saved JSON output; it does
not require that PR's reader, Babeltrace, ROS or LTTng. Audit preparation rejects
mixed clocks, epoch-coordinate labels, overlapping envelopes, duplicate callback
identities, inconsistent totals, invalid duration values and unobserved targets.
Passing these checks validates declarations, not the original capture.

## Prepare and validate

Use fresh output directories from the checkout, with the optional web stack and
test-only MCP SDK installed. `RECORDINGS` contains the three original
`{healthy,missing-imu,recovered}/run.rgd.json` files. `AUDIT` is the original raw
1 GHz phase-audit JSON, not a recording or a rounded documentation table.

```bash
python3 test/benchmarks/phase_investigation.py \
  --audit "$AUDIT" --recordings "$RECORDINGS" \
  --target /gyro_odometer=/sensing/imu/tamagawa/imu_raw \
  --target /gyro_odometer=/sensing/vehicle_velocity_converter/twist_with_covariance \
  --target /vehicle_velocity_converter=/vehicle/status/velocity_status \
  --output /tmp/phase-bundle
python3 test/benchmarks/validate_investigation.py \
  --bundle /tmp/phase-bundle --output /tmp/phase-validation --port 3986
python3 -m pytest test/test_phase_investigation.py -q
```

Each target makes one task spanning all three phases. Participant inputs include
`evidence.json`, `prompt.md`, the three frozen last-frame recordings, and
`input.rgd.json` for the recovered frame. The last frame is context, not a
settled-window selection. The audit and the phase contexts have separate JSON
roots and clocks. The transport validator replays the recovered frame, checks
CLI, actual stdio MCP snapshot/callback/header responses and focused Markdown,
and closes its owned process groups. The original recordings can also be
replayed separately. Port 3986 must be free; change `--port` if needed.

Rubrics and file hashes/indexes live only under `evaluator/`. Never give that
directory to a model. External structured answers use the existing scorer:

```bash
python3 test/benchmarks/investigation_cases.py score \
  --bundle /tmp/phase-bundle --answers /absolute/path/answers \
  --out /tmp/phase-score.json --model-id ACTUAL_RUN_ID
```

An absent target row means zero **declared contained invocations** with a parent
list citation. Its mean/p95/max and source remain unknown, not zero. Conversely,
a recorded 0.0 ms percentile is an observed rounded value, not zero work.
Boundaries and counts require integer answers, including when floats compare
numerically equal. Equal values from another phase or callback are rejected.

Eight explicit evidence gaps require useful next checks: current activity,
settled-sample boundaries, ROS/header clock alignment, per-message causality,
capture coverage, authenticated origin, unrounded work and boundary crossings.
Neither phase counts nor filenames settle these questions. No model invocation,
free-text accuracy, investigation time or whole-stack health is measured.

## Public-input calibration

[Saved results](phase-investigation-results.json) use PR #36's original capture
of unmodified official Autoware gyro/converter C++ nodes replaying the public
DLR AWSIM input bag. There is no new C++ experiment in this calibration.

| Target subscription | Healthy | Missing IMU | Recovered |
|---|---:|---:|---:|
| Gyro IMU | 184 | 0 | 183 |
| Gyro converted velocity | 184 | 183 | 183 |
| Converter vehicle velocity | 184 | 183 | 183 |

Three tasks reject 55 deliberate invalid answers. All three original context
frames/headers match their sources. Nine transport views (three targets times
three frames, including six extra context replays) preserve CLI/MCP responses
and Markdown, then reap owned groups. Those views are repeated evidence, not
nine independent tasks. The scoring CLI returns 0 for scripted references and
1 for an unsupported per-message-cause claim. These are calibration controls,
not model outputs. Source recordings and audit bytes remain unchanged; raw bags,
CTF and response logs stay outside Git.
