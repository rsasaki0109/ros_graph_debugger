# Live agent in a container — the DDS graph is reached over the host network:
#   docker run --rm --net=host --pid=host ghcr.io/rsasaki0109/ros_graph_debugger
# then open http://localhost:3939. Pass agent flags after the image name, e.g.
#   docker run --rm --net=host --pid=host ghcr.io/rsasaki0109/ros_graph_debugger --profile nav2
# Match ROS_DOMAIN_ID (and RMW_IMPLEMENTATION) to the robot with -e.
ARG ROS_DISTRO=jazzy
FROM ros:${ROS_DISTRO}-ros-base

ARG ROS_DISTRO
SHELL ["/bin/bash", "-c"]

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3-pip python3-colcon-common-extensions python3-psutil python3-yaml \
      ros-${ROS_DISTRO}-std-msgs ros-${ROS_DISTRO}-tf2-msgs \
      ros-${ROS_DISTRO}-diagnostic-msgs ros-${ROS_DISTRO}-statistics-msgs \
      ros-${ROS_DISTRO}-lifecycle-msgs \
 && rm -rf /var/lib/apt/lists/*
# Same pip fallback as CI: --break-system-packages is required on 24.04 (jazzy)
# and unknown to the older pip on 22.04 (humble).
RUN pip3 install --no-cache-dir --break-system-packages fastapi uvicorn websockets pydantic \
 || pip3 install --no-cache-dir fastapi uvicorn websockets pydantic

WORKDIR /ws
COPY . src/ros_graph_debugger
RUN source /opt/ros/${ROS_DISTRO}/setup.bash \
 && colcon build --packages-select ros_graph_debugger \
 && rm -rf build log

# Sourcing the workspace on top of the base image's ROS setup.
RUN sed -i 's|^exec "\$@"|source /ws/install/setup.bash --\nexec "$@"|' /ros_entrypoint.sh

EXPOSE 3939
ENTRYPOINT ["/ros_entrypoint.sh", "ros2", "run", "ros_graph_debugger", "agent", "--host", "0.0.0.0", "--no-browser"]
