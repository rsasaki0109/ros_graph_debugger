"""Headless tests for the rgd top data model and CLI smoke path."""

import asyncio

import pytest

from ros_graph_debugger.cli import main
from ros_graph_debugger.replay import build_demo_recording
from ros_graph_debugger.top_tui import (
    TopHistory,
    build_top_model,
    create_top_app,
    recording_source,
    render_plain,
    sparkline,
    textual_missing_message,
)


def test_sparkline_uses_block_levels():
    s = sparkline([0, 1, 2, 3, 4], width=5)
    assert len(s) == 5
    assert s[0] != s[-1]


def test_top_model_surfaces_demo_bottleneck():
    header, snaps = build_demo_recording()
    hist = TopHistory()
    model = None
    for snap in snaps[:18]:
        model = build_top_model(snap, hist, profile=header['profile'])

    assert model is not None
    assert any(stage == 'perception' and status == 'critical'
               for stage, status in model.readiness)
    assert any('bottleneck' in issue['kind'] for issue in model.issues)
    detector = next(r for r in model.rows if r.name == '/detector')
    objects = next(r for r in model.rows
                   if r.name == '/perception/object_recognition/objects')
    assert detector.callback_p95_ms and detector.callback_p95_ms > 100
    assert objects.status == 'critical'
    assert objects.spark


def test_render_plain_fits_80x24():
    header, snaps = build_demo_recording()
    hist = TopHistory()
    model = build_top_model(snaps[18], hist, profile=header['profile'])
    text = render_plain(model, width=80, height=24)
    lines = text.splitlines()
    assert len(lines) <= 24
    assert all(len(line) <= 80 for line in lines)
    assert 'Issues' in text


def test_textual_missing_message_points_to_extra():
    assert 'ros_graph_debugger[tui]' in textual_missing_message()


def test_cli_top_plain_demo(capsys):
    assert main(['top', '--demo', '--plain', '--frames', '18',
                 '--width', '80', '--height', '24']) == 0
    out = capsys.readouterr().out
    assert 'ROS Graph Top' in out
    assert 'detector' in out
    assert 'Likely bottleneck' in out


def test_textual_app_builds_with_pilot_when_installed():
    pytest.importorskip('textual')
    from textual.widgets import DataTable, Static

    header, snaps = build_demo_recording()
    app = create_top_app(recording_source(snaps[18:19], loop=False),
                         interval=60.0, profile=header['profile'])

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one('#table', DataTable)
            issues = app.query_one('#issues', Static)
            assert table.row_count > 0
            assert 'Likely bottleneck' in str(issues.content)
            await pilot.press('s')
            await pilot.pause()
            assert app.sort_idx == 1

    asyncio.run(run())
