#include <ai_msgs/msg/perception_targets.hpp>
#include <hbm_img_msgs/msg/hbm_msg1080_p.hpp>
#include <rclcpp/rclcpp.hpp>

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <chrono>
#include <cstring>
#include <iomanip>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <map>
#include <unordered_map>
#include <vector>

namespace {

volatile sig_atomic_t stop_flag = 0;
void signal_handler(int) { stop_flag = 1; }

bool read_all(int fd, void *buffer, size_t size) {
  auto *dst = static_cast<unsigned char *>(buffer);
  while (size && !stop_flag) {
    ssize_t count = recv(fd, dst, size, 0);
    if (count > 0) { dst += count; size -= static_cast<size_t>(count); continue; }
    if (count == 0) return false;
    if (errno == EINTR) continue;
    if (errno == EAGAIN || errno == EWOULDBLOCK) { std::this_thread::sleep_for(std::chrono::milliseconds(1)); continue; }
    return false;
  }
  return size == 0;
}

bool write_all(int fd, const void *buffer, size_t size) {
  const auto *src = static_cast<const unsigned char *>(buffer);
  while (size && !stop_flag) {
    ssize_t count = send(fd, src, size, MSG_NOSIGNAL);
    if (count > 0) { src += count; size -= static_cast<size_t>(count); continue; }
    if (count < 0 && errno == EINTR) continue;
    return false;
  }
  return size == 0;
}

std::string json_escape(const std::string &value) {
  std::string result;
  for (char value_char : value) {
    if (value_char == '"' || value_char == '\\') { result += '\\'; result += value_char; }
    else if (value_char == '\n') result += "\\n";
    else result += value_char;
  }
  return result;
}

#pragma pack(push, 1)
struct CaptureHeader {
  uint64_t sequence; uint32_t width; uint32_t height; uint64_t timestamp_ns; uint64_t frames;
  uint32_t codec; uint32_t data_size; uint32_t active_slot; uint8_t reserved[20];
};
#pragma pack(pop)
static_assert(sizeof(CaptureHeader) == 64, "capture header must be 64 bytes");

class CaptureRing {
 public:
  explicit CaptureRing(const std::string &source) {
    path_ = "/dev/shm/aix5-" + source;
    fd_ = open(path_.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd_ < 0) throw std::runtime_error("capture ring unavailable: " + path_);
    struct stat info{};
    if (fstat(fd_, &info) != 0 || info.st_size < static_cast<off_t>(sizeof(CaptureHeader))) throw std::runtime_error("capture ring size invalid");
    size_ = static_cast<size_t>(info.st_size);
    map_ = static_cast<uint8_t *>(mmap(nullptr, size_, PROT_READ, MAP_SHARED, fd_, 0));
    if (map_ == MAP_FAILED) { map_ = nullptr; throw std::runtime_error("capture ring mmap failed"); }
  }
  CaptureRing(int owner_pid, int owner_fd) {
    path_ = "/proc/" + std::to_string(owner_pid) + "/fd/" + std::to_string(owner_fd);
    fd_ = open(path_.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd_ < 0) throw std::runtime_error("capture fd unavailable: " + path_);
    struct stat info{};
    if (fstat(fd_, &info) != 0 || info.st_size < static_cast<off_t>(sizeof(CaptureHeader))) throw std::runtime_error("capture fd size invalid");
    size_ = static_cast<size_t>(info.st_size);
    map_ = static_cast<uint8_t *>(mmap(nullptr, size_, PROT_READ, MAP_SHARED, fd_, 0));
    if (map_ == MAP_FAILED) { map_ = nullptr; throw std::runtime_error("capture fd mmap failed"); }
  }
  ~CaptureRing() { if (map_) munmap(map_, size_); if (fd_ >= 0) close(fd_); }
  bool latest(std::vector<uint8_t> &frame, int &width, int &height) {
    for (int retry = 0; retry < 20; ++retry) {
      uint64_t sequence_1 = __atomic_load_n(reinterpret_cast<uint64_t *>(map_), __ATOMIC_ACQUIRE);
      if (!sequence_1 || (sequence_1 & 1U)) { std::this_thread::yield(); continue; }
      CaptureHeader header{}; std::memcpy(&header, map_, sizeof(header)); uint32_t slots = 0; std::memcpy(&slots, header.reserved, sizeof(slots));
      if (slots < 2 || slots > 8 || header.width < 16 || header.height < 16 || header.data_size != header.width * header.height * 3 / 2 || header.active_slot >= slots) continue;
      size_t offset = sizeof(CaptureHeader) + static_cast<size_t>(header.active_slot) * header.data_size;
      if (offset + header.data_size > size_) continue;
      frame.resize(header.data_size); std::memcpy(frame.data(), map_ + offset, header.data_size); std::atomic_thread_fence(std::memory_order_acquire);
      uint64_t sequence_2 = __atomic_load_n(reinterpret_cast<uint64_t *>(map_), __ATOMIC_ACQUIRE);
      if (sequence_1 == sequence_2 && !(sequence_2 & 1U)) { width = header.width; height = header.height; return true; }
    }
    std::cerr << "ring latest failed" << std::endl;
    return false;
  }
 private:
  std::string path_; int fd_ = -1; size_t size_ = 0; uint8_t *map_ = nullptr;
};

class BodyBridgeNode final : public rclcpp::Node {
 public:
  explicit BodyBridgeNode(int socket_fd) : Node("aix5_body_bridge_native"), socket_fd_(socket_fd) {
    // Match the official hobot_codec publisher profile. The TROS shared-memory
    // subscriber is created with SensorDataQoS; using the same profile is
    // required for FastDDS shared-memory delivery on the X5. A generic
    // reliable publisher can appear in `ros2 topic info` while delivering no
    // HBM samples to the zero-copy subscription.
    publisher_ = create_publisher<hbm_img_msgs::msg::HbmMsg1080P>(
        "/aix5/body/shared/image", rclcpp::SensorDataQoS());
    subscription_ = create_subscription<ai_msgs::msg::PerceptionTargets>("/aix5/body/shared/out", 10, std::bind(&BodyBridgeNode::on_result, this, std::placeholders::_1));
  }
  void publish_latest(const std::string &source, uint32_t sequence, int owner_pid, int owner_fd) {
    auto ring = rings_.find(source);
    // The Python capture worker duplicates its ring fd. After a reconnect the
    // new process can reuse the same numeric fd, so fd-only caching would
    // leave this bridge mapped to the deleted ring from the previous worker.
    // Pair the fd with the owning PID to force a remap on every capture
    // process restart.
    if (ring == rings_.end() || ring_fds_[source] != owner_fd || ring_pids_[source] != owner_pid) {
      if (ring != rings_.end()) rings_.erase(ring);
      ring = rings_.emplace(source, std::make_unique<CaptureRing>(owner_pid, owner_fd)).first;
      ring_fds_[source] = owner_fd;
      ring_pids_[source] = owner_pid;
    }
    std::vector<uint8_t> frame; int width = 0, height = 0;
    if (!ring->second->latest(frame, width, height)) { send_empty(source + ":" + std::to_string(sequence)); return; }
    auto loaned = publisher_->borrow_loaned_message(); if (!loaned.is_valid()) { std::cerr << "loan unavailable" << std::endl; send_empty(source + ":" + std::to_string(sequence)); return; }
    auto &message = loaned.get(); int index = next_index_++; message.index = index; auto stamp = get_clock()->now();
    std::cerr << "publish index=" << index << " frame=" << source << ":" << sequence << std::endl;
    message.time_stamp.sec = static_cast<int32_t>(stamp.seconds()); message.time_stamp.nanosec = static_cast<uint32_t>(stamp.nanoseconds() % 1000000000LL);
    message.height = height; message.width = width; message.step = width; message.data_size = frame.size(); std::memset(message.encoding.data(), 0, message.encoding.size()); std::memcpy(message.encoding.data(), "nv12", 4);
    if (frame.size() > message.data.size()) { std::cerr << "frame too large" << std::endl; send_empty(source + ":" + std::to_string(sequence)); return; }
    std::memcpy(message.data.data(), frame.data(), frame.size());
    // Keep every published index until its matching result arrives. The
    // detector can have more than two samples in flight when four cameras
    // share one BPU; evicting at size > 2 silently discarded valid results.
    std::string dropped_frame;
    {
      std::lock_guard<std::mutex> lock(frame_mutex_);
      frame_ids_[index] = source + ":" + std::to_string(sequence);
      // Safety valve if the detector stops publishing. This is deliberately
      // high enough not to affect normal four-camera load.
      if (frame_ids_.size() > 1024) {
        auto oldest = frame_ids_.begin();
        dropped_frame = oldest->second;
        frame_ids_.erase(oldest);
      }
    }
    if (!dropped_frame.empty()) send_empty(dropped_frame);
    publisher_->publish(std::move(loaned));
  }
 private:
  void send_empty(const std::string &frame_id) { const std::string payload = "{\"frame_id\":\"" + json_escape(frame_id) + "\",\"detections\":[]}"; uint32_t size = htonl(payload.size()); std::lock_guard<std::mutex> lock(socket_mutex_); write_all(socket_fd_, &size, sizeof(size)); write_all(socket_fd_, payload.data(), payload.size()); }
  void on_result(const ai_msgs::msg::PerceptionTargets::ConstSharedPtr &message) {
    if (!message || socket_fd_ < 0) return; std::string frame_id = message->header.frame_id; std::vector<std::string> dropped;
    try {
      const int index = std::stoi(frame_id);
      std::lock_guard<std::mutex> lock(frame_mutex_);
      auto it = frame_ids_.find(index);
      if (it != frame_ids_.end()) {
        frame_id = it->second;
        frame_ids_.erase(it);
      } else {
        // Never guess a camera for an unknown result. Results can arrive out
        // of order; consuming the oldest pending frame crosses channels.
        std::cerr << "result index=" << index << " unmapped" << std::endl;
        return;
      }
    } catch (...) {
      std::cerr << "result index=" << frame_id << " invalid" << std::endl;
      return;
    }
    std::cerr << "result index=" << message->header.frame_id << " mapped=" << frame_id << " targets=" << message->targets.size() << std::endl;
    for (const auto &old : dropped) send_empty(old);
    std::ostringstream json; json << std::setprecision(8) << "{\"frame_id\":\"" << json_escape(frame_id) << "\",\"detections\":["; bool first = true;
    for (const auto &target : message->targets) { if (target.type != "person") continue; std::vector<std::array<float,3>> keypoints; for (const auto &group : target.points) if (group.type == "body_kps") { size_t count = std::min<size_t>(17, group.point.size()); for (size_t i = 0; i < count; ++i) keypoints.push_back({static_cast<float>(group.point[i].x), static_cast<float>(group.point[i].y), i < group.confidence.size() ? group.confidence[i] : 0.0f}); } for (const auto &roi : target.rois) { if (roi.type != "body" && roi.type != "head" && roi.type != "face" && roi.type != "hand") continue; if (!first) json << ','; first = false; json << "{\"label\":\"" << roi.type << "\",\"confidence\":1.0,\"model_confidence\":" << roi.confidence << ",\"bbox\":[" << roi.rect.x_offset << ',' << roi.rect.y_offset << ',' << roi.rect.x_offset + roi.rect.width << ',' << roi.rect.y_offset + roi.rect.height << "],\"track_id\":" << target.track_id << ",\"keypoints\":["; if (roi.type == "body") for (size_t i = 0; i < keypoints.size(); ++i) { if (i) json << ','; json << "{\"x\":" << keypoints[i][0] << ",\"y\":" << keypoints[i][1] << ",\"score\":" << keypoints[i][2] << '}'; } json << "]}"; } }
    json << "]}"; const std::string payload = json.str(); uint32_t size = htonl(payload.size()); std::lock_guard<std::mutex> lock(socket_mutex_); write_all(socket_fd_, &size, sizeof(size)); write_all(socket_fd_, payload.data(), payload.size());
  }
  int socket_fd_; int next_index_ = 1; std::mutex socket_mutex_, frame_mutex_; rclcpp::Publisher<hbm_img_msgs::msg::HbmMsg1080P>::SharedPtr publisher_; rclcpp::Subscription<ai_msgs::msg::PerceptionTargets>::SharedPtr subscription_; std::map<int,std::string> frame_ids_; std::unordered_map<std::string,std::unique_ptr<CaptureRing>> rings_; std::unordered_map<std::string,int> ring_fds_; std::unordered_map<std::string,int> ring_pids_;
};

}  // namespace

