"""Reject mismatched evidence and preserve failed command diagnostics."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'benchmarks'))
from investigation_cases import save
from measure_collection import assert_payload, expected_inputs, run_command, summarize
from ros_graph_debugger.recording import make_header, write_recording


@pytest.fixture
def participant(tmp_path):
    header = make_header(100, 1)
    header['future_extension'] = dict(keep=True)
    snapshot = dict(timestamp=101, callbacks=[dict(source='trace', p95_ms=0.0)], issues=[])
    evidence = dict(snapshot=snapshot, recording_metadata=dict(mode='replay', recording_header=header),
                    separate_sidecar=dict(clock='unrelated', duration_ms=123))
    save(tmp_path / 'evidence.json', evidence)
    write_recording(str(tmp_path / 'input.rgd.json'), header, [snapshot])
    return tmp_path, evidence


def test_unknown_clock_and_sidecar_stay_separate_from_transport(participant):
    folder, original = participant
    evidence, expected = expected_inputs(folder)
    assert evidence == original
    assert expected == dict(snapshot=original['snapshot'], metadata=original['recording_metadata'])
    assert 'header_age_clock' not in expected['metadata']['recording_header']
    assert 'separate_sidecar' not in expected
    assert expected['snapshot']['callbacks'][0]['p95_ms'] == 0.0


@pytest.mark.parametrize('change', ['header', 'clock', 'frame', 'extra_frame'])
def test_mismatched_or_unfrozen_inputs_cannot_be_benchmarked(participant, change):
    folder, evidence = participant
    header = copy.deepcopy(evidence['recording_metadata']['recording_header'])
    snapshot = copy.deepcopy(evidence['snapshot'])
    if change == 'header': header['future_extension']['keep'] = False
    elif change == 'clock': header['header_age_clock'] = dict(selected='bag')
    elif change == 'frame': snapshot['callbacks'][0]['source'] = 'application_report'
    frames = [snapshot, snapshot] if change == 'extra_frame' else [snapshot]
    write_recording(str(folder / 'input.rgd.json'), header, frames)
    with pytest.raises(AssertionError):
        expected_inputs(folder)


def test_summary_preserves_actual_spread_without_speed_thresholds():
    rows = [dict(payload_exact=True, cli=dict(total_ns=a), mcp=dict(total_ns=b))
            for a, b in [(10, 100), (30, 200), (20, 300)]]
    summary = summarize(rows)
    assert summary['cli'] == dict(min_ns=10, median_ns=20, max_ns=30, operation_count=2, repetitions=3)
    assert summary['mcp']['median_ns'] == 200
    assert summary['mcp']['operation_count'] == 2


@pytest.mark.parametrize('changed', [1.0, True, '1'])
def test_equal_numbers_of_wrong_types_cannot_count_as_exact_transport(changed):
    with pytest.raises(AssertionError, match='snapshot/header mismatch'):
        assert_payload(dict(recording_header=dict(clock_messages=changed)),
                       dict(recording_header=dict(clock_messages=1)))


@pytest.mark.parametrize('rows', [[], [dict(payload_exact=False)],
    [dict(payload_exact=True, cli=dict(total_ns=-1), mcp=dict(total_ns=1))],
    [dict(payload_exact=True, cli=dict(total_ns=True), mcp=dict(total_ns=1))]])
def test_incomplete_or_invalid_measurements_are_rejected(rows):
    with pytest.raises(AssertionError):
        summarize(rows)


def test_failed_cli_command_keeps_output_and_returncode(tmp_path):
    out, err = tmp_path / 'out', tmp_path / 'err'
    with pytest.raises(AssertionError, match='CLI exit 7'):
        run_command([sys.executable, '-c', "import sys;print('partial');print('failure',file=sys.stderr);sys.exit(7)"], out, err)
    assert out.read_text() == 'partial\n' and err.read_text() == 'failure\n'


def test_timed_out_cli_is_reaped_and_partial_output_is_saved(tmp_path):
    out, err = tmp_path / 'out', tmp_path / 'err'
    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, '-c', "import time;print('started',flush=True);time.sleep(60)"], out, err, timeout=0.5)
    assert out.read_text() == 'started\n'
