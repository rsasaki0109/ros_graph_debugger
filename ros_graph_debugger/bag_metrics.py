"""Read known processing MetricArray CDR1 without custom message bindings.

Both supported official schemas are Time stamp + Metric[] metric_array, where
Metric has three strings: name, unit, value. Never infer a schema from a topic.
"""

from __future__ import annotations

import struct

from .bag_analyze import BagReaderUnavailable, _storage_id
from .metric_arrays import inspect_arrays

METRIC_ARRAY_TYPES = frozenset({
    'tier4_metric_msgs/msg/MetricArray',
    'autoware_internal_metric_msgs/msg/MetricArray',
})


class _Cdr:
    def __init__(self, raw: bytes):
        if len(raw) < 4 or raw[:4] not in (b'\x00\x00\x00\x00', b'\x00\x01\x00\x00'):
            raise ValueError('MetricArray needs plain CDR1 with zero encapsulation options')
        self.order = '<' if raw[1] == 1 else '>'
        self.raw = raw[4:]
        self.pos = 0

    def integer(self, signed=False):
        self.pos += (-self.pos) % 4
        if len(self.raw) - self.pos < 4:
            raise ValueError('truncated MetricArray CDR integer')
        result, = struct.unpack_from(self.order + ('i' if signed else 'I'), self.raw, self.pos)
        self.pos += 4
        return result

    def string(self):
        length = self.integer()
        if length < 1 or length > len(self.raw) - self.pos:
            raise ValueError('invalid MetricArray CDR string length')
        chunk = self.raw[self.pos:self.pos + length]
        self.pos += length
        if chunk[-1] != 0 or b'\0' in chunk[:-1]:
            raise ValueError('invalid MetricArray CDR string terminator')
        try:
            return chunk[:-1].decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('invalid MetricArray CDR UTF-8 string') from exc


def decode_metric_array(raw: bytes) -> dict:
    """Strictly decode the fixed official schemas; reject other encodings."""
    cdr = _Cdr(raw)
    stamp = {'sec': cdr.integer(signed=True), 'nanosec': cdr.integer()}
    count = cdr.integer()
    # Each entry needs at least three uint32 lengths, so reject impossible
    # counts before looping. Exact string/alignment bounds are checked below.
    if count > (len(cdr.raw) - cdr.pos) // 12:
        raise ValueError('invalid MetricArray CDR sequence length')
    metrics = [{'name': cdr.string(), 'unit': cdr.string(), 'value': cdr.string()}
               for _ in range(count)]
    # C++ may pad the final aggregate to its 4-byte alignment, while the
    # Python serializer stops at the last string. Padding has no value.
    remaining = len(cdr.raw) - cdr.pos
    if remaining not in (0, (-cdr.pos) % 4):
        raise ValueError('unexpected trailing MetricArray CDR bytes')
    return {'stamp': stamp, 'metric_array': metrics}


def inspect_metric_bag(path: str, topic: str) -> dict:
    """Inspect one explicit recorded topic, preserving bag and array clocks."""
    try:
        import rosbag2_py
    except ImportError as exc:
        raise BagReaderUnavailable('bag metrics needs a sourced ROS 2 install '
                                   '(rosbag2_py); custom MetricArray bindings are not required') from exc

    storage = _storage_id(path)
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=path, storage_id=storage),
                rosbag2_py.ConverterOptions(input_serialization_format='cdr',
                                            output_serialization_format='cdr'))
    selected = [t for t in reader.get_all_topics_and_types() if t.name == topic]
    if not selected:
        raise ValueError(f'bag has no topic {topic!r}')
    if len(selected) != 1:
        raise ValueError(f'bag topic {topic!r} has ambiguous metadata')
    metadata = selected[0]
    if metadata.type not in METRIC_ARRAY_TYPES:
        raise ValueError(f'unsupported MetricArray type {metadata.type!r}')
    if metadata.serialization_format != 'cdr':
        raise ValueError(f'unsupported serialization {metadata.serialization_format!r}')

    reader.set_filter(rosbag2_py.StorageFilter(topics=[topic]))
    documents, receipts = [], []
    while reader.has_next():
        name, raw, received_ns = reader.read_next()
        if name != topic:
            continue
        try:
            documents.append(decode_metric_array(raw))
        except ValueError as exc:
            raise ValueError(f'{topic} message {len(documents)}: {exc}') from exc
        receipts.append(int(received_ns))
    if not documents:
        raise ValueError(f'bag topic {topic!r} contains no recorded messages')
    result = inspect_arrays(documents, topic)
    result['topic_context_source'] = 'bag_metadata'
    result['bag_source'] = {
        'type': metadata.type, 'storage_identifier': storage,
        'serialization_format': 'cdr', 'message_timestamp_clock': 'bag_recording',
        'delivery_completeness': 'unknown',
    }
    for message, received_ns in zip(result['messages'], receipts):
        message['bag_received_timestamp_ns'] = received_ns
    return result
