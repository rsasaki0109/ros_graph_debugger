"""Freeze phase-audit sidecars and original recordings; never align their clocks.

Inputs are declared audit metadata, not authenticated trace provenance. No model
is invoked, and neither the audit nor callback rows are grafted into snapshots.
"""
import argparse
import hashlib
import json
from pathlib import Path
import uuid

from investigation_cases import digest, field_claim, number, save
from ros_graph_debugger.recording import read_recording, write_recording

PHASES = ('healthy', 'missing-imu', 'recovered')
LIMITS = {
    'current_health_from_historical_phases': 'collect_current_activity_and_output',
    'settled_snapshot_alignment_from_command_envelopes': 'mark_settled_sampling_boundaries',
    'ros_header_alignment_from_raw_ctf_clock': 'verify_trace_and_ros_clock_alignment',
    'per_message_cause_from_phase_counts': 'collect_paired_per_message_timings',
    'complete_capture_from_phase_audit': 'audit_original_trace_loss_and_registration',
    'authenticated_origin_from_file_hash': 'verify_original_capture_provenance',
    'zero_work_from_rounded_duration': 'inspect_unrounded_callback_intervals',
    'no_execution_from_absent_contained_rows': 'inspect_boundary_crossings_and_capture_coverage',
}


def validate_audit(audit):
    """Reject inconsistent declarations; this does not authenticate their origin."""
    assert isinstance(audit.get('run_id'), str) and audit['run_id'], 'missing run identity'
    assert type(audit.get('marker_pid')) is int and audit['marker_pid'] > 0, 'invalid marker process'
    assert type(audit.get('markers')) is int and audit['markers'] == 6, 'six markers required'
    phases = audit['phases']
    assert isinstance(phases, list) and [p['phase'] for p in phases] == list(PHASES), 'phase order'
    previous, clocks, contained = None, set(), 0
    for phase in phases:
        begin, end = phase['begin_ns'], phase['end_ns']
        assert type(begin) is int and type(end) is int and 0 <= begin < end, 'invalid boundaries'
        assert previous is None or previous < begin, 'overlapping phases'
        previous = end
        clock = phase['clock']
        assert isinstance(clock, str) and clock.startswith('ctf_raw_1ghz:'), 'raw 1 GHz clock required'
        token = clock.removeprefix('ctf_raw_1ghz:')
        assert str(uuid.UUID(token)) == token, 'canonical clock UUID required'
        clocks.add(clock)
        for key in ('invocations', 'boundary_crossing'):
            assert type(phase[key]) is int and phase[key] >= 0, 'invalid invocation count'
        rows = phase['callbacks']
        assert isinstance(rows, list), 'callback list required'
        identities = set()
        for row in rows:
            assert all(isinstance(row.get(k), str) for k in ('node', 'callback', 'topic')), 'callback identity'
            identity = (row['node'], row['callback'], row['topic'])
            assert identity not in identities, 'duplicate callback identity'
            identities.add(identity)
            assert row.get('source') == 'trace', 'paired trace source required'
            assert type(row.get('count')) is int and row['count'] > 0, 'invalid callback count'
            assert all(number(row.get(k)) and row[k] >= 0 for k in ('mean_ms', 'p95_ms', 'max_ms')), 'invalid duration'
            assert row['mean_ms'] <= row['max_ms'] and row['p95_ms'] <= row['max_ms'], 'inconsistent duration'
        assert sum(r['count'] for r in rows) == phase['invocations'], 'aggregate count mismatch'
        contained += phase['invocations']
    assert len(clocks) == 1, 'mixed phase clocks'
    assert type(audit.get('total_paired')) is int and audit['total_paired'] >= contained, 'invalid total count'


def phase_rubric(case_id, evidence):
    audit, target = evidence['phase_audit'], evidence['target_callback']
    validate_audit(audit)
    assert any(all(row[k] == target[k] for k in ('node', 'callback', 'topic'))
               for phase in audit['phases'] for row in phase['callbacks']), 'target absent from all phases'
    claims = []
    for index, phase in enumerate(audit['phases']):
        root, prefix = f'/phase_audit/phases/{index}', f'phase_{index}'
        for key in ('phase', 'begin_ns', 'end_ns', 'clock', 'boundary_crossing'):
            claim = field_claim(evidence, prefix + '_' + key, root + '/' + key)
            if key in ('begin_ns', 'end_ns', 'boundary_crossing'):
                claim['value_type'] = 'integer'
            claims.append(claim)
        indexes = [i for i, row in enumerate(phase['callbacks'])
                   if all(row[k] == target[k] for k in ('node', 'callback', 'topic'))]
        row_root = root + f'/callbacks/{indexes[0]}' if indexes else root + '/callbacks'
        claims.append(dict(id=prefix + '_contained_count', status='observed',
                           value=phase['callbacks'][indexes[0]]['count'] if indexes else 0,
                           evidence=[row_root + '/count' if indexes else row_root], value_type='integer'))
        for key in ('mean_ms', 'p95_ms', 'max_ms', 'source'):
            if indexes:
                claims.append(field_claim(evidence, prefix + '_' + key, row_root + '/' + key))
            else:
                claims.append(dict(id=prefix + '_' + key, status='unknown', value=None, evidence=[row_root]))
    return dict(case_id=case_id, evidence_sha256=digest(evidence), claims=claims,
                limitations=[dict(id=key, status='not_established', next_check=value) for key, value in LIMITS.items()])


