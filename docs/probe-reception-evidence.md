# Rate measurements and delivery evidence

A low received rate is a useful contract failure, but it does not by itself
identify a slow publishing callback. Receiver loss, queue pressure and observer
work can also reduce the observed rate. The public camera experiment in
[observer-image-cost.md](observer-image-cost.md) motivated this distinction:
complete delivery at a separate RELIABLE receiver did not certify delivery at
RGD's BEST_EFFORT probe.

## Measurement scope

New snapshots add `topics[].rate_measurement_scope`:

| Value | Samples used for the rate |
|---|---|
| `probe_receipt` | Messages taken by the agent's live subscription |
| `subscriber_statistics` | rclcpp subscriber topic statistics |
| `bag_recording` | Messages present in the recorded bag |
| `null` | Unknown, including older snapshots |

Live rates use middleware receive timestamps when the whole sample window has
valid, nondecreasing timestamps; otherwise they use monotonic callback times.
The default live window contains 50 received samples, rather than 50 seconds.
Bag rates use the configured time window. None of these rates certifies all
messages published during that interval were received or recorded.

`rate_drop` and `bottleneck` keep their existing thresholds, kinds, severity and
related topics. Evidence now includes the scope and `delivery completeness:
unknown`. Bottleneck explanations describe a candidate for investigation and
suggest comparing publisher-side timing with receiver delivery. Publisher
callback timing, processing-time reports or independently verified source
frames can provide additional evidence. Scope survives graph polling; subscriber
statistics cannot overwrite a live probe's scope.

The existing `rate_source` field is retained, including the historical `probe`
value used by bag analysis. Older snapshots are readable and their missing scope
is unknown. There is no new required dependency or HTTP route.

## Native MessageInfo capability experiment

On 2026-10-12, the public AR-tag camera bag was replayed with its original
recorded ROS Clock in each cached Humble and Jazzy environment. The player and
common raw receiver explicitly used RELIABLE QoS, with the default RMW. This
was a capability check, separate from the default BEST_EFFORT failure controls.
Each run retained 70/70 interior source frames, zero duplicates and verified
cleanup. The derived observations and raw-result hashes are in
[probe-message-info-results.json](probe-message-info-results.json).

The receiver used the same `with_message_info`/`ProbeExecutor` adapter as RGD,
including Humble's legacy raw-message take path.

| Raw callback metadata | Humble observation | Jazzy observation |
|---|---|---|
| Source and receive timestamps | Available | Available |
| Publication sequence number | Absent | Integer |
| Reception sequence number | Absent | `null` |
| Publisher GID | Absent | Absent |

A publication sequence number without publisher identity cannot safely separate
interleaved publishers. A graph poll reporting one publisher does not prove that
all messages in a rate window came from that publisher. These observations
therefore do not support a per-publisher loss counter in the current Python
callback path. Missing or unsupported metadata remains unknown, never zero loss.
These are results for the tested installations, rather than a promise about
all rclpy versions or middleware implementations.

The installed Jazzy `rmw/types.h` documents the missing-sequence sentinel as
`RMW_MESSAGE_INFO_SEQUENCE_NUMBER_UNSUPPORTED` (`UINT64_MAX`); supported
publication numbers identify messages only when combined with the publisher
GID. Even with that identity, gaps between received sequence numbers would
cover only the bounded observed interval, with loss before the first and after
the last receipt remaining unknown. Such gaps would not identify whether the
cause was transport, queue pressure or observer scheduling.


## Recording reports and AI briefings

`rgd report RUN.rgd.json --md REPORT.md` and the HTML report include a rate measurement sources table.
Each topic has separate counts for probe receipts, subscriber statistics, bag
recording and unknown measurements. Counts are snapshots with a numeric rate,
including zero, rather than delivered message counts or publisher counts.
Unmeasured counts include topic appearances without a numeric rate; snapshots
where that topic is absent are excluded. Sources are counted across the whole
recording, so a source change or an older segment is visible instead of being
represented by the first sample alone. These counts do not establish delivery
completeness or publisher-side throughput.

