"""Deliver middleware receive timestamps to raw probes on Humble and Jazzy."""

from typing import NamedTuple

from rclpy.executors import SingleThreadedExecutor
from rclpy.subscription import Subscription


_NATIVE_MESSAGE_INFO = hasattr(Subscription, 'CallbackType')


class _ReceivedMessage(NamedTuple):
    raw: bytes
    info: dict


def with_message_info(callback):
    """Use the native two-argument callback when supported by rclpy."""
    if _NATIVE_MESSAGE_INFO:
        return callback

    def legacy(raw):
        if isinstance(raw, _ReceivedMessage):
            callback(raw.raw, raw.info)
        else:
            # DebuggerNode also works with an ordinary Humble executor.
            callback(raw, None)

    legacy._rgd_message_info = True
    return legacy


class ProbeExecutor(SingleThreadedExecutor):
    """Run collectors on one spin thread without worker-pool dispatch.

    Collectors already share a mutually exclusive callback group. Dispatching
    them through a pool adds contention with high-frequency /clock and TF and
    can make the observer drop messages while other subscribers keep up.
    """

    def _take_subscription(self, subscription):
        # Humble takes (message, info) from RMW but discards info before calling
        # the callback. Adapt only our marked raw probes; every other entity
        # and newer rclpy's native callback dispatch stay on the base path.
        if _NATIVE_MESSAGE_INFO or not getattr(
                subscription.callback, '_rgd_message_info', False):
            return super()._take_subscription(subscription)
        with subscription.handle:
            taken = subscription.handle.take_message(subscription.msg_type, subscription.raw)
        return _ReceivedMessage(*taken) if taken is not None else None
