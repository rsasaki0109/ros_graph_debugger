"""Validate task transport and rubric negative controls, without invoking a model."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

from investigation_cases import digest, negative_controls, save, score
from ros_graph_debugger.markdown import snapshot_to_markdown
from ros_graph_debugger.recording import header_age_clock_note

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'test' / 'integration'))
from run_scenario import Processes


async def validate_case(bundle, case, output, port):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    ident = case['case_id']
    directory = output / ident
    directory.mkdir()
    participant, evaluator = bundle / 'participant' / ident, bundle / 'evaluator' / ident
    evidence = json.loads((participant / 'evidence.json').read_text())
    rubric = json.loads((evaluator / 'rubric.json').read_text())
    assert digest(evidence) == rubric['evidence_sha256']
    base = f'http://127.0.0.1:{port}'
    processes = Processes(directory)
    try:
        processes.start('replay', [sys.executable, '-m', 'ros_graph_debugger.cli', 'serve',
                                  str(participant / 'input.rgd.json'), '--port', str(port), '--no-browser', '--once'])
        deadline = time.monotonic() + 15
        while True:
            processes.check()
            try:
                with urllib.request.urlopen(base + '/api/v1/snapshot', timeout=1) as response:
                    assert json.load(response) == evidence['snapshot']
                break
            except OSError:
                assert time.monotonic() < deadline, 'replay readiness timeout'
                await asyncio.sleep(0.05)
        cli = subprocess.run([sys.executable, '-m', 'ros_graph_debugger.cli', '--base', base, 'snapshot'],
                             cwd=REPO, capture_output=True, timeout=10)
        (directory / 'cli.json').write_bytes(cli.stdout)
        (directory / 'cli.stderr.log').write_bytes(cli.stderr)
        assert cli.returncode == 0 and json.loads(cli.stdout) == evidence['snapshot']
        params = StdioServerParameters(command=sys.executable, args=['-m', 'ros_graph_debugger.mcp_server'],
                                       cwd=str(REPO), env=dict(os.environ, RGD_BASE=base))
        with (directory / 'mcp.stderr.log').open('w') as log:
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for name, expected in (('get_runtime_snapshot', evidence['snapshot']),
                                           ('get_callbacks', evidence['snapshot'].get('callbacks', [])),
                                           ('get_recording_metadata', evidence['recording_metadata'])):
                        response = await session.call_tool(name, {})
                        assert not response.isError
                        text = ''.join(c.text for c in response.content if c.type == 'text')
                        save(directory / (name + '.json'), json.loads(text))
                        assert json.loads(text) == expected
                    response = await session.call_tool('get_node_briefing', {'target': case['target']})
                    assert not response.isError
                    text = ''.join(c.text for c in response.content if c.type == 'text')
                    (directory / 'briefing.md').write_text(text)
                    expected = snapshot_to_markdown(evidence['snapshot'], focus=case['target'])
                    expected += '\n\n## Header age clock\n\n' + header_age_clock_note(evidence['recording_metadata']['recording_header']) + '\n'
                    assert text == expected
        # The reference response is an explicit scripted oracle, not a model.
        reference = score(evidence, rubric, rubric)
        assert reference['passed'], reference
        save(directory / 'scripted-reference-score.json', reference)
        failures = {}
        for label, answer in negative_controls(rubric).items():
            result = score(evidence, rubric, answer)
            assert not result['passed'], label
            failures[label] = result
        save(directory / 'negative-controls.json', failures)
        return {'case_id': ident, 'evidence_sha256': rubric['evidence_sha256'],
                'cli_snapshot_exact': True, 'mcp_snapshot_and_metadata_exact': True,
                'mcp_callbacks_exact': True,
                'focused_markdown_exact': True, 'scripted_reference_passed': True,
                'deliberate_invalid_answers_rejected': len(failures), 'model_invoked': False}
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
    parser.add_argument('--port', type=int, default=3972)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.bundle / 'manifest.json').read_text())
    assert manifest, 'no cases'
    results = []
    try:
        for case in manifest:
            result = await validate_case(args.bundle, case, args.output, args.port)
            results.append(result)
            print(json.dumps(result), flush=True)
    finally:
        save(args.output / 'results.json', {'scope': 'transport and structured rubric calibration; no AI accuracy measurement',
                                           'model_invoked': False, 'completed_cases': results})


if __name__ == '__main__':
    asyncio.run(main())
