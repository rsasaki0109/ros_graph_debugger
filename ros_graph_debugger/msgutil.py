"""Best-effort message helpers.

Kept dependency-free (pure attribute access) so it can be unit-tested without
ROS by passing a duck-typed message object.
"""

from __future__ import annotations


def header_stamp_age_ms(msg, now_epoch: float):
    """Age in milliseconds of a message's header.stamp relative to ``now_epoch``.

    Returns None when the message has no usable ``header.stamp`` (no header,
    field missing, or an unset zero stamp). A slightly negative age (clock skew)
    is clamped to 0. This is the cheap "Tier A" latency proxy — it reflects how
    old the data in a message is, not a true end-to-end trace.
    """
    header = getattr(msg, 'header', None)
    if header is None:
        return None
    stamp = getattr(header, 'stamp', None)
    if stamp is None:
        return None
    sec = getattr(stamp, 'sec', None)
    nanosec = getattr(stamp, 'nanosec', None)
    if sec is None or nanosec is None:
        return None
    t = sec + nanosec * 1e-9
    if t <= 0.0:  # unset stamp (0) — not meaningful
        return None
    age_ms = (now_epoch - t) * 1000.0
    return 0.0 if age_ms < 0 else age_ms


def has_header(msg) -> bool:
    return getattr(msg, 'header', None) is not None
