# Evidence-grounded investigation cases

These tasks separate observations, unsupported conclusions and the next useful
measurement. They use frozen original snapshots from real ROS runs and public
data. Preparation invokes no model. Scoring checks **structured claims and
citations**, not the correctness of free-text reasoning, blind AI accuracy or
human investigation time.

## Sources and tasks

The ten CI cases come from the existing separate-process integration and real
LTTng artifacts. Six cover:
slow/healthy detector output, CPU warmup/load/recovery, and an uninitialized ROS
clock with valid DDS receipts. The slow/healthy minimum 10 Hz and CPU controls
are checked before exporting tasks. Existing ROS integration gates are unchanged.
Four duration cases add the processing-time helper from each slow/healthy run
and the subscription/timer/service callbacks from each slow/healthy LTTng run.
Preparation requires the original source, count, duration and issue-attribution
gates; it does not relabel or fill in source data to make a case pass.

Optional local cases add actual Nav2 healthy/paused/recovered recordings and the
original public Autoware camera/Clock bag, its live replay and an older recording
without probe-gap fields. Missing fields remain unknown; recorded header-age
clock provenance comes from the original header, not from numerical ages.
Snapshots without their original recording header get a clearly labelled frozen
wrapper; it supplies no invented clock provenance. Historical Nav2 CPU values
predate the corrected sampler and cannot establish an idle process.

Participants see `participant/case-NNN/prompt.md`, `evidence.json` and the frozen
`input.rgd.json`. Evaluator rubrics, original source paths/hashes and frame indexes
are in a separate `evaluator/` directory. Never give that directory to a model.
Case IDs do not name the phase. This separation prepares future evaluations; it
does not establish that a blind model experiment has already occurred.

### Probe windows and diagnostic tasks

Two optional case families accept existing recordings without ROS or a model.
They freeze the **last original frame** and header; preparation does not validate
the experiment's healthy/fault/recovery contracts or authenticate its producer.
Check those contracts independently before using a run as runtime evidence.

`--window-recording PATH --window-topic TOPIC` grades the existing rate/scope and
the sample count, window span, rate clock and local processed-sample age. A retained
rate is not current delivery. Processing age is not DDS receipt age, header age
or evidence of publisher activity; a backlog may keep processing recent.
Legacy fields and unsupported clocks remain unknown, with parent-object citations.

`--diagnostic-recording PATH --diagnostic-name NAME` grades matching task rows and
all declared diagnostic issue references/evidence in the frame. Diagnostic names
are opaque task labels; graph-name or hardware-ID matches and a sole graph
publisher do not authenticate per-status node identity. Old `related_nodes` values
are preserved as declared, without retrofitting missing `related_diagnostics`.
An absent diagnostic list is unknown; an available list with no matching task is
an observed count of zero. Neither proves system health. Diagnostic cases use the
full runtime briefing, because their task labels are not node/topic focus queries.

Repeat either recording option to export multiple independent frames. For example,
after independently validating the public-input Autoware scenario, use its three
`run.rgd.json` files for normal/omission/recovery tasks:

```bash
python3 test/benchmarks/investigation_cases.py prepare --output /tmp/fresh-window-tasks \
  --window-recording /path/to/scenario/healthy/run.rgd.json \
  --window-recording /path/to/scenario/missing-imu/run.rgd.json \
  --window-recording /path/to/scenario/recovered/run.rgd.json \
  --window-topic /localization/twist_estimator/twist_with_covariance \
  --diagnostic-recording /path/to/scenario/healthy/run.rgd.json \
  --diagnostic-recording /path/to/scenario/missing-imu/run.rgd.json \
  --diagnostic-recording /path/to/scenario/recovered/run.rgd.json \
  --diagnostic-name 'gyro_odometer: gyro_odometer_status'
python3 test/benchmarks/validate_investigation.py --bundle /tmp/fresh-window-tasks \
  --output /tmp/fresh-window-validation
```

Raw observations come only from the original frame. Evaluator provenance records
its file hash and index; it does not certify sensor accuracy, original full-stack
behavior or per-status origin. The scorer rejects citation swaps between window
span/header age, processing age/DDS timestamp gap and diagnostic/node references,
as well as unsupported delivery and node-origin conclusions. This is scripted
rubric calibration, not measured model accuracy or investigation-time improvement.

## Prepare and validate

From the checkout, with the optional web stack and test-only MCP SDK available
(`pip install -e '.[web]' 'mcp==1.30.0'`):

```bash
python3 test/benchmarks/investigation_cases.py prepare \
  --integration /absolute/path/integration-artifacts \
  --trace-recordings /absolute/path/trace-artifacts \
  --output /tmp/investigation-bundle
python3 test/benchmarks/validate_investigation.py \
  --bundle /tmp/investigation-bundle --output /tmp/investigation-validation
```

