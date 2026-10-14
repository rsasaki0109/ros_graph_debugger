"""Inspect MetricArray dumps without inventing graph or timing facts.

Autoware's checker republishes cached module values with a new array stamp.
Those observations are not callback samples or evidence of source freshness.
"""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate field {key!r}')
        result[key] = value
    return result


def _yaml_documents(text):
    try:
        import yaml
    except ImportError as exc:
        raise ValueError('YAML input requires PyYAML (install the existing '
                         'ros_graph_debugger[web] extra), or use JSON') from exc

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise ValueError('YAML field names must be scalar') from exc
            if duplicate:
                raise ValueError(f'duplicate field {key!r}')
            result[key] = loader.construct_object(value_node, deep=True)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                                 mapping)
    try:
        return [doc for doc in yaml.load_all(text, Loader=UniqueLoader)
                if doc is not None]  # ros2 echo leaves a trailing separator
    except yaml.YAMLError as exc:
        raise ValueError(f'invalid YAML: {exc}') from exc


def read_dump(path: str, input_format: str = 'auto') -> list[dict]:
    """Read a JSON object/list or ROS echo YAML documents; reject ambiguity."""
    text = Path(path).read_text(encoding='utf-8')
    if not text.strip():
        raise ValueError('input contains no MetricArray messages')
    if input_format == 'auto':
        input_format = 'json' if Path(path).suffix.lower() == '.json' or \
            text.lstrip().startswith(('{', '[')) else 'yaml'
    if input_format == 'json':
        obj = json.loads(text, object_pairs_hook=_unique_json_object)
        documents = obj if isinstance(obj, list) else [obj]
    elif input_format == 'yaml':
        documents = _yaml_documents(text)
    else:
        raise ValueError('input format must be auto, json or yaml')
    if not documents:
        raise ValueError('input contains no MetricArray messages')
    return documents


def _stamp(msg):
    if 'stamp' not in msg:
        return None, 'missing'
    stamp = msg['stamp']
    if isinstance(stamp, dict):
        sec, ns = stamp.get('sec'), stamp.get('nanosec')
        if type(sec) is int and -(2 ** 31) <= sec < 2 ** 31 and \
                type(ns) is int and 0 <= ns < 10 ** 9:
            return {'sec': sec, 'nanosec': ns}, 'present'
    return None, 'invalid'


def _duration(entry):
    name, unit, value = (entry.get(k) for k in ('name', 'unit', 'value'))
    if not isinstance(name, str) or not name:
        return None, 'invalid_name'
    if not name.startswith('processing_time/') or name == 'processing_time/':
        return None, 'not_processing_time'
    if unit != 'millisecond':
        return None, 'unsupported_unit'
    if not isinstance(value, str):
        return None, 'invalid_value_type'
    try:
        duration = float(value)
    except ValueError:
        return None, 'non_numeric_value'
    if not math.isfinite(duration):
        return None, 'non_finite_value'
    if duration < 0:
        return None, 'negative_duration'
    return duration, None


def inspect_arrays(documents: list[dict], topic: str | None = None) -> dict:
    """Keep every observation, including duplicates and rejected entries.

    A supplied topic is context, not verified original ownership. No callback
    statistics, graph nodes, budgets, deduplication or health verdict inferred.
    """
    if not documents:
        raise ValueError('input contains no MetricArray messages')
    messages = []
    entries = accepted = 0
    for index, doc in enumerate(documents):
        if not isinstance(doc, dict) or not isinstance(doc.get('metric_array'), list):
            raise ValueError(f'message {index}: expected a metric_array list')
        array = doc['metric_array']
        if not all(isinstance(e, dict) for e in array):
            raise ValueError(f'message {index}: metric entries must be objects')
        names = Counter(e.get('name') for e in array
                        if isinstance(e.get('name'), str))
        rows = []
        for entry_index, entry in enumerate(array):
            duration, reason = _duration(entry)
            accepted += reason is None
            entries += 1
            rows.append({
                'entry_index': entry_index,
                'name': entry.get('name') if isinstance(entry.get('name'), str) else None,
                'unit': entry.get('unit') if isinstance(entry.get('unit'), str) else None,
                'value_text': entry.get('value') if isinstance(entry.get('value'), str) else None,
                'processing_time_ms': duration,
                'status': 'valid_duration' if reason is None else 'unusable_duration',
                'reason': reason,
                'duplicate_name': names.get(entry.get('name'), 0) > 1
                    if isinstance(entry.get('name'), str) else False,
                'node': None,
                'source_topic': None,
                'measurement_stamp': None,
            })
        stamp, stamp_status = _stamp(doc)
        messages.append({'message_index': index, 'array_stamp': stamp,
                         'array_stamp_status': stamp_status, 'metrics': rows})
    return {
        'format': 'rgd.metric-array-inspection.v1',
        'topic_context': topic,
        'topic_context_source': 'user_supplied' if topic is not None else None,
        'measurement_scope': 'cached_metric_observation',
        'node_ownership': 'unknown',
        'source_freshness': 'unknown',
        'source_measurement_count': None,
        'counts': {'messages': len(messages), 'entries': entries,
                   'valid_duration_observations': accepted,
                   'unusable_duration_observations': entries - accepted},
        'messages': messages,
    }
