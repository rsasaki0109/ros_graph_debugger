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
    if family == 'cpu':
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
    return cases


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def export_case(output, ident, snapshot, header, family, target, source, index):
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
    prompt += '\n\nDoes the evidence establish each of these conclusions?\n' + '\n'.join('- ' + c['id'] for c in rubric['limitations'])
    prompt += '\n\nChoose next_check from: ' + ', '.join(sorted({c['next_check'] for c in rubric['limitations']})) + '.\n'
    prompt += '\nUse only participant inputs. Evaluator files contain reference answers and provenance; do not expose them to a model.\n'
    (participant / 'prompt.md').write_text(prompt)
    save(evaluator / 'rubric.json', rubric)
    save(evaluator / 'provenance.json', {'source': str(source.resolve()), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                                       'snapshot_index': index, 'family': family, 'target': target,
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
    for source in (args.public_live, args.public_legacy):
        if source:
            rows = json.loads(source.read_text())
            index, row = next((i, r) for i, r in enumerate(rows) if r['offset'] is not None and 10 <= r['offset'] < 20)
            cases.append((row['snapshot'], None, 'topic', IMAGE, source, index))
    if args.public_bag:
        header, rows = read_recording(str(args.public_bag))
        index, snapshot = next((i, s) for i, s in enumerate(rows) if any(t['name'] == IMAGE and number(t.get('rate_hz')) for t in s['topics']))
        cases.append((snapshot, header, 'topic', IMAGE, args.public_bag, index))
    assert cases, 'provide at least one source family'
    manifest = []
    for index, (snapshot, header, family, target, source, source_index) in enumerate(cases):
        if header is None:
            header = make_header(snapshot['timestamp'], 1)
            header['evaluation_wrapper'] = 'Frozen original snapshot; original recording header unavailable'
        ident = f'case-{index + 1:03d}'
        export_case(args.output, ident, snapshot, header, family, target, source, source_index)
        manifest.append({'case_id': ident, 'target': target})
    save(args.output / 'manifest.json', manifest)
    print(f'Prepared {len(cases)} real-source cases; no model invoked.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--output', type=Path, required=True)
    for name in ('integration', 'nav-recordings', 'public-live', 'public-legacy', 'public-bag'):
        p.add_argument('--' + name, type=Path)
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
