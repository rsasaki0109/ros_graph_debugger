# Releasing

A release is a version bump plus a tag. CI does the publishing.

## 1. Bump the version

The version lives in `ros_graph_debugger/__init__.py` and is mirrored in
`setup.py`, `package.xml`, and the top `## [x.y.z]` heading of `CHANGELOG.md`
(move the `[Unreleased]` entries under it). `test/test_version.py` fails if any
of them disagree.

## 2. Tag and push

```bash
git tag v0.5.0
git push origin v0.5.0
```

`.github/workflows/release.yml` then:

- checks the tag matches `__version__`;
- builds the sdist + wheel with `RGD_PYPI_BUILD=1` (only the `rgd` console
  script is installed, so a pip install doesn't put a generic `agent` on the
  PATH) and publishes them to PyPI;
- builds the agent image for Jazzy and Humble and pushes it to
  `ghcr.io/rsasaki0109/ros_graph_debugger` as `:X.Y.Z-<distro>` and `:<distro>`,
  plus `:X.Y.Z` and `:latest` for Jazzy.

Run the workflow by hand (**Actions → Release → Run workflow**) for a dry run:
everything builds, nothing is published.

## One-time setup

**PyPI trusted publishing** (no API token stored in the repo): on PyPI, add a
*pending publisher* for project `ros_graph_debugger` with owner
`rsasaki0109`, repository `ros_graph_debugger`, workflow `release.yml`, and
environment `pypi`. Create the `pypi` environment in the GitHub repo settings
(optionally with a required reviewer, to approve each upload).

**GHCR**: the first push creates the package as private. Make it public under
the package's settings on GitHub so `docker pull` works without a login.

## ROS buildfarm (apt `ros-<distro>-ros-graph-debugger`)

Not automated — it needs a release repository and a `rosdistro` PR:

1. Create an empty `rsasaki0109/ros_graph_debugger-release` repository.
2. `pip install bloom`, then from this repo:
   `bloom-release --rosdistro jazzy --track jazzy ros_graph_debugger --new-track`
   (repeat for `humble`) and answer its prompts with the release repo URL.
3. Bloom opens the `ros/rosdistro` pull request. The rosdep keys in
   `package.xml` (`python3-fastapi`, `python3-uvicorn`, …) must resolve on the
   target platforms, or the buildfarm job fails.