def prepare(audit_path, recordings, targets, output):
    audit = json.loads(audit_path.read_text())
    validate_audit(audit)
    contexts, provenance = [], []
    for phase in PHASES:
        source = recordings / phase / 'run.rgd.json'
        header, frames = read_recording(str(source))
        assert frames, f'{phase}: empty recording'
        contexts.append(dict(phase=phase, snapshot=frames[-1],
                             recording_metadata=dict(mode='replay', recording_header=header)))
        provenance.append(dict(phase=phase, source=str(source.resolve()),
                               sha256=hashlib.sha256(source.read_bytes()).hexdigest(), snapshot_index=len(frames)-1))
    assert targets and len(targets) == len(set(targets)), 'unique callback targets required'
    output.mkdir(parents=True, exist_ok=False)
    manifest = []
    for index, (node, topic) in enumerate(targets):
        assert node.startswith('/') and topic.startswith('/'), 'absolute node and subscription topic required'
        ident = f'case-{index+1:03d}'
        participant, evaluator = output / 'participant' / ident, output / 'evaluator' / ident
        participant.mkdir(parents=True); evaluator.mkdir(parents=True)
        target = dict(node=node, topic=topic, callback='sub ' + topic)
        evidence = dict(snapshot=contexts[-1]['snapshot'], recording_metadata=contexts[-1]['recording_metadata'],
                        phase_recordings=contexts, phase_audit=audit, target_callback=target)
        rubric = phase_rubric(ident, evidence)
        save(participant / 'evidence.json', evidence)
        for context in contexts:
            write_recording(str(participant / (context['phase'] + '.rgd.json')),
                            context['recording_metadata']['recording_header'], [context['snapshot']])
        write_recording(str(participant / 'input.rgd.json'), contexts[-1]['recording_metadata']['recording_header'],
                        [contexts[-1]['snapshot']])
        save(evaluator / 'rubric.json', rubric)
        save(evaluator / 'provenance.json', dict(family='phase_audit', recordings=provenance,
             audit_source=str(audit_path.resolve()), audit_sha256=hashlib.sha256(audit_path.read_bytes()).hexdigest(),
             scope='File identity only; no authenticated origin, clock alignment or settled-window selection'))
        prompt = f'# Investigation {ident}\n\nTarget subscription: `{node}` on `{topic}`.\n\n'
        prompt += 'Read evidence.json and the frozen recordings through CLI/MCP. The phase audit is a separate sidecar, '
        prompt += 'not a live snapshot or ROS/header-clock measurement. Phase names label assertion command envelopes. '
        prompt += 'Count only declared wholly contained invocations; an absent row has no measured duration. '
        prompt += 'Never infer zero work from a rounded 0.0 ms value.\n\n'
        prompt += f'Return JSON with case_id `{ident}`, evidence_sha256 `{rubric["evidence_sha256"]}`, claims and limitations. '
        prompt += 'Each claim needs id, status (observed/unknown), value and evidence (exact JSON Pointers into evidence.json). '
        prompt += 'Preserve integer boundaries exactly. Unknown durations are null with parent citations. '
        prompt += 'Each limitation needs id, status and next_check. Free text is unscored.\n\nRequested observations:\n'
        prompt += '\n'.join('- ' + c['id'] for c in rubric['claims'])
        prompt += '\n\nDoes the evidence establish these conclusions?\n' + '\n'.join('- ' + c['id'] for c in rubric['limitations'])
        prompt += '\n\nChoose next_check from: ' + ', '.join(sorted(LIMITS.values())) + '.\n'
        prompt += '\nUse participant inputs only. Evaluator files are scripted references, never model inputs.\n'
        (participant / 'prompt.md').write_text(prompt)
        manifest.append(dict(case_id=ident, target=node))
    save(output / 'manifest.json', manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--recordings', type=Path, required=True)
    parser.add_argument('--target', action='append', required=True, metavar='NODE=TOPIC')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    targets = []
    for target in args.target:
        node, sep, topic = target.partition('=')
        assert sep, '--target requires NODE=TOPIC'
        targets.append((node, topic))
    prepare(args.audit, args.recordings, targets, args.output)
    print(f'Prepared {len(targets)} declared phase-audit cases; no model invoked.')


if __name__ == '__main__':
    main()