Missing or unrecognized scope values count as unknown. A legacy `rate_source`
value does not fill in the missing scope: older bag analysis and live probes
both used `probe`. Rendering an older aggregate summary without these counters
shows unavailable counts rather than invented zeros.

Reports retain all evidence from the representative issue sample, including
later uncertainty statements. The sample is still an example, not evidence
that every occurrence has identical measurements. The original issue frequency,
severity, health rollup and rate values remain unchanged.

Snapshot Markdown, including focused briefings through `get_node_briefing`,
shows a rate scope column and states the delivery limitation even when no issue
is detected. JSON snapshots retain their original fields and values. This
improves evidence presentation; it does not measure AI diagnostic accuracy or
human investigation-time savings.


A public-camera validation covered 88 bag snapshots, 11 live replay snapshots
and 10 older live snapshots without the scope field. In each case, the report
counts matched numeric-rate topic appearances, Markdown/HTML retained the
limitation, and actual stdio MCP returned the original JSON snapshot plus the
expected focused scope label. The CLI report matched the verified renderers.
Replay process groups were reaped. Source hashes and derived checks are in
[probe-report-validation.json](probe-report-validation.json); raw inputs and
responses remain outside the repository.


## Probe receipt-to-callback gap

RMW defines its time point as the rcutils nanosecond time point since the Unix
epoch; the tested default Fast DDS receiver supplies it before callback entry.
Live probes expose `probe_callback_delay_p95_ms`: the local system timestamp
at the start of the Python probe callback minus the middleware's receive
timestamp. Capture occurs before payload/header decoding, so that decoding
belongs to the callback body and is excluded. Taking/copying messages and
executor waiting before callback entry are included. The exact middleware
receipt point is implementation dependent; the gap does not separate these
causes, measure publisher callback execution or certify packet delivery.

`probe_callback_delay_samples` and `probe_callback_delay_window_samples` give
coverage of the same bounded receipt window used for the rate. An empty window
or any missing/nonpositive/future timestamp pair makes the p95 unknown until
those samples expire. Valid zero gaps remain zero. Positive system clock steps
can inflate the gap; negative gaps are unknown, never clamped to zero. ROS Clock
and header ages are separate, and simulation time need not be initialized.
Older recordings, bag analysis and subscriber statistics have no such probe
measurement. Low-rate issue evidence includes the gap when available without
changing the rate threshold, severity or verdict. Markdown briefings show it as
probe wait p95 with its clock limitation.

The condition-controlled test uses a separate publisher and receiver with
RELIABLE QoS/depth 100, verifies all 30 sequence values exactly, and keeps ROS
Clock uninitialized. The receiver holds callback 0 until the publisher has
sent 12 messages; the bounded probe window must show a p95 above 300 ms and
then below 100 ms after fresh receipts replace the backlog, while DDS receipt
rate remains above 10 Hz. These are observation-test gates, not new application
budgets. In the native Humble/Jazzy Fast DDS capability runs, delayed receipts
showed roughly 500–300 ms and the final receipts about 0.1 ms. This confirms the
meaning on the tested implementations, rather than all RMW/version combinations.

Run in a built/sourced ROS workspace:

```bash
ROS_DOMAIN_ID=97 python3 test/integration/run_probe_delay_scenario.py --output /tmp/probe-delay
```

Use a fresh output directory. Readiness/release are polled, timeouts fail and
owned child process groups are stopped in `finally`. CI runs this scenario in
both existing integration jobs using their fixed ROS_DOMAIN_ID and default RMW;
raw timestamps, probe metrics, publisher rows, logs and cleanup are saved in
`integration-<distro>/probe-delay/`. All existing rate/age/issue assertions and
the five-minute integration job budget remain in place.


