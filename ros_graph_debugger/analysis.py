"""Rule-based issue engine and bottleneck analyzer.

This is the part that makes ros_graph_debugger more than a viewer: it turns raw
graph + metrics into a ranked list of human- and AI-readable issues, each with
evidence and suggested actions. Keep rules cheap and explainable.
"""

from __future__ import annotations

import time

from .model import (
    CRITICAL,
    INFO,
    WARNING,
    Issue,
    RuntimeGraphStore,
)

_SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}


def analyze(store: RuntimeGraphStore, thresholds) -> list[Issue]:
    nodes, topics, tf, diagnostics = store.working_copy()
    issues: list[Issue] = []
    now = time.time()
    counter = _Counter()

    # Recompute per-topic status while we walk the rules so the graph edges
    # reflect the same verdicts the issue list reports.
    for name, t in topics.items():
        status = 'ok' if (t.publisher_count and t.subscriber_count) else 'unknown'

        # 1 / 2: dangling connections.
        if t.subscriber_count and not t.publisher_count:
            status = WARNING
            issues.append(Issue(
                id=counter.next('no_publisher'), severity=WARNING,
                kind='no_publisher',
                title=f'{name} has subscribers but no publisher',
                explanation='Subscribers are waiting for data that nobody '
                            'publishes.',
                evidence=[f'subscribers: {t.subscriber_count}',
                          'publishers: 0'],
                suggested_actions=['Start the expected publisher',
                                   'Check remapping and namespaces'],
                related_topics=[name], related_nodes=list(t.subscribers)))
        elif t.publisher_count and not t.subscriber_count:
            issues.append(Issue(
                id=counter.next('no_subscriber'), severity=INFO,
                kind='no_subscriber',
                title=f'{name} has publishers but no subscriber',
                explanation='Data is being published but nothing consumes it.',
                evidence=[f'publishers: {t.publisher_count}',
                          'subscribers: 0'],
                suggested_actions=['This may be intentional (debug/log topic)'],
                related_topics=[name], related_nodes=list(t.publishers)))

        # 3: QoS mismatch (connected in graph but data may not flow).
        if t.qos_status == 'mismatch':
            status = CRITICAL
            issues.append(Issue(
                id=counter.next('qos_mismatch'), severity=CRITICAL,
                kind='qos_mismatch',
                title=f'QoS mismatch on {name}',
                explanation='A subscriber requests a stronger guarantee than '
                            'the publisher offers, so messages will not be '
                            'delivered.',
                evidence=_qos_evidence(t),
                suggested_actions=['Align reliability (reliable vs best_effort)',
                                   'Align durability (volatile vs transient_local)'],
                related_topics=[name],
                related_nodes=list(t.publishers) + list(t.subscribers)))

        # 4: stale (probed but no recent message).
        if t.probed and t.last_seen_time is not None:
            age = (now - t.last_seen_time) * 1000.0
            t.age_ms = round(age, 1)
            if age > thresholds.stale_topic_ms:
                status = CRITICAL
                issues.append(Issue(
                    id=counter.next('topic_stale'), severity=CRITICAL,
                    kind='topic_stale',
                    title=f'{name} is stale',
                    explanation='No messages observed recently on a topic that '
                                'was active.',
                    evidence=[f'last message: {age:.0f} ms ago',
                              f'expected within: {thresholds.stale_topic_ms:.0f} ms'],
                    suggested_actions=['Inspect the publishing node',
                                       'Check upstream inputs and CPU load'],
                    related_topics=[name], related_nodes=list(t.publishers)))

        # 5: rate below profile expectation (exact topic or matching pattern).
        expected = thresholds.min_rate_for(name)
        if expected and t.probed and t.rate_hz is not None \
                and t.rate_hz < expected:
            status = WARNING if status not in (CRITICAL,) else status
            issues.append(Issue(
                id=counter.next('rate_drop'), severity=WARNING,
                kind='rate_drop',
                title=f'{name} rate below expectation',
                explanation=f'Observed {t.rate_hz:.1f} Hz, expected '
                            f'>= {expected:.1f} Hz.',
                evidence=[f'observed: {t.rate_hz:.1f} Hz',
                          f'expected: {expected:.1f} Hz'],
                suggested_actions=['Inspect the publishing node callback',
                                   'Check upstream topic freshness'],
                related_topics=[name], related_nodes=list(t.publishers)))

        # 5b: data older than the profile's max-age expectation (latency Tier A).
        max_age = thresholds.max_age_for(name)
        if max_age and t.probed and t.header_age_p95_ms is not None \
                and t.header_age_p95_ms > max_age:
            if status != CRITICAL:
                status = WARNING
            issues.append(Issue(
                id=counter.next('stale_data'), severity=WARNING,
                kind='stale_data',
                title=f'{name} data is older than expected',
                explanation=f'p95 message age {t.header_age_p95_ms:.0f} ms exceeds '
                            f'the expected {max_age:.0f} ms; consumers may act on '
                            'stale data.',
                evidence=[f'p95 header age: {t.header_age_p95_ms:.0f} ms',
                          f'expected within: {max_age:.0f} ms'],
                suggested_actions=['Inspect the publishing pipeline latency',
                                   'Check upstream input freshness and CPU load'],
                related_topics=[name], related_nodes=list(t.publishers)))

        # 6: high bandwidth.
        if t.bandwidth_bps and t.bandwidth_bps > thresholds.high_bandwidth_bps:
            issues.append(Issue(
                id=counter.next('high_bandwidth'), severity=INFO,
                kind='high_bandwidth',
                title=f'{name} bandwidth is high',
                explanation='High transport pressure; may stress the network '
                            'or executor.',
                evidence=[f'bandwidth: {t.bandwidth_bps/1e6:.1f} MB/s'],
                suggested_actions=['Consider downsampling or compression'],
                related_topics=[name]))

        # 7: large messages.
        if t.p95_msg_size_bytes and \
                t.p95_msg_size_bytes > thresholds.large_msg_bytes:
            issues.append(Issue(
                id=counter.next('large_message'), severity=INFO,
                kind='large_message',
                title=f'{name} carries large messages',
                explanation='Large per-message payloads can cause queue buildup '
                            'and latency.',
                evidence=[f'p95 size: {t.p95_msg_size_bytes/1e6:.2f} MB'],
                suggested_actions=['Check resolution / density of the payload'],
                related_topics=[name]))

        t.status = status

    # 8 / 9: TF staleness.
    for (parent, child), edge in tf.items():
        if edge.status == CRITICAL:
            issues.append(Issue(
                id=counter.next('tf_stale'), severity=CRITICAL,
                kind='tf_stale',
                title=f'TF {parent} -> {child} is stale',
                explanation='The transform has not been updated recently; '
                            'consumers may use an outdated pose.',
                evidence=[f'age: {edge.age_ms:.0f} ms' if edge.age_ms
                          else 'no recent update'],
                suggested_actions=['Inspect the broadcaster for this transform',
                                   'Check localization / sensor input freshness'],
                related_frames=[parent, child]))

    # 10: diagnostics WARN/ERROR.
    for name, st in diagnostics.items():
        if st.level >= 2:
            issues.append(Issue(
                id=counter.next('diagnostics'), severity=CRITICAL,
                kind='diagnostics_error',
                title=f'Diagnostics ERROR: {name}',
                explanation=st.message or 'Component reported an error.',
                evidence=[f'level: {st.level}', f'hardware_id: {st.hardware_id}'],
                suggested_actions=['Inspect the reporting component'],
                related_nodes=[name]))
        elif st.level == 1:
            issues.append(Issue(
                id=counter.next('diagnostics'), severity=WARNING,
                kind='diagnostics_warn',
                title=f'Diagnostics WARN: {name}',
                explanation=st.message or 'Component reported a warning.',
                evidence=[f'level: {st.level}'],
                suggested_actions=['Inspect the reporting component'],
                related_nodes=[name]))

    # 11: high CPU / RSS.
    for node_id, n in nodes.items():
        if n.cpu_percent is not None \
                and n.cpu_percent > thresholds.high_cpu_percent:
            n.status = WARNING
            issues.append(Issue(
                id=counter.next('high_cpu'), severity=WARNING,
                kind='high_cpu',
                title=f'{n.name} CPU is high',
                explanation='Process is CPU-bound; may be a bottleneck.',
                evidence=[f'CPU: {n.cpu_percent:.0f}%',
                          f'mapping confidence: {n.process_mapping_confidence}'],
                suggested_actions=['Profile the node callbacks',
                                   'Check executor / thread configuration'],
                related_nodes=[node_id]))

    # 14: bottleneck inference — healthy input, hot node, dropping output.
    issues.extend(_infer_bottlenecks(nodes, topics, thresholds))

    issues.sort(key=lambda i: _SEVERITY_ORDER.get(i.severity, 9))
    return issues


