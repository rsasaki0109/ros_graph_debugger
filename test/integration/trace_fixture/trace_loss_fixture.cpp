// Test-only: actual rclcpp dispatch emits the normal ROS tracepoints.
// File gates coordinate the owned recorder; no CTF or events are fabricated.
#include <chrono>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>
#include <thread>
#include <rclcpp/rclcpp.hpp>
int main(int argc, char **argv) {
  if (argc != 3 && argc != 4) return 2;
  const bool packets = argc == 4 && std::string(argv[3]) == "packets";
  if (argc == 4 && !packets) return 2;
  const std::filesystem::path root(argv[1]);
  const unsigned expected = std::stoul(argv[2]);
  if (packets && expected != 5500) return 2;
  rclcpp::init(argc, argv);
  auto node = std::make_shared<rclcpp::Node>("trace_loss_fixture",
      rclcpp::NodeOptions().start_parameter_services(false)
          .start_parameter_event_publisher(false));
  unsigned count = 0;
  auto timer = node->create_wall_timer(std::chrono::microseconds(1), [&count]() { ++count; });
  rclcpp::TimerBase::SharedPtr warm_timer;
  if (packets) {
    timer->cancel();
    warm_timer = node->create_wall_timer(std::chrono::milliseconds(1), [&count]() { ++count; });
  }
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(node);
  const auto deadline = std::chrono::steady_clock::now() +
      std::chrono::seconds(packets ? 45 : 30);
  auto wait = [&](const char *gate) {
    while (!std::filesystem::exists(root / gate)) {
      if (std::chrono::steady_clock::now() > deadline) return false;
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    return true;
  };
  std::ofstream(root / "ready") << "registered\n";
  if (packets) {
    if (!wait("warm-go")) return 3;
    while (count < 500) executor.spin_once(std::chrono::seconds(1));
    warm_timer->cancel();
    std::ofstream(root / "warm-done") << count << "\n";
  }
  if (!wait("go")) return 3;
  if (packets) timer->reset();
  while (count < expected) executor.spin_once(std::chrono::seconds(1));
  timer->cancel();
  std::ofstream(root / "done") << count << "\n";
  if (!wait("exit")) return 4;
  rclcpp::shutdown();
  return 0;
}