int main(int argc, char **argv) {
  signal(SIGINT, signal_handler); signal(SIGTERM, signal_handler); rclcpp::init(argc, argv); int server = socket(AF_INET, SOCK_STREAM, 0); int one = 1; setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)); sockaddr_in address{}; address.sin_family = AF_INET; address.sin_addr.s_addr = htonl(INADDR_LOOPBACK); address.sin_port = 0; if (bind(server, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0) return 2; socklen_t address_len = sizeof(address); getsockname(server, reinterpret_cast<sockaddr *>(&address), &address_len); listen(server, 1); fcntl(server, F_SETFL, O_NONBLOCK);
  auto node = std::make_shared<BodyBridgeNode>(-1); while (!stop_flag && node->count_subscribers("/aix5/body/shared/image") == 0) { rclcpp::spin_some(node); std::this_thread::sleep_for(std::chrono::milliseconds(50)); } std::cout << "READY " << ntohs(address.sin_port) << std::endl; int client = -1; while (!stop_flag && client < 0) { client = accept(server, nullptr, nullptr); if (client < 0) { rclcpp::spin_some(node); std::this_thread::sleep_for(std::chrono::milliseconds(10)); } } if (client < 0) { rclcpp::shutdown(); return 3; } timeval timeout{0,20000}; setsockopt(client,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout)); node.reset(); node = std::make_shared<BodyBridgeNode>(client); std::thread ros_thread([node]() { rclcpp::spin(node); });
  while (!stop_flag && rclcpp::ok()) { uint32_t length = 0; if (!read_all(client,&length,sizeof(length))) break; length = ntohl(length); if (length < 7 || length > 16*1024*1024) break; std::vector<uint8_t> packet(length); if (!read_all(client,packet.data(),packet.size())) break; uint16_t source_len=0; uint32_t sequence=0; std::memcpy(&source_len,packet.data(),2); std::memcpy(&sequence,packet.data()+2,4); source_len=ntohs(source_len); sequence=ntohl(sequence); if (6u+source_len+1u > packet.size()) break;
    std::string source(reinterpret_cast<const char *>(packet.data()+6),source_len);
    const uint8_t encoding = packet[6+source_len];
    if (encoding == 4) {
      if (packet.size() < 11u + source_len) break;
      uint32_t owner_pid=0, owner_fd=0; std::memcpy(&owner_pid,packet.data()+7+source_len,4); std::memcpy(&owner_fd,packet.data()+11+source_len,4);
      node->publish_latest(source,sequence,static_cast<int>(ntohl(owner_pid)),static_cast<int>(ntohl(owner_fd)));
    } else if (encoding == 3) {
      node->publish_latest(source,sequence,0,-1);
    } }
  close(client); close(server); rclcpp::shutdown(); if (ros_thread.joinable()) ros_thread.join(); node.reset(); return 0;
}
