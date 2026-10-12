// Test-only rclcpp node: exercise real subscription, timer and service tracepoints.
#include <chrono>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>

#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/float64.hpp>
#include <std_srvs/srv/trigger.hpp>

class TraceFixture : public rclcpp::Node {
 public:
  explicit TraceFixture(const std::string &mode)
      : Node("trace_" + mode, rclcpp::NodeOptions()
                 .start_parameter_services(false)
                 .start_parameter_event_publisher(false)),
        slow_(mode == "slow") {
    subscription_ = create_subscription<std_msgs::msg::Float64>(
        "~/input", 10, [this](std_msgs::msg::Float64::ConstSharedPtr) {
          work();
          ++subscriptions_;
          progress();
        });
    timer_ = create_wall_timer(std::chrono::milliseconds(200), [this]() {
      work();
      ++timers_;
      progress();
    });
    service_ = create_service<std_srvs::srv::Trigger>(
        "~/reset", [this](const std::shared_ptr<std_srvs::srv::Trigger::Request>,
                          std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
          work();
          response->success = true;
          ++services_;
          progress();
        });
  }

 private:
  void work() const {
    // This is the injected callback workload, not a readiness delay.
    if (slow_) {
      std::this_thread::sleep_for(std::chrono::milliseconds(150));
    }
  }
  void progress() const {
    std::cout << "RGD_PROGRESS " << subscriptions_ << " " << timers_ << " "
              << services_ << std::endl;
  }
  bool slow_;
  unsigned subscriptions_ = 0, timers_ = 0, services_ = 0;
  rclcpp::Subscription<std_msgs::msg::Float64>::SharedPtr subscription_;
  rclcpp::TimerBase::SharedPtr timer_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr service_;
};

int main(int argc, char **argv) {
  if (argc != 2 || (std::string(argv[1]) != "slow" && std::string(argv[1]) != "healthy")) {
    throw std::invalid_argument("usage: trace_fixture slow|healthy");
  }
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<TraceFixture>(argv[1]));
  rclcpp::shutdown();
  return 0;
}
