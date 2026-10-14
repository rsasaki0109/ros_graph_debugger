# Application processing-time input validation

Processing-time topics contain publisher-defined measurements in milliseconds,
not necessarily the start/end duration of one callback. RGD accepts finite,
non-negative numeric `data` values. Boolean values, missing/non-numeric data,
negative values, NaN and positive/negative infinity are rejected. Zero is a
valid measurement; rejecting an unusable value never substitutes zero.

The live collector and bag windows share this validation. Rolling statistics
contain up to 100 **accepted** measurements per topic. Rejected values do not
increment the sample count or evict previous accepted measurements. A window
with no accepted measurements produces no processing statistic. An absent
statistic is unmeasured, not evidence of healthy execution. Existing values can
remain after rejected messages; this change does not add a freshness guarantee
or rejected-sample counter. A valid slow window retains its slow-callback issue
even while invalid data is received. Existing budgets and source attribution
(`application_report`) are unchanged.

Means use overflow-safe averaging so representable finite inputs remain
representable even when their intermediate sum would overflow. This protects
the standard JSON snapshot response from non-finite output. It does not make
extreme values physically plausible or determine the publisher's semantics.

Humble/Jazzy CI uses two real ROS 2 processes: a Float64 publisher and the real
collector/server, plus an independent subscriber in the receiving process.
The scenario starts with a 50 ms slow control sample, sends infinity/NaN/negative
samples, requires valid HTTP 200 snapshots with the original 50 ms evidence,
fills the window with maximum finite doubles, and then requires recovery to
1 ms with no matching slow-callback issue after 100 accepted samples. This is
controlled invalid-data injection, not a fault observed in a public bag.
The publisher and collector use unmodified production thresholds.

Run instructions and artifacts are in
[CONTRIBUTING](../CONTRIBUTING.md#multiprocess-ros-integration).
No new HTTP routes, snapshot fields or required dependencies are introduced.
Autoware MetricArray values and module labels require their own explicit unit
and node-ownership handling; this scalar validation does not claim support for
those arrays or infer graph nodes from metric names.