Use fresh output directories. The source directory must contain `slow/run.rgd.json` and
`healthy/run.rgd.json`, `cpu/{warmup,loaded,recovered}.json`, and
`clock/unstarted.json`, generated by the documented integration scenarios.
Optional `prepare` arguments:

| Argument | Original source |
|---|---|
| `--nav-recordings DIR` | Actual Nav2 `healthy/paused/recovered/run.rgd.json` |
| `--trace-recordings DIR` | Real LTTng `slow/healthy/run.rgd.json` with original source/count/budget evidence |
| `--duration-recording FILE --duration-target NODE` | An original recording, including legacy data without source metadata |
| `--synthetic-demo` | An explicitly synthetic duration control, separate from provided source cases |
| `--public-live FILE` | Camera benchmark `snapshots.json`, measured offsets 10–20 s |
| `--public-legacy FILE` | Older camera replay `snapshots.json`, same offsets |
| `--public-bag FILE` | `rgd bag analyze` recording of the original public camera bag |

See [navigation validation](navigation_validation.md), [public bag validation](public-bag-validation.md)
and [camera observer benchmark](observer-image-cost.md) for reproduction and
source limitations. Raw bags and response logs stay outside Git.

Validation replays each original frame unchanged and checks a real CLI snapshot,
stdio MCP snapshot, callback list, recording metadata and focused Markdown, including the
recorded clock note. Readiness is polled, cases run sequentially and owned replay
groups/MCP sessions are closed on success or failure. Port 3972 must be free;
`--port` changes it. Responses, cleanup, reference scores and negative controls
are retained. The reference response is generated from the rubric and is
explicitly a **scripted oracle**, not a model answer.

CI adds two `investigation` jobs after the Humble/Jazzy integration and trace
jobs. Each downloads both original artifacts and validates all ten cases. The MCP dependency
is test-only; no required package dependency changes. The original integration
job retains its five-minute budget. Task inputs, rubrics, responses and failures
are uploaded as `investigation-<distro>` artifacts.

## Score externally obtained answers

Follow each prompt's JSON contract. Observations need a value and an exact JSON
Pointer citation into `evidence.json`; unknown values are null and cite the
nearest existing parent. Identity binds the answer to the complete evidence
digest. Limitations require the supported `not_established` classification and
a relevant next-check ID. For example, a positive DDS timestamp gap alone does
not establish host-clock synchronization or network latency; CPU alone does not
establish successful processing or callback duration. Independently computed
p95 values cannot be added to obtain an end-to-end p95.

Duration tasks request each selected node's callback label, topic, sample count,
mean/p95/max and **declared** source. Claim IDs keep the original callback-array
index (`duration_N_*`); values and sources must cite that same observation row.
Equal p95 values in different rows do not make their citations interchangeable.
Known source values are `trace`, `application_report` and `synthetic`; missing,
invalid and unrecognized values stay unknown and cite the callback parent.
Missing timing/sample-count values are unknown, not zero. An empty observed
callback list differs from a missing list and does not prove no callback work.

Each duration row also requests `trace_window_start_ns`, `trace_window_end_ns`
and `trace_window_clock`. A complete integer, non-reversed tuple with a declared
`ctf_ns_from_origin:<clock UUID>` on a trace row is an observed **declaration**;
values cite that same callback row. Missing, invalid, incomplete or non-trace
tuples make all three window claims unknown, citing the callback parent. Zero
is valid; nanoseconds must stay exact JSON integers, including values beyond
floating-point precision. The evaluator does not reconstruct bounds from
duration, sample count, snapshot time, filenames or recording header clocks.

Windows describe historical imported invocations. The rubric requires separate
checks for current node activity, CTF/ROS/header alignment, fault-phase markers,
trace completeness and authenticated clock metadata. A recent snapshot, valid
UUID or ROS header-age clock setting establishes none of those conclusions.
The exporter retains the original frame and header without adding nodes. When
a callback owner is absent from the graph, the manifest selects
`get_runtime_briefing`; callbacks and snapshot remain separately available via
MCP. Older manifests still default to `get_node_briefing`.

The rubric rejects a single-callback start/end conclusion from an application
report or unknown source, measured runtime timing from synthetic data, and an
application-cycle measurement inferred from a callback trace. A source string
alone does not authenticate provenance, and p95 alone does not establish a
downstream rate-loss cause. Those questions require verifying original duration
provenance, inspecting cycle boundaries or collecting paired measurements.

Duration tasks retain the seven original negative controls and the three source controls:
source confusion, a value/source citation mismatch, and a source-specific timing
overclaim. Eight additional controls reject fabricated boundaries, wrong-row
boundary citations, invented clocks and each of the five unsupported window
conclusions. A complete declared window also adds a floating-point nanosecond
control. Thus a duration case with rows rejects 18 invalid answers, or 19 with
complete windows. The ten main-based CI cases reject 114 per distro with legacy
window metadata; with complete windows in both real-trace cases the count is
116. Counts depend on the actual source fields, never on filling in missing
metadata. These controls calibrate the rubric, not a model.

