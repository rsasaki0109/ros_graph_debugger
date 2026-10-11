"""Pipeline path over a recording: per-hop rate and callback p95 over time."""

from ros_graph_debugger.cli import main
from ros_graph_debugger.pipeline import default_focus, pipeline_timeline, spark
from ros_graph_debugger.recording import append_snapshot, write_header
from ros_graph_debugger.replay import build_demo_recording
from ros_graph_debugger.report import build_report, render_html, render_markdown

IMAGE = '/sensing/camera/image_raw'
OBJECTS = '/perception/object_recognition/objects'


def _demo():
    return build_demo_recording()


def test_default_focus_is_the_most_blamed_node():
    _, snaps = _demo()
    assert default_focus(snaps) == '/detector'
    assert default_focus([{'issues': []}]) is None


def test_timeline_follows_the_fixed_path_and_lines_up_with_blame():
    _, snaps = _demo()
    tl = pipeline_timeline(snaps)
    assert tl['target'] == '/detector'
    assert tl['nodes'] == ['/camera', '/detector', '/tracker', '/planner', '/controller']
    assert len(tl['t']) == len(tl['blamed']) == len(snaps)
    hops = {h['topic']: h for h in tl['hops']}
    assert all(len(h['rate_hz']) == len(snaps) for h in tl['hops'])

    blamed = [i for i, b in enumerate(tl['blamed']) if b]
    calm = [i for i, b in enumerate(tl['blamed']) if not b]
    assert blamed and calm
    cb = hops[IMAGE]['cb_p95_ms']       # detector's input callback
    rate = hops[OBJECTS]['rate_hz']     # detector's output
    assert min(cb[i] for i in blamed) > max(cb[i] for i in calm)
    assert max(rate[i] for i in blamed) < min(rate[i] for i in calm)


def test_explicit_focus_and_unknown_focus():
    _, snaps = _demo()
    tl = pipeline_timeline(snaps, '/tracker')
    assert tl['pivot'] == '/tracker' and not any(tl['blamed'])
    assert pipeline_timeline(snaps, '/nope') is None
    assert pipeline_timeline([], '/tracker') is None


def test_spark_buckets_gaps_and_flat_series():
    assert spark([]) == ''
    assert spark([1, 2, 3, 4], width=40) == '▁▃▆█'
    assert spark([5, 5, 5]) == '▅▅▅'
    assert spark([None, None, 1, 3], width=4) == '  ▁█'
    assert len(spark(list(range(1000)), width=40)) == 40


def test_report_renders_the_section():
    header, snaps = _demo()
    summary = build_report(header, snaps)
    md = render_markdown(summary)
    assert '## Pipeline path over time (focus: /detector)' in md
    assert f'| {IMAGE} → /detector | 30 |' in md
    html = render_html(summary)
    assert 'Pipeline path over time' in html and '<polyline' in html


def test_no_blame_no_section():
    header, snaps = _demo()
    calm = [dict(s, issues=[]) for s in snaps]
    summary = build_report(header, calm)
    assert summary['pipeline'] is None
    assert 'Pipeline path over time' not in render_markdown(summary)


def test_cli_report_path_flag(tmp_path, capsys):
    header, snaps = _demo()
    path = tmp_path / 'demo.rgd.json'
    with open(path, 'w') as f:
        write_header(f, header)
        for s in snaps:
            append_snapshot(f, s)
    assert main(['report', str(path), '--path', '/planner']) == 0
    assert '## Pipeline path over time (focus: /planner)' in capsys.readouterr().out