A separate live replay of the original public camera/Clock bag retained the
independent RELIABLE receiver's strict 70/70-frame gate. Ten measured camera
snapshots exposed a receive-to-callback p95 of 10.7–13.7 ms with complete
timestamp-pair coverage. Agent subscriptions remained BEST_EFFORT, so these
gaps do not certify probe delivery or explain all low-rate observations.
Actual stdio MCP replay preserved the original JSON fields and rendered the
new gap separately from header age; older live/bag recordings rendered no
probe gap. This is evidence transport and observer timing, not publisher-side
execution time or human investigation-time improvement.

## DDS source-to-receipt timestamp difference

Live probes also expose `probe_source_receipt_gap_p95_ms`, computed as the
middleware receive timestamp minus the middleware source timestamp. RMW defines
both as rcutils Unix-epoch nanosecond time points; the exact capture points in
publishing and receiving are implementation dependent. These are current live
middleware stamps, including stamps from a bag player, not the original bag's
recording time or sensor header time.

Unlike the local receipt-to-callback gap, this signed difference may span two
host clocks. The agent does not establish clock synchronization or publisher
identity for each receipt. Positive, zero and negative differences are retained;
neither sign proves transport behavior. Clock skew/adjustments can affect the
measurement. It is not measured network latency, sensor age, publisher callback
duration or a per-publisher delivery counter. No new issue or budget uses it.

`probe_source_receipt_gap_samples` counts pairs with both timestamps positive;
`probe_source_receipt_gap_window_samples` counts all receipts in the same bounded
window. Any missing/nonpositive timestamp makes the whole-window p95 unknown
until that receipt expires. Negative differences remain valid and are not
clamped to zero. Existing bag/statistics/older recordings lack this measurement.
Focused Markdown/MCP shows DDS stamp gap, probe wait and header age separately,
with an explicit statement that clock synchronization is unknown. Probe-scoped
rate issue evidence includes the available gap and pair coverage, without
changing the rate threshold, verdict or severity.

The DDS gap and local probe wait percentiles are computed independently.
Adding their p95 values does not give an end-to-end p95.

The existing controlled observer-stall scenario now captures the publisher's
system times immediately before/after every publish call. On the tested same
host/default Fast DDS, all 30 middleware source timestamps must fall within their
matching publish call. Source-to-receipt differences must remain between zero
and 100 ms while the independent receipt-to-entry p95 exceeds 300 ms, then
recovers below 100 ms. Metric p95 and coverage must match the raw pairs in the
held/recovered five-receipt windows. This is a strict test of the distinction on
Humble/Jazzy, not a clock-synchronization guarantee for remote machines. All
previous receipt-rate, sequence, ROS-clock and cleanup checks remain required.

Before these numbered messages, the publisher sends a separate warmup marker
until the receiver confirms a real DDS receipt. Discovery endpoint counts alone
did not establish first-message delivery: an initial Humble run observed a
122.9 ms source-to-receipt difference for message 0, while the remaining messages
were roughly 0.06–0.12 ms. That failed run is retained. The readiness handshake
establishes the measurement precondition without relaxing the 30-message or
timing checks and without fixed startup sleeps.

A live replay of the original public camera/Clock bag retained the independent
RELIABLE receiver's strict 70/70-frame gate with zero duplicates. Ten measured
agent snapshots showed DDS stamp gap p95 3.9–4.0 ms, local probe wait p95 13.0 ms,
and ROS header age p95 335–357 ms as distinct observations. Agent probes remained
BEST_EFFORT, so the reliable control does not establish their completeness.
Actual stdio MCP returned the unchanged snapshot and the distinct columns with
unknown clock synchronization; older bag/live recordings showed no DDS gap.
Owned replay and measurement groups were reaped. Derived checks and source/code
hashes are in [probe-source-gap-validation.json](probe-source-gap-validation.json).
These are one controlled replay's observations, not a performance regression
benchmark or a measurement of the original Autoware publisher's execution.