def _infer_bottlenecks(nodes, topics, thresholds) -> list[Issue]:
    """Flag a node as a likely bottleneck when its inputs look healthy, it is
    CPU-hot, and one of its outputs has dropped below expectation."""
    out: list[Issue] = []
    counter = _Counter('bottleneck')
    for node_id, n in nodes.items():
        hot = n.cpu_percent is not None \
            and n.cpu_percent > thresholds.high_cpu_percent
        # Find a slow output.
        slow_topic = None
        slow_expected = None
        for pub in n.publishers:
            t = topics.get(pub)
            if not t or not t.probed:
                continue
            expected = thresholds.min_rate_for(pub)
            if expected and t.rate_hz is not None and t.rate_hz < expected:
                slow_topic = t
                slow_expected = expected
                break
        if not slow_topic:
            continue
        # Are inputs healthy?
        healthy_inputs = []
        for sub in n.subscribers:
            t = topics.get(sub)
            if t and t.probed and t.rate_hz:
                healthy_inputs.append(f'{sub}: {t.rate_hz:.1f} Hz')
        evidence = [f'{slow_topic.name}: {slow_topic.rate_hz:.1f} Hz '
                    f'(expected >= {slow_expected:.1f})']
        if n.cpu_percent is not None:
            evidence.append(f'{n.name} CPU: {n.cpu_percent:.0f}%')
        evidence += healthy_inputs[:3]
        severity = CRITICAL if hot else WARNING
        out.append(Issue(
            id=counter.next('likely'), severity=severity,
            kind='bottleneck',
            title=f'Likely bottleneck: {n.name}',
            explanation=f'{n.name} output {slow_topic.name} dropped below '
                        f'expectation while its inputs look healthy'
                        + (' and it is CPU-bound.' if hot else '.'),
            evidence=evidence,
            suggested_actions=['Inspect this node\'s callback duration',
                               'Check CPU/GPU utilization of its process',
                               'Reduce input load (downsample) if applicable'],
            related_nodes=[node_id], related_topics=[slow_topic.name]))
    return out


def _qos_evidence(topic) -> list[str]:
    lines = []
    for ep in topic.qos_endpoints:
        lines.append(f'{ep.endpoint_type} {ep.node}: '
                     f'{ep.reliability}/{ep.durability}')
    return lines


class _Counter:
    def __init__(self, prefix: str = 'issue') -> None:
        self._prefix = prefix
        self._n = 0

    def next(self, kind: str) -> str:
        self._n += 1
        return f'{self._prefix}_{kind}_{self._n}'
