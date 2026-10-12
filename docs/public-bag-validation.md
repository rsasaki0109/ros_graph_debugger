# Public bag evidence and clock domains

These observations use unchanged public archives downloaded on 2026-10-12.
Raw recordings remain local research inputs. The hashes below are local
fingerprints; the linked distribution documents do not publish reference
checksums. HTTPS transfer, published byte counts, archive integrity and SQLite
integrity/counts were checked separately.

| Archive | Source | Bytes | Local SHA-256 |
|---|---|---:|---|
| DLR `sample_bag.tar.zst` | [TIER IV setup](https://github.com/tier4/driving_log_replayer_v2/blob/38d08c178663a28a558158797fa323544de53602/docs/quick_start/setup.en.md), [public file](https://drive.google.com/file/d/17ppdMKi4IC8J_2-_9nyYv-LAfW0M1re5/view) | 1285540228 | `8d0382c7b1f12b9a0bacc0682ba65dc2ebe0c676cbcc4c3fe094a2b9b87dac07` |
| Real-world AR tag sample ZIP | [Autoware README](https://github.com/autowarefoundation/autoware_universe/blob/dc1fcea1d7ccae851466e7c4db3843c08304c6a5/localization/autoware_landmark_based_localizer/autoware_ar_tag_based_localizer/README.md), [public file](https://drive.google.com/file/d/1VQCQ_qiEZpCMI3-z6SNs__zJ-4HJFQjx/view) | 956306836 | `ad822cb4982cb7073eba662a31238a1c58a1ea81b0f10d82d1bc3b754b24f001` |

The DLR archive contains three input bags (AR tag, eagleye, yabloc), totaling
25,511 messages and 217 RGD snapshots. Independent SQLite/CDR checks matched
1,423 rate/bandwidth windows, 2,846 header-age fields and 1,596 static TF edges.
Empty SQLite catalog rows omitted from metadata have zero messages; every
listed topic's type/count and each duration matches the source. The initial
pose has an unset stamp. Its AWSIM reference poses use `base_link`, while the
biased input uses `map`; no position-accuracy claim is made without a verified
frame transform. These inputs do not include diagnostics, processing times or
CTF traces.

## Re-recorded real-world sample

The Autoware source describes this sample as real-world data with missing IMU
data and low localization accuracy. The bag contains 10,489 messages over
88.24 seconds, including 8,809 `/clock` messages, camera images, derived twist,
an initial pose, map markers and static TF. No IMU messages are recorded; this
does not reconstruct the original live publishers or prove why data is absent.

The first recorded receive time is `1690184194.048663`, while its clock value is
`1685944792.247460`. Sensor stamps follow that recorded ROS clock. Subtracting
sensor stamps from receive time treats approximately 49 calendar days between
the original data and its later recording as delay.

```bash
rgd bag analyze /path/to/ar_tag_based_localizer_sample_bag \
  --profile autoware --out actual.rgd.json
rgd report actual.rgd.json --md actual.md

# Explicitly reproduce the old receive-time basis for comparison.
rgd bag analyze /path/to/ar_tag_based_localizer_sample_bag \
  --profile autoware --age-clock bag --out receive-time.rgd.json
```

Using recorded ROS time changes camera p95 age from
`4239404695.1–4239406142.0 ms` to `19.4–1463.2 ms`. Genuine large delays remain.
All 1,232 age fields in 88 snapshots matched independently decoded clock/header
CDR records. Snapshot timestamps, all other topic fields and TF remain unchanged.
The selected basis is saved in the recording's first-line `header_age_clock`.

The generic assertion returns zero both before and after: the profile does not
set an age expectation for this original camera name. This result does not
certify the stack as healthy. The record lacks the original node graph, process
CPU, diagnostics and callback traces; source frames and missing inputs require
separate interpretation.

## Clock contract in CI

The mandatory Humble/Jazzy scenario records actual separate-process `/clock`
and Odometry publications with `ros2 bag record`. It verifies healthy 20 ms
stamps, real 300 ms delay, recovery, and both states with the clock paused,
using the existing 30 Hz / 100 ms contract. Pure regressions cover unstarted,
zero, malformed and backward-moving clocks, subsecond stamps, explicit clock
selection and unchanged arrival windows. ROS time is selected from a recorded
standard `/clock`, not by choosing whichever age is smaller. System-stamped
recordings can explicitly select `--age-clock bag`; `ros` requires `/clock`.
