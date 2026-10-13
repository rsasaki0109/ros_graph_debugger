// Test-only: actual rclcpp timer dispatch emits its normal ROS tracepoints.
// File gates coordinate the owned recorder; no CTF or trace events are fabricated.
#include <chrono>
#include <filesystem>
#include <fstream>
#include <memory>
#include <thread>
#include <rclcpp/rclcpp.hpp>
int main(int argc, char **argv) {
  if (argc != 3) return 2;
  const std::filesystem::path root(argv[1]);
  const unsigned expected = std::stoul(argv[2]);
  rclcpp::init(argc, argv);
  auto node = std::make_shared<rclcpp::Node>("trace_loss_fixture",
      rclcpp::NodeOptions().start_parameter_services(false)
          .start_parameter_event_publisher(false));
  unsigned count = 0;
  auto timer = node->create_wall_timer(std::chrono::microseconds(1), [&count]() { ++count; });
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(node);
  std::ofstream(root / "ready") << "registered\n";
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
  while (!std::filesystem::exists(root / "go")) {
    if (std::chrono::steady_clock::now() > deadline) return 3;
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  while (count < expected) executor.spin_once(std::chrono::seconds(1));
  timer->cancel();
  std::ofstream(root / "done") << count << "\n";
  while (!std::filesystem::exists(root / "exit")) {
    if (std::chrono::steady_clock::now() > deadline) return 4;
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  rclcpp::shutdown();
  return 0;
}