Place each response at `answers/case-NNN.json`, then run:

```bash
python3 test/benchmarks/investigation_cases.py score \
  --bundle /tmp/investigation-bundle --answers /absolute/path/answers \
  --out /tmp/investigation-score.json --model-id ACTUAL_MODEL_AND_RUN_ID
```

Use a fresh output file. Exit 0 means all structured checks passed; exit 1 means
an answer was missing, malformed or incorrect. Wrong frames, altered evidence,
wrong citations, fabricated values, missing/duplicate/extra claims, unsupported
certainty and irrelevant next checks fail. Record the actual model/version,
parameters, tool-call transcript and repetitions separately; use `unreported`
for unavailable identity rather than inventing it. Do not label scripted oracle
scores as AI results. Free-text reasoning, alternative next-check wording and
investigation time are outside this fixed rubric and need separate review.

## Local verification

The first local run validated 12 real-source cases: six fresh Jazzy ROS cases,
three actual Nav2 recordings and three public camera cases. CLI/MCP preserved
every selected snapshot and header, and the focused briefing matched its
renderer plus recording clock note. All 84 deliberately invalid structured
answers were rejected and replay groups were reaped. No model was invoked.
Derived results and source hashes are in
[investigation-case-results.json](investigation-case-results.json).

An initial validator run omitted the existing replay clock note from its
expected Markdown and failed; it was corrected to compare the complete documented
response. The exact comparison remains in place and the failed result is retained.

The duration extension validates 12 cases locally: the ten integration/trace
cases, one original legacy LTTng recording without source fields, and one
explicitly synthetic demo control. All 102 deliberately invalid answers fail;
the scripted-reference scoring CLI exits 0 and the wrong-source controls exit 1.
CLI/MCP preserve every selected frame/header/callback list, focused Markdown
matches exactly, and replay groups are reaped. The legacy source remains unknown.
Derived hashes and controls are in
[duration-investigation-results.json](duration-investigation-results.json).
These results do not measure model accuracy or investigation time.

The window/diagnostic extension adds six cases from the original public-input
Autoware normal/IMU-omission/recovery recordings and two from a legacy omission
recording. All 58 and 20 deliberately invalid answers respectively are rejected;
the existing 12 cases still reject all 102 controls. CLI/actual stdio MCP preserve
snapshots and original headers exactly, topic-focused and diagnostic-full
Markdown match the complete renderer, and owned replay groups are reaped.
The scoring CLI exits 0 for scripted reference answers and 1 for unsupported
current-delivery/node-origin claims. No model was invoked.

The omission frame retains 29.98 Hz over 50 samples/1,634.279 ms while processed
sample age is 10,698.5 ms; the diagnostic task is ERROR with an empty declared
node reference. These observations are distinct from independent receipt counts
and the experiment's fault injection, which were verified separately. Legacy
window/diagnostic-reference fields remain unknown, and original node-label values
remain unchanged. Local derived results, original frame indexes/file hashes and
benchmark source hashes are in
[window-diagnostic-investigation-results.json](window-diagnostic-investigation-results.json).
Those file hashes bind retained local inputs; they do not authenticate producers
or measure model accuracy, free-text reasoning or investigation time.

The trace-window extension reuses two actual imported public-input Autoware
frames: one with exact CTF window metadata from
[PR #34](https://github.com/rsasaki0109/ros_graph_debugger/pull/34), and the original
import without those fields from [PR #33](https://github.com/rsasaki0109/ros_graph_debugger/pull/33).
Each frame supplies separate gyro-odometer and velocity-converter tasks (four
tasks, not four new experiments). All 74 invalid answers fail; scoring CLI exits
0 for scripted references and 1 for unsupported-current-activity controls.
The callback owners are absent from both captured graphs, so each case uses a
full briefing without inserting nodes. CLI, stdio MCP, callback lists, original
headers and Markdown are exact; source file hashes are unchanged and replay
groups are reaped. No model is invoked.

The existing 12-case integration/trace/legacy/synthetic bundle was separately
reprepared from its retained sources and rejects 150 invalid answers. Latest
helper recalibration preserves all 16 transported case rubrics and rejects 224
controls in total. This main-based benchmark can read newer frozen window
fields without requiring the runtime change to be merged; main-generated
legacy fields remain unknown. Window declarations do not authenticate raw
traces, align phases or establish current health. Derived source hashes and
results are in [trace-window-investigation-results.json](trace-window-investigation-results.json);
raw inputs and evaluator material remain outside Git in
`/workspace/trace-window-investigation-validation/`.
