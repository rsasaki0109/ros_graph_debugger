"""Scripted evidence collection on frozen real ROS snapshots.

This compares CLI processes and a persistent MCP session, not human/AI
diagnosis time or a blind model evaluation. Every answer is validated against
the same original snapshot and the controlled scenario's phase.
"""
import asyncio
import argparse
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

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ros_graph_debugger.recording import make_header, read_recording, write_recording

ROOT = Path.cwd() / 'evidence-evaluation'
REPO = Path(__file__).resolve().parents[2]
NAV_RECORDINGS = None
CPU_SNAPSHOTS = None
BASE = 'http://127.0.0.1:3971'
REPEATS = 5
MCP_MODE = 'snapshot'


def stop(child):
    for sig, budget in [(signal.SIGINT, 3), (signal.SIGTERM, 2), (signal.SIGKILL, 2)]:
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            continue
    assert child.poll() is not None


def facts(snapshot, family, phase):
    nodes = {n['id']: n for n in snapshot['nodes']}
    issues = snapshot['issues']
    if family == 'cpu':
        target = nodes['/integration/cpu_worker']
        cpu = target['cpu_percent']
        hit = [i for i in issues if i['kind'] == 'high_cpu' and target['id'] in i['related_nodes']]
        assert target['process_mapping_confidence'] == 'high' and target['pid'] is not None
        if phase == 'warmup':
            assert cpu is None and not hit
        elif phase == 'loaded':
            assert cpu > 90 and hit and any(e.startswith('CPU: ') for e in hit[0]['evidence'])
        else:
            assert cpu is not None and cpu < 20 and not hit
        return {'pid': target['pid'], 'cpu_percent': cpu, 'mapping_confidence': target['process_mapping_confidence'],
                'high_cpu_evidence': [i['evidence'] for i in hit], 'cpu_interpretation': 'unknown' if cpu is None else 'process total'}
    topics = {t['name']: t for t in snapshot['topics']}
    tf = {(e['parent'], e['child']): e for e in snapshot['tf_edges']}
    active = {n: nodes[n]['lifecycle_state'] for n in ['/map_server', '/planner_server', '/controller_server']}
    assert set(active.values()) == {'active'}
    result = {'lifecycle': active, 'topics': {}, 'tf': {},
              'cpu_interpretation': 'CPU is not scored for the Nav2 delivery/TF task; CPU alone does not establish health'}
    paused = phase == 'paused'
    for name in ['/scan', '/odom']:
        topic = topics[name]
        hit = [i for i in issues if i['kind'] == 'topic_stale' and name in i['related_topics']]
        assert '/loopback_simulator' in topic['publishers'] and topic['probed']
        assert bool(hit) == paused and (topic['age_ms'] > 2000) == paused
        result['topics'][name] = {'publishers': topic['publishers'], 'age_ms': topic['age_ms'],
                                  'stale_evidence': [i['evidence'] for i in hit]}
    for edge in [('map', 'odom'), ('odom', 'base_footprint')]:
        value = tf[edge]
        assert (value['age_ms'] > 1000) == paused and (value['status'] == 'critical') == paused
        hit = [i for i in issues if i['kind'] == 'tf_stale' and all(f in i['related_frames'] for f in edge)]
        assert bool(hit) == paused
        result['tf'][' → '.join(edge)] = {'age_ms': value['age_ms'], 'status': value['status'],
                                         'stale_evidence': [i['evidence'] for i in hit]}
    return result


def cli_snapshot(directory, iteration):
    started = time.perf_counter()
    command = [sys.executable, '-m', 'ros_graph_debugger.cli', '--base', BASE, 'snapshot']
    result = subprocess.run(command, cwd=REPO, capture_output=True, timeout=10)
    elapsed = time.perf_counter() - started
    (directory / f'cli-{iteration}.json').write_bytes(result.stdout)
    (directory / f'cli-{iteration}.stderr.log').write_bytes(result.stderr)
    assert result.returncode == 0, result.stderr.decode()
    return json.loads(result.stdout), elapsed, len(result.stdout)


async def mcp_snapshot(session, directory, iteration, family):
    started = time.perf_counter()
    payloads = {}
    names = (['get_runtime_snapshot'] if MCP_MODE == 'snapshot' else
             ['get_graph', 'get_tf', 'get_issues'] if family == 'nav2' else
             ['get_nodes', 'get_issues'])
    for name in names:
        response = await session.call_tool(name, {})
        assert not response.isError, response
        payloads[name] = '\n'.join(c.text for c in response.content if c.type == 'text')
    elapsed = time.perf_counter() - started
    (directory / f'mcp-{iteration}.json').write_text(json.dumps(payloads, indent=2))
    if MCP_MODE == 'snapshot':
        snapshot = json.loads(payloads['get_runtime_snapshot'])
    else:
        snapshot = json.loads(payloads['get_graph']) if family == 'nav2' else {'nodes': json.loads(payloads['get_nodes'])}
        snapshot['tf_edges'] = json.loads(payloads['get_tf']) if family == 'nav2' else []
        snapshot['issues'] = json.loads(payloads['get_issues'])
    return snapshot, elapsed, sum(len(v.encode()) for v in payloads.values()), len(names)


