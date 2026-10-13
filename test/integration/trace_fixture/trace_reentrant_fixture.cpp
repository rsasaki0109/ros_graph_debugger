// Probe-only real rclcpp reentrant subscription; executor emits all trace events.
#include <chrono>
#include <condition_variable>
#include <fstream>
#include <mutex>
#include <thread>
#include <sys/syscall.h>
#include <unistd.h>
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/int32.hpp>

int main(int argc, char **argv) {
  if (argc != 2) return 2;
  using namespace std::chrono_literals;
  rclcpp::init(argc, argv);
  auto node = std::make_shared<rclcpp::Node>("trace_reentrant_fixture",
      rclcpp::NodeOptions().start_parameter_services(false).start_parameter_event_publisher(false));
  auto group = node->create_callback_group(rclcpp::CallbackGroupType::Reentrant);
  std::mutex mutex;
  std::condition_variable changed;
  bool entered[2] = {false, false}, release[2] = {false, false}, done[2] = {false, false};
  long tids[2] = {0, 0};
  double durations[2] = {0, 0};
  const auto deadline = std::chrono::steady_clock::now() + 20s;
  rclcpp::SubscriptionOptions options;
  options.callback_group = group;
  auto subscription = node->create_subscription<std_msgs::msg::Int32>(
      "/trace_reentrant/input", 10, [&](std_msgs::msg::Int32::ConstSharedPtr message) {
        const unsigned id = message->data;
        if (id >= 2) return;
        const auto start = std::chrono::steady_clock::now();
        std::unique_lock<std::mutex> lock(mutex);
        tids[id] = syscall(SYS_gettid);
        entered[id] = true;
        changed.notify_all();
        if (!changed.wait_until(lock, deadline, [&] { return release[id]; })) return;
        durations[id] = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
        done[id] = true;
        changed.notify_all();
      }, options);
  auto publisher = node->create_publisher<std_msgs::msg::Int32>("/trace_reentrant/input", 10);
  rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 4);
  executor.add_node(node);
  std::thread spin([&] { executor.spin(); });
  int status = 0;
  auto wait = [&](auto predicate) {
    std::unique_lock<std::mutex> lock(mutex);
    return changed.wait_until(lock, deadline, predicate);
  };
  while (publisher->get_subscription_count() == 0 && std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  if (publisher->get_subscription_count() == 0) status = 3;
  if (!status) {
    std_msgs::msg::Int32 message;
    message.data = 0;
    publisher->publish(message);
    if (!wait([&] { return entered[0]; })) status = 4;
    // Workload spacing deliberately makes crossing-thread mispairing measurable.
    std::this_thread::sleep_for(100ms);
    message.data = 1;
    publisher->publish(message);
    if (!wait([&] { return entered[1]; })) status = 5;
    std::this_thread::sleep_for(100ms);
    { std::lock_guard<std::mutex> lock(mutex); release[0] = true; changed.notify_all(); }
    if (!wait([&] { return done[0]; })) status = 6;
    std::this_thread::sleep_for(100ms);
    { std::lock_guard<std::mutex> lock(mutex); release[1] = true; changed.notify_all(); }
    if (!wait([&] { return done[1]; })) status = 7;
  }
  executor.cancel();
  spin.join();
  rclcpp::shutdown();
  std::ofstream output(argv[1]);
  output << "{\"status\":" << status << ",\"samples\":[";
  for (unsigned id = 0; id < 2; ++id) {
    if (id) output << ',';
    output << "{\"id\":" << id << ",\"tid\":" << tids[id]
           << ",\"duration_ms\":" << durations[id] << '}';
  }
  output << "]}\n";
  return status;
}
