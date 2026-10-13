"""Compare exact frozen snapshot/header retrieval, not diagnosis or model time."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time
import urllib.request

from investigation_cases import digest, save
from ros_graph_debugger.recording import read_recording

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'test' / 'integration'))
from run_scenario import Processes


def expected_inputs(participant):
    evidence = json.loads((participant / 'evidence.json').read_text())
    header, frames = read_recording(str(participant / 'input.rgd.json'))
    expected = dict(snapshot=evidence['snapshot'], metadata=evidence['recording_metadata'])
    assert digest(frames) == digest([expected['snapshot']]), 'input must be the exact single frozen frame'
    assert digest(expected['metadata']) == digest(dict(mode='replay', recording_header=header)), 'original header mismatch'
    return evidence, expected


def assert_payload(data, expected):
    # Python numeric equality alone would accept a float or bool in place of
    # an original integer. Bind the complete JSON values and types instead.
    assert digest(data) == digest(expected), 'original snapshot/header mismatch'


def run_command(command, out, err, timeout=10):
    """Own the command group even when it exits or times out."""
    started = time.perf_counter_ns()
    child = subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
        elapsed = time.perf_counter_ns() - started
    except subprocess.TimeoutExpired:
        Processes.signal(child, signal.SIGKILL)
        stdout, stderr = child.communicate(timeout=5)
        raise
    finally:
        Processes.signal(child, signal.SIGKILL)
        child.wait(timeout=5)
        # Preserve even failed command responses before propagating failure.
        out.write_bytes(locals().get('stdout', b''))
        err.write_bytes(locals().get('stderr', b''))
    assert child.returncode == 0, f'CLI exit {child.returncode}; see {err}'
    return stdout, elapsed


def cli_collect(base, directory, label):
    payloads, elapsed, sizes = {}, [], 0
    for command, key in (('snapshot', 'snapshot'), ('metadata', 'metadata')):
        data, duration = run_command([sys.executable, '-m', 'ros_graph_debugger.cli', '--base', base, command],
                                    directory / f'{label}-cli-{command}.json',
                                    directory / f'{label}-cli-{command}.stderr.log')
        payloads[key] = json.loads(data)
        elapsed.append(duration); sizes += len(data)
    return payloads, dict(operation_count=2, operation_ns=elapsed, total_ns=sum(elapsed),
                         application_text_bytes=sizes)


async def mcp_collect(session, directory, label):
    payloads, elapsed, sizes = {}, [], 0
    for name, key in (('get_runtime_snapshot', 'snapshot'), ('get_recording_metadata', 'metadata')):
        started = time.perf_counter_ns()
        response = await asyncio.wait_for(session.call_tool(name, {}), timeout=10)
        duration = time.perf_counter_ns() - started
        assert not response.isError, f'{name}: MCP tool error'
        text = ''.join(c.text for c in response.content if c.type == 'text')
        (directory / f'{label}-mcp-{key}.json').write_text(text)
        payloads[key] = json.loads(text)
        elapsed.append(duration); sizes += len(text.encode())
    return payloads, dict(operation_count=2, operation_ns=elapsed, total_ns=sum(elapsed),
                         application_text_bytes=sizes)


def summarize(rows):
    assert rows and all(r['payload_exact'] for r in rows), 'incorrect or empty measurements'
    result = {}
    for client in ('cli', 'mcp'):
        values = [r[client]['total_ns'] for r in rows]
        assert all(type(v) is int and v >= 0 for v in values), 'invalid elapsed measurements'
        result[client] = dict(min_ns=min(values), median_ns=statistics.median(values), max_ns=max(values),
                              operation_count=2, repetitions=len(values))
    return result


async def measure_case(bundle, case, output, port, repeats, case_index):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    ident = case['case_id']
    assert ident and all(c.isalnum() or c == '-' for c in ident), 'invalid case identity'
    directory = output / ident; directory.mkdir()
    participant = bundle / 'participant' / ident
    evidence, expected = expected_inputs(participant)
    source = participant / 'input.rgd.json'
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    processes = Processes(directory)
    base = f'http://127.0.0.1:{port}'
    rows = []
    try:
        processes.start('replay', [sys.executable, '-m', 'ros_graph_debugger.cli', 'serve', str(source),
                                  '--port', str(port), '--no-browser', '--once'])
        deadline = time.monotonic() + 15
        while True:
            processes.check()
            try:
                with urllib.request.urlopen(base + '/api/v1/snapshot', timeout=1) as response:
                    assert_payload(json.load(response), expected['snapshot'])
                break
            except OSError:
                assert time.monotonic() < deadline, 'replay readiness timeout'
                await asyncio.sleep(0.05)
        params = StdioServerParameters(command=sys.executable, args=['-m', 'ros_graph_debugger.mcp_server'],
                                       cwd=str(REPO), env=dict(os.environ, RGD_BASE=base))
        with (directory / 'mcp.stderr.log').open('w') as log:
            startup = time.perf_counter_ns()
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=10)
                    init_ns = time.perf_counter_ns() - startup
                    discovery = time.perf_counter_ns()
                    available = await asyncio.wait_for(session.list_tools(), timeout=10)
                    discovery_ns = time.perf_counter_ns() - discovery
                    assert {'get_runtime_snapshot', 'get_recording_metadata'} <= {t.name for t in available.tools}
                    # First collection and steady connected-session repeats are
                    # separate. Alternate CLI/MCP order across cases and repeats.
                    for iteration in range(repeats + 1):
                        label = 'first' if iteration == 0 else f'repeat-{iteration}'
                        order = ('cli', 'mcp') if (case_index + iteration) % 2 == 0 else ('mcp', 'cli')
                        timing = {}
                        for client in order:
                            data, timing[client] = (cli_collect(base, directory, label) if client == 'cli'
                                                   else await mcp_collect(session, directory, label))
                            assert_payload(data, expected)
                        row = dict(iteration=label, order=list(order), payload_exact=True, **timing)
                        save(directory / (label + '-timing.json'), row)
                        if iteration == 0:
                            first = row
                        else:
                            rows.append(row)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash, 'input changed during measurement'
        result = dict(case_id=ident, evidence_sha256=digest(evidence), frozen_input_sha256=source_hash,
                      snapshot_sha256=digest(expected['snapshot']), metadata_sha256=digest(expected['metadata']),
                      mcp_initialization_ns=init_ns, mcp_discovery_ns=discovery_ns,
                      mcp_cold_total_ns=init_ns + discovery_ns + first['mcp']['total_ns'],
                      first_collection=first, connected_session_repetitions=rows, summary=summarize(rows),
                      input_unchanged=True, model_invoked=False)
        save(directory / 'result.json', result)
        return result
    finally:
        try:
            processes.close()
        finally:
            cleanup = {}
            for name, child in processes.children:
                try:
                    os.killpg(child.pid, 0)
                except ProcessLookupError:
                    cleanup[name] = child.poll() is not None
                else:
                    cleanup[name] = False
            save(directory / 'cleanup.json', cleanup)
            assert cleanup and all(cleanup.values()), cleanup


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--port', type=int, default=3987)
    args = parser.parse_args()
    assert args.repeats > 0, 'positive repeat count required'
    manifest = json.loads((args.bundle / 'manifest.json').read_text())
    assert manifest and len({c['case_id'] for c in manifest}) == len(manifest), 'nonempty unique tasks required'
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    try:
        for index, case in enumerate(manifest):
            result = await measure_case(args.bundle, case, args.output, args.port, args.repeats, index)
            results.append(result)
            print(json.dumps(dict(case_id=result['case_id'], summary=result['summary'], payload_exact=True,
                                  model_invoked=False)), flush=True)
    finally:
        save(args.output / 'results.json', dict(completed_cases=results, model_invoked=False,
             runtime=dict(python=sys.version, platform=sys.platform),
             scope='Sequential loopback snapshot/header retrieval; sums of timed operation intervals, not full collection wall time. '
                   'Two CLI processes versus two calls in a persistent MCP session. '
                   'Initialization/discovery separate; replay startup, output writes, JSON decoding, sidecar retrieval, '
                   'reasoning, scoring and human/model investigation time excluded. Application text bytes, not wire traffic. '
                   'No synchronized live sampling, speed threshold, AI accuracy or general performance claim.'))


if __name__ == '__main__':
    asyncio.run(main())