async def case(family, phase, source):
    directory = ROOT / (family + '-' + phase)
    directory.mkdir(exist_ok=False)
    if family == 'nav2':
        header, rows = read_recording(str(source))
        index = len(rows) // 2
        selected = rows[index]
    else:
        header, index = make_header('CPU real ROS scenario', 1), 0
        selected = json.loads(source.read_text())
    expected = facts(selected, family, phase)
    header['evaluation_source'] = str(source)
    header['evaluation_snapshot_index'] = index
    frozen = directory / 'frozen.rgd.json'
    write_recording(str(frozen), header, [selected])
    (directory / 'expected-facts.json').write_text(json.dumps(expected, indent=2))
    with (directory / 'replay.log').open('w') as log:
        server = subprocess.Popen([sys.executable, '-m', 'ros_graph_debugger.cli', 'serve',
                                   str(frozen), '--port', BASE.rsplit(':', 1)[1], '--no-browser', '--once'],
                                  cwd=REPO, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while True:
                assert server.poll() is None
                try:
                    with urllib.request.urlopen(BASE + '/api/v1/snapshot', timeout=1) as response:
                        assert facts(json.load(response), family, phase) == expected
                    break
                except OSError:
                    assert time.monotonic() < deadline, 'replay readiness timeout'
                    await asyncio.sleep(0.1)
            cli_cold, cli_cold_time, _ = cli_snapshot(directory, 'cold')
            assert facts(cli_cold, family, phase) == expected
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'ros_graph_debugger.mcp_server'], cwd=str(REPO),
                env=dict(os.environ, RGD_BASE=BASE))
            results = []
            with (directory / 'mcp.stderr.log').open('w') as err:
                startup = time.perf_counter()
                async with stdio_client(params, errlog=err) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        init_time = time.perf_counter() - startup
                        discovery = time.perf_counter()
                        available = {t.name for t in (await session.list_tools()).tools}
                        discovery_time = time.perf_counter() - discovery
                        required = {'get_runtime_snapshot'} if MCP_MODE == 'snapshot' else {'get_graph', 'get_nodes', 'get_tf', 'get_issues'}
                        assert required <= available
                        mcp_cold, cold_time, _, _ = await mcp_snapshot(session, directory, 'cold', family)
                        assert facts(mcp_cold, family, phase) == expected
                        for i in range(REPEATS):
                            # Alternate order to avoid always giving one client
                            # the first warm HTTP request of each iteration.
                            if i % 2:
                                mcp_data, mcp_time, mcp_size, calls = await mcp_snapshot(session, directory, i, family)
                                cli_data, cli_time, cli_size = cli_snapshot(directory, i)
                            else:
                                cli_data, cli_time, cli_size = cli_snapshot(directory, i)
                                mcp_data, mcp_time, mcp_size, calls = await mcp_snapshot(session, directory, i, family)
                            assert facts(cli_data, family, phase) == facts(mcp_data, family, phase) == expected
                            results.append({'cli_seconds': cli_time, 'mcp_seconds': mcp_time,
                                            'cli_output_bytes': cli_size, 'mcp_text_bytes': mcp_size,
                                            'cli_commands': 1, 'mcp_tool_calls': calls, 'facts_equal': True})
            result = {'family': family, 'phase': phase, 'source': str(source), 'mcp_mode': MCP_MODE,
                      'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                      'snapshot_index': index, 'cli_cold_seconds': cli_cold_time,
                      'mcp_initialization_seconds': init_time, 'mcp_first_collection_seconds': cold_time,
                      'mcp_tool_discovery_seconds': discovery_time,
                      'mcp_cold_total_seconds': init_time + discovery_time + cold_time,
                      'warm_repetitions': results, 'fact_accuracy': 'all selected facts matched',
                      'cli_median_seconds': statistics.median(r['cli_seconds'] for r in results),
                      'mcp_median_seconds': statistics.median(r['mcp_seconds'] for r in results)}
            (directory / 'result.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(result), flush=True)
            return result
        finally:
            stop(server)


async def main():
    global ROOT, MCP_MODE, NAV_RECORDINGS, CPU_SNAPSHOTS, BASE
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT)
    parser.add_argument('--mcp-mode', choices=['bundle', 'snapshot'], default='snapshot')
    parser.add_argument('--nav-recordings', type=Path, required=True)
    parser.add_argument('--cpu-snapshots', type=Path, required=True)
    parser.add_argument('--port', type=int, default=3971)
    args = parser.parse_args()
    ROOT, MCP_MODE = args.output.resolve(), args.mcp_mode
    NAV_RECORDINGS, CPU_SNAPSHOTS = args.nav_recordings.resolve(), args.cpu_snapshots.resolve()
    BASE = f'http://127.0.0.1:{args.port}'
    ROOT.mkdir(exist_ok=False)
    results = []
    for phase in ['healthy', 'paused', 'recovered']:
        results.append(await case('nav2', phase,
            NAV_RECORDINGS / phase / 'run.rgd.json'))
    for phase in ['warmup', 'loaded', 'recovered']:
        results.append(await case('cpu', phase,
            CPU_SNAPSHOTS / (phase + '.json')))
    (ROOT / 'results.json').write_text(json.dumps(results, indent=2))
    print('Six real ROS cases: all CLI/MCP selected facts agree; cleanup completed.', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
