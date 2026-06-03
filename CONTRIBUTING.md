# Contributing

Thanks for your interest in ROS Graph Debugger! Bug reports, feature ideas, and
PRs are all welcome.

> 🌐 New here? Click through the [live demo](https://rsasaki0109.github.io/ros_graph_debugger/)
> first — it's the real UI playing the bundled bottleneck scenario, no install.

## Development setup

Requires ROS 2 (Humble or Jazzy) on Linux and Node (for the web syntax check).

```bash
# in a colcon workspace with this package under src/ (or at the root)
source /opt/ros/$ROS_DISTRO/setup.bash
pip install fastapi uvicorn websockets pydantic psutil pyyaml pytest

colcon build --packages-select ros_graph_debugger
source install/setup.bash
```

## Running

```bash
ros2 run ros_graph_debugger agent            # live, opens http://localhost:3939
ros2 run ros_graph_debugger rgd serve --demo # no-ROS demo replay
```

## Tests

The suite is designed to run without a live ROS graph (no DDS required) — pure
logic, in-process rclpy endpoint checks, and a uvicorn-in-thread server.

```bash
python3 -m pytest test/ -v
node --check ros_graph_debugger/web/app.js
```

CI runs the same on Humble and Jazzy (`.github/workflows/ci.yml`).

## Guidelines

- **Keep the agent safe by default**: no auto-subscribing to large topics, bound
  memory, never modify the target system. See
  [docs/performance_safety.md](docs/performance_safety.md).
- **Be honest about uncertainty** (e.g. node→process mapping confidence).
- **Match the surrounding style**: standard library first, minimal dependencies,
  comments that explain *why*.
- If you add or change an HTTP route, update [docs/api.md](docs/api.md) — a test
  (`test/test_docs_api.py`) enforces that code and docs agree.
- Add a test for new analysis rules or data transforms; they should be testable
  without DDS (inject into `RuntimeGraphStore` or pass duck-typed objects).

## Pull requests

Keep PRs focused, describe the user-facing change, and make sure
`pytest test/` and the web check pass. Be kind — see
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
