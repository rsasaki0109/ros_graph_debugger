"""Prepare and score frozen investigation tasks; no model or MCP SDK required.

Scoring covers structured claims and citations, not free-text reasoning or AI
accuracy. Participant inputs and evaluator rubrics are kept in separate trees.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path

from ros_graph_debugger.recording import make_header, read_recording, write_recording

OBJECTS = '/perception/object_recognition/objects'
IMAGE = '/sensing/camera/front/image'
PROCESSING_NODE = '/control/integration/processing'
DURATION_SOURCES = ('trace', 'application_report', 'synthetic')
TOPIC_LIMITS = {
    'publisher_execution_from_rate_or_header_age': 'collect_publisher_callback_trace',
    'network_latency_from_dds_stamp_gap': 'check_capture_points_and_clock_sync',
    'complete_delivery_from_probe_rate': 'compare_publisher_identity_and_receipt_sequences',
    'clock_sync_from_positive_stamp_gap': 'verify_host_clock_synchronization',
    'end_to_end_p95_from_summed_percentiles': 'collect_paired_per_message_timings',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def pointer(document, path):
    """Resolve a JSON Pointer, including escaped keys; reject invalid indexes."""
    if not path.startswith('/'):
        raise ValueError('citation must be an absolute JSON Pointer')
    value = document
    for part in path[1:].split('/'):
        if '~' in part.replace('~0', '').replace('~1', ''):
            raise ValueError('invalid pointer escape')
        key = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            if not key.isdigit() or (len(key) > 1 and key.startswith('0')):
                raise ValueError('invalid array index')
            value = value[int(key)]
        else:
            value = value[key]
    return value


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def field_claim(evidence, ident, path, numeric=False, allowed=None):
    try:
        value = pointer(evidence, path)
    except (KeyError, TypeError):
        value = None
    known = value is not None
    if numeric:
        known = number(value)
    if allowed is not None:
        known = value in allowed
    citation = path
    if not known:
        citation = path.rsplit('/', 1)[0]
        while citation:
            try:
                pointer(evidence, citation)
                break
            except (KeyError, TypeError):
                citation = citation.rsplit('/', 1)[0]
        assert citation, 'unknown fact needs an existing parent citation'
    return {'id': ident, 'status': 'observed' if known else 'unknown',
            'value': value if known else None,
            'evidence': [citation]}


def rubric_for(case_id, evidence, family, target):
    snapshot = evidence['snapshot']
    claims = []
    if family == 'duration':
        callbacks = snapshot.get('callbacks')
        indexes = [i for i, c in enumerate(callbacks) if isinstance(c, dict) and c.get('node') == target] \
            if isinstance(callbacks, list) else []
        claims.append({'id': 'duration_observation_count',
                       'status': 'observed' if isinstance(callbacks, list) else 'unknown',
                       'value': len(indexes) if isinstance(callbacks, list) else None,
                       'evidence': ['/snapshot/callbacks' if isinstance(callbacks, list) else '/snapshot']})
        limits = {'end_to_end_p95_from_duration_percentiles': 'collect_paired_per_message_timings'}
        if not indexes:
            limits['no_callback_work_from_missing_observations'] = 'collect_attributed_callback_trace'
        for index in indexes:
            root = f'/snapshot/callbacks/{index}'
            prefix = f'duration_{index}'
            for key in ('node', 'callback', 'topic'):
                claims.append(field_claim(evidence, prefix + '_' + key, root + '/' + key))
            for key in ('count', 'mean_ms', 'p95_ms', 'max_ms'):
                claims.append(field_claim(evidence, prefix + '_' + key, root + '/' + key, numeric=True))
            declared = field_claim(evidence, prefix + '_declared_source', root + '/source',
                                   allowed=DURATION_SOURCES)
            claims.append(declared)
            limits[prefix + '_authenticated_provenance_from_source_field'] = 'verify_original_duration_source'
            limits[prefix + '_downstream_rate_cause_from_p95'] = 'collect_paired_pipeline_measurements'
            source = declared['value']
            if source == 'application_report':
                limits[prefix + '_callback_start_end_from_application_report'] = 'collect_paired_callback_start_end'
            elif source == 'trace':
                limits[prefix + '_application_cycle_time_from_callback_trace'] = 'inspect_application_cycle_boundaries'
            elif source == 'synthetic':
                limits[prefix + '_measured_runtime_duration_from_synthetic_stats'] = 'collect_real_duration_measurements'
            else:
                limits[prefix + '_callback_start_end_from_unknown_source'] = 'establish_duration_measurement_method'
    elif family == 'diagnostics':
        diagnostics = snapshot.get('diagnostics')
        indexes = [i for i, d in enumerate(diagnostics)
                   if isinstance(d, dict) and d.get('name') == target] \
            if isinstance(diagnostics, list) else []
        claims.append({'id': 'diagnostic_observation_count',
                       'status': 'observed' if isinstance(diagnostics, list) else 'unknown',
                       'value': len(indexes) if isinstance(diagnostics, list) else None,
                       'evidence': ['/snapshot/diagnostics' if isinstance(diagnostics, list) else '/snapshot']})
        for index in indexes:
            root = f'/snapshot/diagnostics/{index}'
            for key in ('name', 'level', 'message', 'hardware_id'):
                claims.append(field_claim(evidence, f'diagnostic_{index}_{key}', root + '/' + key))
        # These are declared issue references, not authenticated status origins.
        for index, issue in enumerate(snapshot['issues']):
            if issue.get('kind') not in ('diagnostics_error', 'diagnostics_warn'):
                continue
            root = f'/snapshot/issues/{index}'
            for key in ('kind', 'related_diagnostics', 'related_nodes', 'evidence'):
                claims.append(field_claim(evidence, f'diagnostic_issue_{index}_{key}', root + '/' + key))
        limits = {
            'node_origin_from_task_label_or_sole_graph_publisher': 'collect_per_status_origin_mapping',
            'authenticated_origin_from_declared_issue_references': 'verify_status_origin_provenance',
            'specific_failure_cause_from_diagnostic_level': 'inspect_inputs_and_component_state',
            'whole_system_health_from_one_diagnostic_task': 'check_other_system_contracts',
        }
    elif family == 'cpu':
        index = next(i for i, n in enumerate(snapshot['nodes']) if n['id'] == target)
        root = f'/snapshot/nodes/{index}'
        for key in ('pid', 'cpu_percent'):
            claims.append(field_claim(evidence, key, root + '/' + key, numeric=True))
        claims.append(field_claim(evidence, 'mapping_confidence', root + '/process_mapping_confidence'))
        limits = {'successful_processing_from_cpu': 'check_actual_output_and_workload',
                  'callback_duration_from_cpu': 'collect_publisher_callback_trace'}
        if not number(snapshot['nodes'][index].get('cpu_percent')):
            limits['idle_from_missing_cpu_sample'] = 'wait_for_valid_cpu_baseline'
    elif family == 'nav2':
        for target_node in ('/map_server', '/planner_server', '/controller_server'):
            index = next(i for i, n in enumerate(snapshot['nodes']) if n['id'] == target_node)
            claims.append(field_claim(evidence, target_node + '_state', f'/snapshot/nodes/{index}/lifecycle_state'))
        for name in ('/scan', '/odom'):
            index = next(i for i, t in enumerate(snapshot['topics']) if t['name'] == name)
            claims.append(field_claim(evidence, name + '_age_ms', f'/snapshot/topics/{index}/age_ms', numeric=True))
        for parent, child in (('map', 'odom'), ('odom', 'base_footprint')):
            index = next(i for i, e in enumerate(snapshot['tf_edges']) if (e['parent'], e['child']) == (parent, child))
            claims.append(field_claim(evidence, parent + '_' + child + '_age_ms', f'/snapshot/tf_edges/{index}/age_ms', numeric=True))
        limits = {'message_flow_from_discovered_endpoints': 'check_receipt_freshness',
                  'whole_system_health_from_active_lifecycle': 'check_unmeasured_requirements',
                  'specific_failure_cause_from_stale_delivery': 'check_process_state_and_sender_trace',
                  'idle_cpu_from_historical_nav2_sample': 'collect_corrected_cpu_samples'}
    else:
        index = next(i for i, t in enumerate(snapshot['topics']) if t['name'] == target)
        root = f'/snapshot/topics/{index}'
        for ident, key in (
            ('rate_hz', 'rate_hz'), ('dds_stamp_gap_p95_ms', 'probe_source_receipt_gap_p95_ms'),
            ('local_probe_wait_p95_ms', 'probe_callback_delay_p95_ms'), ('header_age_p95_ms', 'header_age_p95_ms'),
        ):
            claims.append(field_claim(evidence, ident, root + '/' + key, numeric=True))
        claims.append(field_claim(evidence, 'rate_scope', root + '/rate_measurement_scope',
                                  allowed=('probe_receipt', 'bag_recording', 'subscriber_statistics')))
        claims.append(field_claim(evidence, 'recorded_header_age_clock',
                                  '/recording_metadata/recording_header/header_age_clock/selected', allowed=('ros', 'bag')))
        limits = dict(TOPIC_LIMITS)
        if family == 'topic_window':
            for key in ('probe_rate_window_samples', 'probe_rate_window_duration_ms',
                        'probe_last_sample_age_ms'):
                claims.append(field_claim(evidence, key, root + '/' + key, numeric=True))
            claims.append(field_claim(evidence, 'probe_rate_window_clock',
                                      root + '/probe_rate_window_clock',
                                      allowed=('rmw_received_timestamp', 'callback_monotonic')))
            limits.update({
                'current_delivery_from_retained_rate_window': 'compare_independent_receipts_over_time',
                'dds_receipt_age_from_processed_sample_age': 'inspect_raw_receipt_and_processing_timestamps',
                'publisher_activity_from_processed_sample_age': 'collect_publisher_identity_and_activity',
            })
    claims.append({'id': 'issue_kinds', 'status': 'observed',
                   'value': sorted({i['kind'] for i in snapshot['issues']}), 'evidence': ['/snapshot/issues']})
    return {'case_id': case_id, 'evidence_sha256': digest(evidence), 'claims': claims,
            'limitations': [{'id': ident, 'status': 'not_established', 'next_check': check}
                            for ident, check in limits.items()]}


def equal_value(actual, expected):
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return number(actual) and actual == expected
    return type(actual) is type(expected) and actual == expected


def score(evidence, rubric, answer):
    """Reject false values, mismatched sources/citations and unsupported claims."""
    errors = []
    for key in ('case_id', 'evidence_sha256'):
        if answer.get(key) != rubric[key]:
            errors.append(key + ': wrong evidence identity')
    if digest(evidence) != rubric['evidence_sha256']:
        errors.append('evidence changed after task preparation')
    for section in ('claims', 'limitations'):
        supplied = answer.get(section)
        if not isinstance(supplied, list) or any(not isinstance(c, dict) for c in supplied):
            errors.append(section + ': expected a list of objects')
            continue
        ids = [c.get('id') for c in supplied]
        expected = {c['id']: c for c in rubric[section]}
        if any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)) or set(ids) != set(expected):
            errors.append(section + ': missing, duplicate or unsupported claim')
            continue
        for item in supplied:
            wanted = expected[item['id']]
            if item.get('status') != wanted['status']:
                errors.append(item['id'] + ': unsupported status')
            if section == 'claims':
                if 'value' not in item or not equal_value(item.get('value'), wanted['value']):
                    errors.append(item['id'] + ': value does not match the cited observation')
                if item.get('evidence') != wanted['evidence']:
                    errors.append(item['id'] + ': citation refers to the wrong measurement')
                else:
                    for path in item['evidence']:
                        try:
                            pointer(evidence, path)
                        except (KeyError, IndexError, TypeError, ValueError):
                            errors.append(item['id'] + ': citation does not resolve')
            elif item.get('next_check') != wanted['next_check']:
                errors.append(item['id'] + ': next check does not address this evidence gap')
    return {'passed': not errors, 'errors': errors, 'scope': 'structured claims and citations; reasoning text unscored'}


def negative_controls(answer):
    """Deliberately incorrect structured answers; never counted as model outputs."""
    cases = {}
    bad = copy.deepcopy(answer); bad['evidence_sha256'] = '0' * 64; cases['wrong_frame'] = bad
    bad = copy.deepcopy(answer); bad['claims'][0]['evidence'] = ['/snapshot/timestamp']; cases['wrong_citation'] = bad
    bad = copy.deepcopy(answer); bad['claims'].pop(); cases['missing_fact'] = bad
    bad = copy.deepcopy(answer); bad['claims'].append(copy.deepcopy(bad['claims'][0])); cases['duplicate_fact'] = bad
    bad = copy.deepcopy(answer); bad['claims'][0]['value'] = 'fabricated'; cases['invented_value'] = bad
    bad = copy.deepcopy(answer); bad['limitations'][0]['status'] = 'established'; cases['unsupported_cause'] = bad
    bad = copy.deepcopy(answer); bad['limitations'][0]['next_check'] = 'lower_rate_threshold'; cases['irrelevant_next_check'] = bad
    sources = [c for c in answer['claims'] if c['id'].endswith('_declared_source')]
    if sources:
        source = sources[0]
        bad = copy.deepcopy(answer)
        next(c for c in bad['claims'] if c['id'] == source['id']).update(
            status='observed', value='application_report' if source['value'] == 'trace' else 'trace')
        cases['duration_source_confusion'] = bad
        prefix = source['id'].removesuffix('_declared_source')
        bad = copy.deepcopy(answer)
        value = next(c for c in bad['claims'] if c['id'] == prefix + '_p95_ms')
        value['evidence'] = source['evidence']
        # Even equal values must be cited from the matching metric, not its source.
        if value['evidence'] == next(c for c in answer['claims'] if c['id'] == value['id'])['evidence']:
            value['evidence'] = ['/snapshot/issues']
        cases['duration_value_source_pair'] = bad
        bad = copy.deepcopy(answer)
        limitation = next(c for c in bad['limitations'] if c['id'].startswith(prefix + '_')
                          and not c['id'].endswith(('_authenticated_provenance_from_source_field',
                                                   '_downstream_rate_cause_from_p95')))
        limitation['status'] = 'established'
        cases['duration_semantics_overclaim'] = bad
    observations = {c['id']: c for c in answer['claims']}
    for ident, label, path in (
        ('probe_last_sample_age_ms', 'processing_age_as_dds_gap', 'dds_stamp_gap_p95_ms'),
        ('probe_rate_window_duration_ms', 'window_span_as_header_age', 'header_age_p95_ms'),
    ):
        if ident in observations:
            bad = copy.deepcopy(answer)
            citation = observations[path]['evidence']
            if citation == observations[ident]['evidence']:
                citation = ['/snapshot/issues']
            next(c for c in bad['claims'] if c['id'] == ident)['evidence'] = citation
            cases[label] = bad
    for ident in observations:
        if ident.startswith('diagnostic_issue_') and ident.endswith('_related_diagnostics'):
            bad = copy.deepcopy(answer)
            next(c for c in bad['claims'] if c['id'] == ident)['evidence'] = \
                observations[ident.removesuffix('_related_diagnostics') + '_related_nodes']['evidence']
            cases['diagnostic_reference_as_node_reference'] = bad
            break
    for ident, label in (
        ('current_delivery_from_retained_rate_window', 'retained_rate_proves_current_delivery'),
        ('publisher_activity_from_processed_sample_age', 'processing_age_proves_publisher_activity'),
        ('node_origin_from_task_label_or_sole_graph_publisher', 'task_label_proves_node_origin'),
    ):
        if any(c['id'] == ident for c in answer['limitations']):
            bad = copy.deepcopy(answer)
            next(c for c in bad['limitations'] if c['id'] == ident)['status'] = 'established'
            cases[label] = bad
    return cases


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def export_case(output, ident, snapshot, header, family, target, source, index, source_kind='provided_source'):
    participant, evaluator = output / 'participant' / ident, output / 'evaluator' / ident
    participant.mkdir(parents=True); evaluator.mkdir(parents=True)
    evidence = {'snapshot': snapshot, 'recording_metadata': {'mode': 'replay', 'recording_header': header}}
    rubric = rubric_for(ident, evidence, family, target)
    save(participant / 'evidence.json', evidence)
    write_recording(str(participant / 'input.rgd.json'), header, [snapshot])
    prompt = f'# Investigation {ident}\n\nFocus: `{target}`. Read the frozen evidence through CLI/MCP.\n\n'
    prompt += f'Return JSON with case_id `{ident}`, evidence_sha256 `{rubric["evidence_sha256"]}`, '
    prompt += 'claims and limitations arrays. Each claim needs id, status (observed/unknown), value and evidence (JSON Pointers into evidence.json). '
    prompt += 'For an unavailable fact use null and cite its parent object; never turn missing data into zero. '
    prompt += 'Limitations need id, status (established/not_established) and next_check. Reasoning text may be included but is not automatically scored.\n\n'
    prompt += 'Requested observations:\n' + '\n'.join('- ' + c['id'] for c in rubric['claims'])
    if family == 'duration':
        prompt += '\n\nDeclared source is metadata, not authenticated provenance. '
        prompt += 'Report each duration and its source from the same callback row; '
        prompt += 'a label or filename does not establish a missing measurement method. '
        prompt += 'An application report need not time a single callback; synthetic data is not measured runtime evidence.\n'
    elif family == 'topic_window':
        prompt += '\n\nA probe rate uses a bounded received-sample window that can remain during silence. '
        prompt += 'Sample-processing age uses a local monotonic clock; it is not DDS receipt age, header age, '
        prompt += 'or proof of publisher activity. Missing legacy fields remain unknown.\n'
    elif family == 'diagnostics':
        prompt += '\n\nDiagnostic names are opaque task labels. Report issue references as declared, '
        prompt += 'including absent legacy fields, without treating a task name, hardware ID or sole graph '
        prompt += 'publisher as per-status node identity. Issue observations include all diagnostic issues '
        prompt += 'in the frame, not an inferred association with the focus task.\n'
    prompt += '\n\nDoes the evidence establish each of these conclusions?\n' + '\n'.join('- ' + c['id'] for c in rubric['limitations'])
    prompt += '\n\nChoose next_check from: ' + ', '.join(sorted({c['next_check'] for c in rubric['limitations']})) + '.\n'
    prompt += '\nUse only participant inputs. Evaluator files contain reference answers and provenance; do not expose them to a model.\n'
    (participant / 'prompt.md').write_text(prompt)
    save(evaluator / 'rubric.json', rubric)
    save(evaluator / 'provenance.json', {'source': str(source.resolve()), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                                       'snapshot_index': index, 'family': family, 'target': target,
                                       'source_kind': source_kind,
                                       'original_recording_header_available': 'evaluation_wrapper' not in header})


def prepare(args):
    args.output.mkdir(parents=True, exist_ok=False)
    cases = []
    if args.integration:
        for phase in ('slow', 'healthy'):
            source = args.integration / phase / 'run.rgd.json'
            header, rows = read_recording(str(source))
            choices = []
            for index, snapshot in enumerate(rows):
                topic = next((t for t in snapshot['topics'] if t['name'] == OBJECTS), None)
                if snapshot['timestamp'] - rows[0]['timestamp'] < 5 or not topic or not number(topic.get('rate_hz')):
                    continue
                kinds = {i['kind'] for i in snapshot['issues']}
                faulty = topic['rate_hz'] < 10 and bool(kinds & {'rate_drop', 'bottleneck'})
                healthy = topic['rate_hz'] >= 10 and not any(i['kind'] in ('rate_drop', 'bottleneck', 'slow_callback') and '/detector' in i['related_nodes'] for i in snapshot['issues'])
                if (phase == 'slow' and faulty) or (phase == 'healthy' and healthy):
                    choices.append((index, snapshot))
            assert choices, f'{phase}: original rate contracts not met'
            index, snapshot = choices[0]
            cases.append((snapshot, header, 'topic', OBJECTS, source, index))
            index, snapshot = next((i, s) for i, s in enumerate(rows)
                                   if s['timestamp'] - rows[0]['timestamp'] >= 5
                                   and any(c.get('node') == PROCESSING_NODE
                                           and c.get('source') == 'application_report'
                                           and c.get('p95_ms') == 120
                                           for c in s.get('callbacks', []))
                                   and any(issue['kind'] == 'slow_callback'
                                           and PROCESSING_NODE in issue['related_nodes']
                                           and 'duration source: application-reported processing time' in issue['evidence']
                                           for issue in s['issues']))
            cases.append((snapshot, header, 'duration', PROCESSING_NODE, source, index))
        for phase in ('warmup', 'loaded', 'recovered'):
            source = args.integration / 'cpu' / (phase + '.json')
            snapshot = json.loads(source.read_text())
            worker = next(n for n in snapshot['nodes'] if n['id'] == '/integration/cpu_worker')
            hits = [i for i in snapshot['issues'] if i['kind'] == 'high_cpu' and worker['id'] in i['related_nodes']]
            assert worker['process_mapping_confidence'] == 'high' and worker['pid'] is not None
            if phase == 'warmup':
                assert worker['cpu_percent'] is None and not hits
            elif phase == 'loaded':
                assert worker['cpu_percent'] > 90 and hits
            else:
                assert number(worker['cpu_percent']) and worker['cpu_percent'] < 20 and not hits
            cases.append((snapshot, None, 'cpu', worker['id'], source, 0))
        source = args.integration / 'clock' / 'unstarted.json'
        snapshot = json.loads(source.read_text())
        topic = next(t for t in snapshot['topics'] if t['name'] == '/localization/kinematic_state')
        assert topic['header_age_p95_ms'] is None and topic['rate_hz'] >= 10
        cases.append((snapshot, None, 'topic', topic['name'], source, 0))
    if args.nav_recordings:
        for phase in ('healthy', 'paused', 'recovered'):
            source = args.nav_recordings / phase / 'run.rgd.json'
            header, rows = read_recording(str(source))
            cases.append((rows[len(rows)//2], header, 'nav2', '/controller_server', source, len(rows)//2))
    if args.trace_recordings:
        for phase in ('slow', 'healthy'):
            source = args.trace_recordings / phase / 'run.rgd.json'
            header, rows = read_recording(str(source))
            target = '/trace_' + phase
            choices = []
            for index, snapshot in enumerate(rows):
                callbacks = [c for c in snapshot.get('callbacks', []) if c.get('node') == target]
                issues = [i for i in snapshot['issues'] if i['kind'] == 'slow_callback']
                if snapshot['timestamp'] - rows[0]['timestamp'] < 1 or len(callbacks) != 3:
                    continue
                if not all(c.get('source') == 'trace' and number(c.get('count')) and c['count'] >= 8
                           and number(c.get('p95_ms')) for c in callbacks):
                    continue
                faulty = all(c['p95_ms'] >= 150 for c in callbacks) and len(issues) == 3 \
                    and all(target in i['related_nodes']
                            and 'duration source: paired callback trace' in i['evidence'] for i in issues)
                healthy = all(c['p95_ms'] < 100 for c in callbacks) and not issues
                if (phase == 'slow' and faulty) or (phase == 'healthy' and healthy):
                    choices.append((index, snapshot))
            assert choices, f'{phase}: original trace contracts not met'
            index, snapshot = choices[0]
            cases.append((snapshot, header, 'duration', target, source, index))
    if args.duration_recording:
        assert args.duration_target, '--duration-recording requires --duration-target'
        header, rows = read_recording(str(args.duration_recording))
        index, snapshot = next((i, s) for i, s in enumerate(rows)
                               if any(c.get('node') == args.duration_target for c in s.get('callbacks', [])))
        cases.append((snapshot, header, 'duration', args.duration_target, args.duration_recording, index))
    for option, target_option, family in (
        ('window_recording', 'window_topic', 'topic_window'),
        ('diagnostic_recording', 'diagnostic_name', 'diagnostics'),
    ):
        sources = getattr(args, option, None) or []
        target = getattr(args, target_option, None)
        assert not sources or target, f'--{option.replace("_", "-")} requires --{target_option.replace("_", "-")}'
        for source in sources:
            header, rows = read_recording(str(source))
            assert rows, f'{source}: no snapshots'
            index = len(rows) - 1
            snapshot = rows[index]
            if family == 'topic_window':
                assert any(t['name'] == target for t in snapshot['topics']), f'{source}: target topic absent'
            cases.append((snapshot, header, family, target, source, index))
    for source in (args.public_live, args.public_legacy):
        if source:
            rows = json.loads(source.read_text())
            index, row = next((i, r) for i, r in enumerate(rows) if r['offset'] is not None and 10 <= r['offset'] < 20)
            cases.append((row['snapshot'], None, 'topic', IMAGE, source, index))
    if args.public_bag:
        header, rows = read_recording(str(args.public_bag))
        index, snapshot = next((i, s) for i, s in enumerate(rows) if any(t['name'] == IMAGE and number(t.get('rate_hz')) for t in s['topics']))
        cases.append((snapshot, header, 'topic', IMAGE, args.public_bag, index))
    source_cases = len(cases)
    if args.synthetic_demo:
        from ros_graph_debugger.replay import build_demo_recording
        header, rows = build_demo_recording()
        controls = args.output / 'controls'; controls.mkdir()
        source = controls / 'synthetic-demo.rgd.json'
        write_recording(str(source), header, rows)
        cases.append((rows[18], header, 'duration', '/detector', source, 18))
    assert cases, 'provide at least one source family'
    manifest = []
    for index, (snapshot, header, family, target, source, source_index) in enumerate(cases):
        if header is None:
            header = make_header(snapshot['timestamp'], 1)
            header['evaluation_wrapper'] = 'Frozen original snapshot; original recording header unavailable'
        ident = f'case-{index + 1:03d}'
        kind = 'provided_source' if index < source_cases else 'synthetic_control'
        export_case(args.output, ident, snapshot, header, family, target, source, source_index, kind)
        entry = {'case_id': ident, 'target': target}
        if family == 'diagnostics':
            # A diagnostic task label is not a node/topic focus query.
            entry['briefing_tool'] = 'get_runtime_briefing'
        manifest.append(entry)
    save(args.output / 'manifest.json', manifest)
    print(f'Prepared {source_cases} source cases and {len(cases) - source_cases} synthetic control cases; no model invoked.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--output', type=Path, required=True)
    for name in ('integration', 'nav-recordings', 'trace-recordings', 'duration-recording',
                 'public-live', 'public-legacy', 'public-bag'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--duration-target')
    p.add_argument('--window-recording', type=Path, action='append', help='Freeze the last frame; repeat for multiple recordings')
    p.add_argument('--window-topic')
    p.add_argument('--diagnostic-recording', type=Path, action='append', help='Freeze declared task/issue observations; repeat for multiple recordings')
    p.add_argument('--diagnostic-name')
    p.add_argument('--synthetic-demo', action='store_true', help='Add an explicitly synthetic duration control')
    p = sub.add_parser('score')
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--answers', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--model-id', required=True, help='Record actual model/run identity; unreported is allowed explicitly')
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args); return
    if args.out.exists():
        parser.error('--out must be a fresh file')
    results = {}
    for case in json.loads((args.bundle / 'manifest.json').read_text()):
        ident = case['case_id']
        evidence = json.loads((args.bundle / 'participant' / ident / 'evidence.json').read_text())
        rubric = json.loads((args.bundle / 'evaluator' / ident / 'rubric.json').read_text())
        try:
            answer = json.loads((args.answers / (ident + '.json')).read_text())
            results[ident] = score(evidence, rubric, answer)
        except (OSError, ValueError, TypeError, AttributeError):
            results[ident] = {'passed': False, 'errors': ['missing or malformed answer']}
    save(args.out, {'model_id': args.model_id, 'scope': 'structured claims; free text and investigation time unscored', 'cases': results})
    raise SystemExit(0 if results and all(r['passed'] for r in results.values()) else 1)


if __name__ == '__main__':
    main()
