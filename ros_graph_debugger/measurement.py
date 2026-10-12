"""Name rate observation locations without guessing from legacy source fields."""

RATE_SCOPE_LABELS = {
    'probe_receipt': 'probe receipts',
    'subscriber_statistics': 'subscriber statistics',
    'bag_recording': 'bag recording',
    'unknown': 'unknown',
}


def rate_measurement_scope(topic: dict) -> str:
    scope = topic.get('rate_measurement_scope')
    return scope if isinstance(scope, str) and scope in RATE_SCOPE_LABELS else 'unknown'


RATE_DELIVERY_NOTE = (
    'Rates describe observed messages. Delivery completeness and publisher-side '
    'throughput are unknown; a low rate alone does not prove a slow publishing callback.'
)
