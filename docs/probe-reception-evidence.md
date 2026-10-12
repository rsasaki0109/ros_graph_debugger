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
