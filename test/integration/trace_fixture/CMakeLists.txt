cmake_minimum_required(VERSION 3.16)
project(rgd_trace_fixture)

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)

add_executable(trace_fixture trace_fixture.cpp)
target_compile_features(trace_fixture PRIVATE cxx_std_17)
ament_target_dependencies(trace_fixture rclcpp std_msgs std_srvs)

add_executable(trace_loss_fixture trace_loss_fixture.cpp)
target_compile_features(trace_loss_fixture PRIVATE cxx_std_17)
ament_target_dependencies(trace_loss_fixture rclcpp)
