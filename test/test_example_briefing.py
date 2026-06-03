"""Pin docs/example_briefing.md to its generator, so the showcase never drifts.

If this fails, regenerate the doc:
    python -m ros_graph_debugger.examples.sample_briefing
"""

import os

from ros_graph_debugger.examples.sample_briefing import (
    _doc_path,
    build_sample_briefing,
)


def test_committed_sample_matches_generator():
    with open(_doc_path()) as f:
        committed = f.read()
    assert committed == build_sample_briefing(), (
        'docs/example_briefing.md is stale — regenerate with '
        '`python -m ros_graph_debugger.examples.sample_briefing`')


def test_committed_sample_exists_under_docs():
    assert os.path.basename(_doc_path()) == 'example_briefing.md'


def test_sample_showcases_the_headline_features():
    md = build_sample_briefing()
    # bottleneck + stale TF + slow callback all present in the whole-system view
    assert 'Likely bottleneck: detector' in md
    assert 'TF map -> base_link is stale' in md
    assert 'Slow callback in /detector' in md
    # focused view adds the pipeline path with rate + callback bottlenecks
    assert '## Pipeline path' in md
    assert '⟵ slowest' in md and '⟵ slowest cb' in md
