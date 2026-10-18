# RDK X5 局域网 AI 视频分析应用：技术架构与设计演进

## 1. 文档目的

> **公开版本**：v0.4（2026-10-18）
>
> **文档定位**：本文保留早期方案的设计动机，并更新主要架构到当前源码。安装、端口、配置项和运行步骤以 [README](README.md) 及源码为准；历史阶段划分不代表尚未实现的功能已经交付。旧版中“自动软件回退、CPU 编码、MQTT 作为 HA 主接入方式”的描述已由下面的硬件优先、显式回退和 ONVIF 方案替代。本文不包含真实部署的账号、密码、内网地址、设备标识或阶段实测记录。

**当前架构**：FastAPI 管理设备、规则和本地存储；每路原生采集进程使用 RDK VPU 解码；共享编码管线使用 VPU 输出 H.264。浏览器采用 WebRTC（H.264 + Opus），Home Assistant 采用 ONVIF 设备/媒体/事件服务与 RTSP（H.264 + AAC）。BPU 推理在独立工作进程执行，默认每路分析上限 5 FPS；视频播放与分析频率分离，跟随摄像头实际输入帧率。

默认 Web / ONVIF 端口为 `8080`，RTSP 输出端口为 `8554/TCP`，systemd 服务名为 `aix5-ai.service`。应用、虚拟环境和数据路径由部署配置决定；本文中的路径仅为示例，不是某台设备的真实环境。

本文用于统一三类信息：

1. 产品目标：2～4 路摄像头、区域规则、事件留存、ONVIF / Home Assistant 接入和长期运行。
2. 当前源码基线：控制面、媒体面、推理调度、规则状态与持久化的职责。
3. 后续边界：专用模型、四路负载、长期稳定性及更多协议兼容性仍需分别验证。

```text
ONVIF / RTSP 摄像头
        │
        ├── 每路 native capture → VPU 解码 → NV12 共享内存有界环形缓冲
        │       ├── 独立采样 → BGR / 640×640 预处理 → BPU 工作进程
        │       │        └── 检测结果 → RuleEngine → SQLite / 事件截图
        │       │                                └── ONVIF Events PullPoint
        │       └── 共享 VPU H.264 编码
        │                ├── aiortc / WebRTC → 浏览器 video
        │                └── RTSP 发布 → MediaMTX → Home Assistant
        │
        └── FFmpeg 音频探测/解码 → 有界 PCM 缓冲
                 ├── Opus → WebRTC
                 └── AAC → RTSP

检测元数据 → WebRTC DataChannel → 浏览器 Canvas 独立叠加
硬件失败 → 分层重试；仅显式启用软件回退后允许软件视频编解码
```

FastAPI / Uvicorn 是单个服务控制进程。采集、原生硬编码、BPU 推理和 MediaMTX 使用各自受管理的子进程；每路异步任务负责生命周期和状态，避免同步推理或编码阻塞控制面。MQTT 代码保留为旧部署兼容路径，当前网页和新接入流程使用 ONVIF。

## 2. 产品约束与参考硬件

### 2.1 用户需求

- 目标是可以长期运行的完整应用，允许分阶段实现。
- 第一阶段支持 2～4 路局域网摄像头。
- 摄像头来源：ONVIF 发现并获取 RTSP 地址，或手动添加 RTSP URL。
- 支持 H.264、H.265；RTSP URL 可能包含用户名和密码，配置只保存在开发板本地。
- 断线后指数退避重连；Web 页面显示状态并支持手动重连；Home Assistant 显示离线状态。
- 使用官方模型或 Model Zoo 模型，不做额外训练。
- 当前已支持 COCO 80 类；烟火/烟雾需要专用模型，尚不作为可用检测能力。规则支持矩形、多边形区域。
- 规则可在 Web 页面配置：目标类型、置信度阈值、冷却时间、区域范围。
- 保存事件记录和事件截图，不保存连续视频；默认保留 7 天，可选 3 / 7 / 30 天，并按容量清理旧数据。
- Web 页面显示 CPU、内存、BPU 使用率，要求可连续运行 24 小时。
- Home Assistant 通过原生 ONVIF 集成获得每路视频和每条规则的 binary_sensor；配套规则同步组件同步显示名称及删除操作，无需 MQTT Broker。
- 事件至少包含摄像头名称、事件类型、目标类型、置信度、时间、截图 URL。
- Web 仅局域网访问，有单用户账号密码；页面只做中文桌面浏览器适配。
- 页面包括画面预览、设备管理、规则配置、事件记录、ONVIF 联动和系统设置；完整日志查询是后续增强项。
- 配置与规则持续保存，事件按保留策略清理；支持导出。
- 板端：RDK X5、RDK OS 3.5.0、Ubuntu 22.04.5、aarch64、8 GB 内存、32 GB TF 卡；无外接盘。
- 开机自动启动，异常退出自动重启；允许联网安装依赖和模型；通过 SSH/VS Code Remote SSH 开发和验证。

### 2.2 参考验证配置

以下硬件描述来自既有开发记录，仅表示参考配置；具体内存、存储和系统包版本应以实际板卡为准。

| 项目 | 参考配置 |
|---|---|
| 开发板 | D-Robotics RDK X5 |
| CPU / 架构 | 8 核 Cortex-A55，ARM64 / aarch64 |
| 内存 | 8 GB |
| 存储 | 32 GB TF 卡；应用不要求外接盘 |
| 操作系统 | RDK OS 3.5.0，Ubuntu 22.04.5 |
| 图形环境 | Xubuntu / XFCE、LightDM；应用运行不依赖桌面会话 |
| 媒体与推理接口 | RDK `hb_mm_mc` 多媒体接口、`hbm_runtime.HB_HBMRuntime` |

本项目没有把 SoC 的营销型号或独立 SDK 小版本写死为运行契约；应根据 RDK OS 镜像核对头文件、动态库与模型兼容性。官方说明参见 [RDK X5 文档](https://developer.d-robotics.cc/rdk_x_doc/RDK?v=3.5.0&p=RDK+X5)。

当前源码支持 YOLO11n / YOLO11m 的已登记模型文件；烟火模型尚未验证。Web 前端为 FastAPI 返回的内嵌 HTML/CSS/JavaScript，未使用 React/Vite。SQLite 使用 WAL，事件目录、数据库和凭据文件位于 `AIX5_DATA_DIR`。

性能取决于输入码率、Profile、帧率、模型、并发观看者和是否软件回退。历史某一时刻的 CPU、内存和帧率采样不构成通用指标；验收时应同时记录输入解码、输出编码、浏览器接收帧率、冻结和丢包，而不能仅以模型 FPS 推断播放流畅度。

## 3. 总体设计决策

### 3.1 应用形态

当前采用 systemd 管理的原生部署，以便使用 RDK 媒体和 BPU 驱动：

```text
FastAPI / Uvicorn（控制进程）
  ├─ 单用户登录、设备管理、系统设置
  ├─ ONVIF 输入发现/探测与输出 Device/Media/Analytics/Events
  ├─ 每路采集 worker、WebRTC peer、共享视频管线
  ├─ RuleEngine、SQLite、事件截图与保留策略
  ├─ 每路 native capture / native encoder 子进程
  ├─ 单 BPU 工作进程：按 model_id 加载并缓存 detector
  └─ MediaMTX 子进程：认证 RTSP 输出

Home Assistant
  ├─ 原生 ONVIF：视频 Profile、PullPoint、规则传感器状态
  └─ edge_ai_onvif 配套组件：规则目录、名称和删除同步
```

控制面和计算任务通过有界帧队列、共享内存及进程任务接口协作。浏览器和 RTSP 消费者复用编码结果，避免每个观看者重复硬编码。媒体与推理异常分别恢复；不能把某一通道失败扩散为所有摄像头停止。

## 4. 视频接入与处理架构

### 4.1 摄像头生命周期

每个摄像头有独立状态机：

```text
DISABLED
   │ enable
   ▼
CONNECTING ──成功──► RUNNING ──断流/超时──► BACKOFF
   ▲                    │                     │
   │                    └─手动重连────────────┘
   └──────────────失败 ◄───────────────────────┘
```

当前采集状态包括 `disabled`、`connecting`、`running`、`disconnected`、`paused` 和 `error`；图中的 BACKOFF 表示重试等待阶段，不要求单独持久化为同名状态。

每个 worker 提供：

- 连接和首帧分别设置超时，具体数值以采集器实现为准。
- `read_timeout`：5 秒无帧视为异常。
- 指数退避：1、2、4、8、16、32、60 秒；成功收到稳定帧后清零。
- 最大重试间隔 60 秒。
- 手动重连立即取消当前等待并开始新连接。
- 连续失败原因记录到日志和运行时状态，但不把包含密码的原始 URL 写入日志。

### 4.2 RTSP 接入

默认视频路径要求硬件编解码，采集链路为：

1. `HardwareCapture` 为每路摄像头启动 native 采集器。
2. native 采集器负责 RTSP 连接、RTP 解析和 RDK VPU 硬件解码。
3. 解码后的 NV12 帧写入 `/dev/shm/aix5-<camera_id>`，采用有界环形缓冲和序列号校验。
4. 原生编码器按视频时间戳读取 NV12；Python 仅按分析频率采样并转换 BGR，JPEG 截图为旁路，不阻塞播放。
5. 首帧超时或硬件解码错误进入重试/错误状态。系统设置 `allow_software_fallback` 默认关闭，只有显式开启时才允许 FFmpeg 软件解码。
6. 硬件编码初始化在原生库、Python 编码适配层和原生流进程管理层分别设置有限重试、退避和恢复机制，处理上电时 VPU 尚未就绪的情况。

软件回退会增加 CPU 占用，可能降低输出尺寸或分析频率，状态中明确标注 `software`；默认模式不以软件编码掩盖硬件失败。音频解码独立使用 FFmpeg，不受视频硬件回退开关限制。

当前状态机保留指数退避：1、2、4、8、16、32、60 秒；手动重连会取消当前 worker 任务并立即重新建立连接。

## 5. 浏览器实时预览

### 5.1 当前 WebRTC 预览协议

当前主预览路径已经从 MJPEG 升级为 WebRTC/H.264：

- 浏览器为每路摄像头创建 `RTCPeerConnection`，向 `/api/cameras/{camera_id}/webrtc/offer` 提交 offer。
- 服务端使用 `aiortc` 发送共享管线生成的 H.264 包，优先协商 H.264。
- 浏览器主动创建名为 `detections` 的 DataChannel；服务端在通道打开后发送检测 JSON。
- 视频内容和检测元数据分离：视频保持干净，Canvas 在外部叠加框、中文名称和置信度。
- 系统设置可选 `960×540` 或 `1920×1080` 预览上限；保持源画面比例，不把低分辨率输入放大成伪高清。全屏切换通过现有连接更新预览模式。
- 页面登录后建立连接；切换其他 Tab 不关闭 WebRTC，因此返回预览不会黑屏。页面关闭或刷新时才释放连接。

当前主发送路径使用 RDK VPU H.264 编码，原生输入/输出线程并行驱动硬件队列。初始化失败先重试；显式允许软件回退后才可使用 libx264。视频播放保持源实际帧率，AI 的 5 FPS 分析上限不限制播放。

有输入音频时，FFmpeg 解码为有界 PCM 缓冲，WebRTC 协商 Opus；预览卡片显示输入等级，每路声音默认关闭、独立开启。无音频轨道时显示无音频输入，不错误声明 AAC/Opus 源能力。

### 5.2 兼容回退

`/api/cameras/{camera_id}/mjpeg` 和 `/api/cameras/{camera_id}/preview.jpg` 仍保留，用于浏览器 WebRTC 不可用、调试和规则画面。正常页面优先 WebRTC；回退会显示 MJPEG，但不应作为两路以上长期预览的主路径。

## 6. AI 推理与模型架构

### 6.1 BPU 推理

使用板端 `hbm_runtime.HB_HBMRuntime` 加载官方 `.bin` 模型，通过 `set_scheduling_params` 设置优先级和 BPU 核；不使用 OpenCV DNN 作为正式推理后端。

每路 `inference_loop` 从长度为 1 的最新帧队列取帧，按 `analysis_fps` 节流；全局信号量与独立的单 BPU 工作进程限制并发推理。预处理缩小为 640×640 后传给工作进程，避免将全部 1080p 帧在进程间反复复制。

当前已实现：

- 按摄像头 `model_id` 在 BPU 工作进程中加载并缓存模型。
- 摄像头队列满时丢弃旧帧。
- 记录推理延迟、检测数和最后错误。
- 检测框坐标映射回原始 BGR 分辨率。

### 6.2 第一版模型策略

建议默认使用轻量目标检测模型：

- 人、猫、狗等 COCO 类别：优先使用 X5 Model Zoo 的 YOLO11n/YOLOv8n 级别模型；必要时使用现有 `yolov8_640x640` 或下载官方 X5 `.bin`。
- 烟、火、烟雾：必须使用带有对应标签的专用模型。通用 COCO 检测模型没有可靠的烟火类别，不能把“检测到某个普通物体”当作烟火检测结果。
- 模型以注册表管理，不把类别名称硬编码在前端：`model_id`、模型文件、输入尺寸、标签文件、后处理版本、适用硬件、版本和校验值均登记。
- 第一阶段先用官方可直接运行的模型打通人/宠物/通用目标链路；启动时检查烟火模型是否存在，缺失时在 Web 页面明确标记“模型未安装”，不能静默伪造能力。

### 6.3 输入预处理和后处理

当前 YOLO11n 预处理为：

1. 摄像头原始帧先转 BGR。
2. 使用保持宽高比的 Letterbox 缩放到 `640×640`。
3. 使用 OpenCV 转换成 NV12。
4. 送入 `hbm_runtime.HB_HBMRuntime`，输入张量展开为 `(1, 960, 640, 1)`。
5. 后处理把检测框映射回摄像头原始 BGR 尺寸。

因此当前存在一次 BGR→NV12 转换；后续性能优化可以改成从共享内存 NV12 直接缩放和预处理，但必须保持 Model Zoo 的 letterbox 和量化约定不变。

### 6.4 资源保护策略

当前已落实：

- 每路帧队列最大长度为 1，满队列丢弃旧帧。
- inference 使用全局 `asyncio.Semaphore(1)`，避免多路并发推理把 BPU/CPU 挤爆。
- WebRTC 主路径复用原生 NV12 编码输出，不依赖 JPEG 预览缓存；JPEG 预览异步旁路生成。
- 消费者存在时创建/保留共享 H.264 管线，RTSP 和 WebRTC 复用；所有消费者退出后释放资源。
- 硬件失败先重试；只有显式允许时软件回退。

当前建议档位：

| 路数 | 输入 | BPU 模型 | 分析帧率 | 预览 |
|---|---|---|---:|---|
| 1～2 路 | 1920×1080 优先子码流 | YOLO11n 640×640 | 约 5 FPS/路 | WebRTC H.264 |
| 3～4 路 | 根据总负载选择主/子码流；严格硬件模式下不兼容码流报错 | YOLO11n 640×640 | 上限 5 FPS/路 | 可选 540p / 1080p，上限不超过源尺寸 |

高分辨率 BPU 输入不能只修改 Python 常量，必须有对应输入尺寸编译的 `.bin` 模型和匹配后处理。

## 7. 区域规则与事件状态机

### 7.1 区域数据

前端在当前摄像头的预览画布上绘制：

- 矩形：`{type: "rectangle", points: [[x1,y1],[x2,y2]]}`
- 多边形：`{type: "polygon", points: [[x1,y1], ...]}`

坐标以原图宽高归一化到 `[0, 1]`，保存时不受浏览器缩放、画面大小影响。

### 7.2 区域命中

默认使用检测框底部中心点作为“目标所在位置”，原因是人和宠物的脚/身体底部更适合判断是否进入地面区域。规则可在后续增加：

- 框中心点
- 框与区域的交集比例
- 越线方向
- 停留时间

### 7.3 规则字段

当前 API 中 `zone` 以归一化点列表保存；前端根据绘制模式解释为矩形或多边形。规则字段为：

```json
{
  "id": "rule-uuid",
  "camera_id": "camera-uuid",
  "enabled": true,
  "target_labels": ["person", "cat", "dog"],
  "confidence_threshold": 0.35,
  "cooldown_seconds": 60,
  "zone": [[0.10, 0.20], [0.90, 0.20], [0.90, 0.95], [0.10, 0.95]],
  "min_hits": 2,
  "clear_seconds": 3
}
```

### 7.4 事件触发

每路摄像头、每条规则都维护独立状态：

```text
CLEAR → 连续命中累计 → ACTIVE
                       ├─ 新活动期且冷却结束 → 正式事件 → ONVIF true
                       └─ 冷却未结束 → 无新正式事件
ACTIVE → 超过 clear_seconds 未命中 → CLEAR → 清除已发布的活动状态
```

当前本地事件 payload 包含：

- `id`：本地事件 ID；ONVIF 扩展字段 `EventId` 引用同一值。
- `camera_id` / `camera_name`、`rule_id` / `rule_name`。
- `event_type`：`target_detected` 或 `target_entered_zone`。
- `target_type`、`confidence`、`bbox`。
- `timestamp`：UTC ISO 8601；`created`：epoch 秒。
- `image_path`、`screenshot_url`、`size`。

断线状态用于清除规则告警，不伪造为普通目标检测事件；区域快照、越线方向和独立去重字段可在后续扩展。

规则内部按摄像头、规则和目标类别维护命中状态；正式告警必须满足类别、阈值、区域、连续命中及冷却条件。ONVIF 的 true 只来自正式事件，冷却期间即使目标仍在场也不产生新的 true。

对外每条界面规则对应一个稳定的 ONVIF Rule token 和一个传感器。多个类别的已触发告警聚合为 OR，最后一个活动目标清除后才发 false。改名保留 token 和状态；停用/断线清除状态，删除发 Deleted，并由 HA 配套组件清理实体。新规则默认阈值来自所选模型的实际检测下限（当前 COCO 模型为 0.35），不允许创建低于算法过滤下限的无效阈值。

## 8. 存储、覆盖和保留策略

### 8.1 目录布局

```text
/opt/aix5-ai/current/    # 当前代码和 native 采集器
/opt/aix5-ai/venv/        # Python 虚拟环境
/var/lib/aix5-ai/
  app.db                          # SQLite，WAL 模式
  events/<event-id>.jpg          # 事件 JPEG
  initial-admin.txt               # 首次启动随机账号提示文件，0600
  .onvif-secret.key               # ONVIF 凭据加密密钥，0600；与数据库配对备份

journald                         # 当前 systemd 日志来源
```

摄像头、RTSP URL、规则和 Home Assistant 配置都只保存在板端 SQLite/配置文件；不上传云端。

### 8.2 账号和凭据

- Web 使用单个本地账户，Argon2id 验证密码；新安装随机生成初始密码，升级保留已有账户。
- ONVIF 严格校验 HTTP Basic、WS-Security PasswordText / PasswordDigest；Digest 校验时间范围并拒绝重复 Nonce。
- Digest 需要可恢复的共享密码，因此 ONVIF 凭据单独加密保存；密钥位于数据目录的权限受限文件，备份数据库时必须配对保管密钥。不能将此设计描述为“所有用途都只保存不可逆哈希”。
- 摄像头 RTSP URL 保存在本地配置中，仅供授权控制面和采集器使用。导出脱敏，日志避免完整凭据；源 RTSP 凭据不出现在网关 `GetStreamUri` 返回值中。
- RTSP 网关使用应用账户认证，内部发布凭据仅供本机发布进程使用。
- 用户名/密码变更在系统设置执行，撤销旧会话并同步 ONVIF / RTSP 认证；不修改操作系统账户。

### 8.3 事件容量和保留

用户可以设置：

- `retention_days`：默认 7 天，可选 3 / 7 / 30 天。
- `max_event_storage_mb`：默认 2048 MB；建议硬上限 8192 MB，避免 32 GB TF 卡被事件占满。
- JPEG 质量与截图尺寸目前由服务端实现控制，尚未作为完整用户设置公开。

清理 worker 每 5 分钟运行：

1. 删除超过保留天数的事件文件和数据库记录。
2. 如果事件目录超过容量，从最旧事件开始删除，直到低于目标水位（例如 90%）。
3. 数据库清理与文件删除保持一致；周期性压缩数据库可作为后续维护策略，不视为当前已有能力。
4. 文件写入采用临时文件 + `fsync` + 原子重命名，避免掉电产生半张图片。

TF 卡空间有限，不保存连续视频；事件截图采用 JPEG，并通过保留时间与容量限制总写入量。事件时间保留 UTC/epoch，页面按系统设置中的 IANA 时区显示，默认 `Asia/Shanghai`；切换显示时区不重写历史记录。

## 9. Web 应用与前端

### 9.1 技术栈

当前实现与目标技术栈需要区分：

| 层次 | 当前实现 | 后续目标 |
|---|---|---|
| 后端 | Python 3.10、FastAPI、Uvicorn、Pydantic v2 | 保持 |
| 前端 | FastAPI 内嵌中文 HTML/CSS/JavaScript | 功能稳定后可拆分 React/Vite |
| 媒体 | RDK 原生采集/硬编码、共享内存、MediaMTX；FFmpeg 音频解码 | 增强异构摄像头兼容性 |
| 浏览器视频 | aiortc H.264 + Opus WebRTC，Canvas 元数据叠加 | 优化负载和异常恢复 |
| 推理 | 独立进程 hbm_runtime YOLO11n/YOLO11m、Python 后处理 | 专用模型和调度增强 |
| 数据库 | SQLite + WAL，JSON 配置列 | 长期运行继续保持，必要时迁移结构化表 |
| ONVIF | 输入 onvif-zeep；输出 Device/Media/Analytics/Events、WS-Discovery | 扩展兼容性；未声明 ONVIF 认证 |
| Home Assistant | 原生 ONVIF + 配套规则元数据同步组件 | 更多 HA 版本兼容与回归 |
| MQTT | paho-mqtt 旧部署兼容路径，默认关闭 | 不作为新接入方式 |

### 9.2 当前页面

当前页面为中文桌面端科技风格，六个 Tab 为：

1. 画面预览：WebRTC 多路网格、Canvas 检测框、分辨率/帧率、音频等级与独立静音。
2. 设备管理：添加 RTSP/ONVIF、拖动排序、设备状态、重连、AI 类别分组全选、编辑和删除。
3. 规则配置：全局规则列表、新增/编辑弹窗、按摄像头算法提供类别与默认阈值、矩形/多边形区域。
4. 事件记录：筛选、截图查看和 JSON/CSV 导出。
5. ONVIF 联动：网关地址、订阅、规则事件和 RTSP 输出状态。
6. 系统设置：资源状态、预览分辨率、视频软件回退开关、事件保留时间、显示时区及账户。

ONVIF 添加使用独立进度窗口，显示扫描、验证凭据、读取 Profile、保存和连接等阶段，提供已用时间和明确错误原因；关闭窗口可结束当前操作。设备名称来自 WS-Discovery Scopes，设备 IP 单独显示。

### 9.3 当前 API 与后续 API

当前已实现的主要接口：

```text
GET    /healthz
POST   /api/auth/login
POST   /api/auth/logout

GET    /api/system/status
GET    /api/cameras
POST   /api/cameras
PATCH  /api/cameras/{id}
PATCH  /api/cameras/{id}/labels
POST   /api/cameras/{id}/reconnect
DELETE /api/cameras/{id}

POST   /api/onvif/discover
POST   /api/onvif/probe
GET    /api/cameras/{id}/preview.jpg
GET    /api/cameras/{id}/mjpeg
POST   /api/cameras/{id}/webrtc/offer

GET    /api/detection-labels
GET    /api/rules
GET    /api/cameras/{id}/rule-options
GET    /api/cameras/{id}/rules
POST   /api/cameras/{id}/rules
PATCH  /api/rules/{id}
DELETE /api/rules/{id}

GET    /api/events
GET    /api/events/{id}/image
GET    /api/events/export

GET    /api/settings
PATCH  /api/settings
POST   /api/settings/mqtt/test           # 旧部署兼容接口
GET    /api/settings/export

POST   /onvif/device_service
POST   /onvif/media_service
POST   /onvif/analytics_service
POST   /onvif/events_service
POST   /onvif/events/subscriptions/{token}
GET    /onvif/snapshots/{id}.jpg
```

`/ws/preview` 仍保留用于状态兼容，主视频预览使用 WebRTC。规则 PATCH 合并已保存字段，改名不重置触发条件；全局规则列表新建优先且编辑不改变创建顺序。`/api/logs`、`/readyz` 等运维接口属于后续增强。

## 10. Home Assistant 集成

### 10.1 原生 ONVIF 视频与事件

AIX5 作为名为 `Edge-AI-IPC` 的多通道 ONVIF 网关。WS-Discovery 使用持久化设备 UUID；Home Assistant 添加一次 ONVIF 后读取多个 Media Profile，每路获得网关自身的认证 RTSP 地址。

- Device：设备信息、服务能力、时钟、Scopes。
- Media：视频源、H.264 Profile、音频配置、Stream URI、Snapshot URI。
- Events：PullPoint 建立、拉取、续期、同步、取消；未支持的操作返回明确 SOAP Fault。
- Analytics：`GetRules` 提供完整规则目录，包含规则 UUID、显示名、摄像头、启用状态和目标类别。
- 媒体：共享 H.264 编码通过 MediaMTX 输出；有音频时使用 AAC，使 HA Stream 能播放。浏览器 WebRTC 的 Opus 与该分支独立，不将源音频永久改成 Opus。

ONVIF 事件只接收 RuleEngine 正式触发出口：普通检测框不直接上报告警。每条规则一个稳定传感器，类别聚合为 OR，状态 true/false 与配置、冷却、停用、删除和断线处理保持一致。初始化和复位属于状态同步，不算新业务告警。没有规则时不发送业务 true。

### 10.2 规则名称与删除同步组件

HA 原生 FieldDetector 解析器使用固定名称，且不会根据 ONVIF `Deleted` 自动移除注册实体。仓库提供独立的 [`edge_ai_onvif` 配套组件](home_assistant/README.md)：复用已添加 ONVIF 条目的凭据，通过标准 Analytics 读取完整目录，仅使用 HA 公开注册表 API 同步名称和删除。

视频、传感器创建和告警状态仍由 HA 原生 ONVIF 负责；配套组件不替换 HA 核心、不重复创建传感器、不需要 MQTT 或额外 HA API Token。改名保留 `entity_id`，避免破坏自动化；清理只针对所选网关的规则身份，完整目录读取失败时保留现有实体。

旧版“规则 + 目标类别”到“每规则一个实体”的迁移需要在原生条目暂停窗口进行。一对一迁移保留实体 ID；存在多个旧实体引用同一规则时先提示检查，不静默删除自动化使用的身份。

### 10.3 已知边界

当前实现针对 HA 需要的设备、媒体和事件接口提供兼容性，不等价于 ONVIF Profile S/T 认证。原生 HA 不会自动展示所有扩展字段或本地截图，设备触发器、逐摄像头子设备等能力仍需另行设计。不同 HA 版本、编解码组合和长期负载需要持续回归。

## 11. 数据库设计

### 11.1 当前 SQLite 结构

当前 SQLite 位于 `AIX5_DATA_DIR/app.db`，开启 WAL、外键和 busy timeout。实际表为：

- `kv`：设备 ID、设置、管理员凭据哈希、会话相关值。
- `sources`：摄像头配置 JSON，包括名称、RTSP URL、传输方式、模型和类别；运行时状态不持久化。
- `rules`：按 `camera_id` 关联的规则 JSON。
- `detections`：事件 payload、截图路径、大小和创建时间。
- `sessions`：管理员会话 token 的 SHA-256 哈希和过期时间。
- `onvif_nonces`：已接受的 Digest Nonce 指纹与过期时间，用于拒绝重放。

运行时帧、BGR、JPEG 和计数器不写入 SQLite；事件截图写入 `AIX5_DATA_DIR/events`。

### 11.2 当前保留策略

retention worker 每 300 秒执行一次：先删除超过配置保留天数的事件，再按最大事件容量从旧到新删除，直到回到容量水位的 90%。默认容量 2048 MB。

## 12. 监控、日志和 24 小时运行

### 12.1 当前运行指标

`/api/system/status` 当前提供：

- CPU、内存、磁盘；
- BPU `ratio` 字段（驱动调度值，不直接等于百分比）；
- WebRTC 活跃观看者数量；
- 编码模式 `H264 on demand`；
- 每路摄像头的输入宽高、解码后端、解码帧数、预览帧数、检测数、推理延迟和最后错误。

GPU busy 百分比当前固件没有暴露可靠 sysfs 指标，因此页面不应把缺失值伪装成 GPU 利用率。

### 12.2 当前自动恢复

- systemd 负责主进程异常退出自动重启。
- 每路采集 worker 独立指数退避。
- 硬件解码/编码启动与短暂失败进行有限重试；仅允许时软件回退。
- WebRTC connection state 失败时清理 peer。
- 浏览器切换 Tab 不关闭已建立的 WebRTC；关闭页面才释放观看者。

### 12.3 后续增强

仍需补充：结构化日志查询、`/readyz`、BPU 每核利用率、GPU/VPU 明确指标、worker 心跳和故障注入测试。

## 13. 部署方案

### 13.1 当前部署

当前采用 systemd 原生部署，不使用 Docker 运行 AI 服务。以下路径为示例，实际服务文件和环境变量以 README 为准：

```text
应用代码：/opt/aix5-ai/current
虚拟环境：/opt/aix5-ai/venv
数据目录：/var/lib/aix5-ai
服务：aix5-ai.service
监听：http://0.0.0.0:8080
采集器：current/native/rdk_capture_mm
```

systemd 关键行为：

- User：`<service-user>`；
- `Restart=always`；
- `RestartSec=5`；
- `LimitNOFILE=65536`；
- 通过 `AIX5_DATA_DIR`、`AIX5_CAPTURE_BIN` 注入运行目录。

### 13.2 目标部署增强

继续完善可重复安装、版本回滚与配对备份；数据库和 ONVIF 密钥需一起保管。RDK 媒体/BPU 依赖仍建议原生 systemd 运行，避免在未验证容器驱动条件下直接迁移。

## 14. 分阶段实施计划

### 阶段 0～1：已完成基线

- RDK X5 硬解码和 BPU YOLO11n 已跑通。
- stride 适配、共享内存有界环形缓冲已完成。
- Web 登录、RTSP 添加、事件截图、规则基础能力和 systemd 部署已完成。

### 阶段 2：已完成主要能力

- 2～4 路摄像头配置和多路预览页面。
- ONVIF WS-Discovery、账号认证、主码流/子码流 Profile 添加。
- 指数退避、手动重连、硬件初始化重试、显式软件回退开关。
- WebRTC/H.264 网格和全屏预览。
- 检测框/目标名称通过 DataChannel 发送，前端 Canvas 外部叠加。
- 规则、区域绘制、事件记录、容量与 3 / 7 / 30 天保留。

### 阶段 2 收尾与已知限制

- ONVIF 设备名称/IP 展示、多通道 Profile 和“添加”交互已经落地。
- 仍需增加完整日志查看和更准确的硬件利用率采集。
- 高 Profile/高分辨率码流可能不被 VPU 支持；关闭回退时明确报错，开启后评估软件回退负载。

### 阶段 3：ONVIF 与 Home Assistant 已实现

- 多通道 ONVIF 网关、H.264/AAC RTSP 输出及严格账户认证。
- 基于正式规则的 PullPoint 状态同步，每条规则一个实体。
- 配套规则名称/删除同步组件及保留实体 ID 的升级路径。
- HA 视频、规则事件、改名与删除的联调；更多 HA 版本和故障场景持续回归。

### 阶段 4：长期运行增强

- 四路压力测试和故障注入。
- 配置/数据库备份与回滚。
- HTTPS、RTSP 传输保护、摄像头凭据保护和更细粒度权限评估。

## 15. 验收标准

### 当前已验证功能

- 能发现局域网 ONVIF 摄像头，并显示设备名称、IP、主码流/子码流 Profile。
- 能手动添加包含用户名和密码的 RTSP URL。
- H.264、H.265 已有实机输入验证；正常路由优先 RDK VPU 硬解。
- 摄像头断线可指数退避重连，Web 页面显示状态并提供重连按钮。
- 浏览器支持 WebRTC/H.264 单路和多路网格查看。
- BPU 使用 YOLO11n 完成目标检测，结果包含类别、置信度和框。
- 检测框、中文目标名称和置信度由前端 Canvas 外部叠加。
- 矩形/多边形区域规则可配置，冷却时间和连续命中规则有效。
- 事件记录和截图可查看、导出，并按容量和选定的 3 / 7 / 30 天策略清理。

### 集成验收与后续目标

当前需要保持回归的能力：ONVIF 发现和认证、各路 H.264/AAC 视频、正式规则 true/false、规则增删改名以及重启恢复。设备触发器、更多事件扩展字段、逐摄像头子设备和所有 HA 版本的兼容性不作为已交付承诺。

### 稳定性

- 当前已进行多路持续运行和断线恢复验证；完整 24 小时压力验收仍需在阶段 2 收尾执行。
- 目标是任一路断开/恢复至少 20 次不造成进程泄漏。
- 事件目录不超过配置容量；磁盘不足时服务仍保持可用并告警。
- 主服务崩溃后 systemd 自动恢复。

### 安全

- 未登录不能访问配置、设备、规则和事件接口。
- 日志避免完整 RTSP 凭据，导出脱敏；源 RTSP 凭据不外泄给网关媒体消费者。
- ONVIF 与 RTSP 请求必须使用有效凭据；Digest 错误密码、过期时间和 Nonce 重放应被拒绝。
- 默认 HTTP 监听 `0.0.0.0`，部署者需通过网络边界限定局域网访问；仅选择监听地址并不自动提供防火墙保护。

## 16. 主要风险和处理方式

| 风险 | 影响 | 处理 |
|---|---|---|
| 烟火/烟雾模型不在当前基础包 | 第一版无法提供该类别可靠检测 | 启动时检查专用模型；先完成通用目标链路，补充官方兼容模型后启用 |
| H.265 浏览器兼容性不一致 | 直接 video 播放失败 | 板端 VPU 解码后统一 H.264；浏览器 WebRTC 与 RTSP 共用输出 |
| 摄像头码流和 RDK Decoder 兼容性差异 | 个别厂家无法解码 | 硬件重试与显式错误；软件回退须由用户开启 |
| 4 路总负载超过 BPU/内存 | 延迟、丢帧 | 分析 FPS 限制、最新帧策略、动态降级、性能门槛 |
| TF 卡写放大和空间不足 | 文件系统损坏/服务停止 | 事件 JPEG 限额、原子写、容量水位、日志轮转、建议预留至少 8 GiB |
| HA 原生 ONVIF 不同步规则名称/删除 | 固定名称或残留实体 | 独立元数据同步组件读取权威规则目录，保留稳定实体 ID |
| 无 Swap 且模型/多路缓存突增 | OOM | 限制队列和 JPEG 缓存，监测内存，必要时加入小型 zram/swap 作为系统策略评估项 |

## 17. 当前建议采用的默认值

```yaml
server:
  host: 0.0.0.0
  port: 8080
  network_scope: lan  # 部署要求；由防火墙/网络配置落实

video:
  max_cameras: 4
  transport: tcp
  reconnect_backoff_max_seconds: 60
  preview_transport: webrtc
  preview_codec: h264
  preview_resolution: 960x540  # 可选 1920x1080；不超过源尺寸
  playback_fps: source
  allow_software_fallback: false
  jpeg_quality: 80

inference:
  model: yolo11n
  input_size: 640x640
  input_format: nv12
  analysis_fps: 5
  confidence_threshold: 0.35
  bpu_priority: 128
  max_concurrent_inference: 1

retention:
  days: 7
  max_event_storage_mb: 2048
  high_watermark_percent: 90

display:
  timezone: Asia/Shanghai

home_assistant:
  integration: onvif
  http_port: 8080
  rtsp_port: 8554
  video_codec: h264
  audio_codec: aac
  rule_metadata_companion: edge_ai_onvif
```

以上 YAML 用于说明概念，实际配置入口和环境变量以 README / API Schema 为准。`analysis_fps` 控制推理上限；`preview_fps` 为兼容旧配置保留，不用它把 WebRTC 播放限制成 5 FPS。

## 18. 架构维护原则

当前工程以 FastAPI 控制面、原生 VPU 媒体管线、独立 BPU 推理、SQLite/事件 JPEG 以及 ONVIF/RTSP 输出为基础。硬件与软件路径、分析与播放频率、业务告警与状态同步需要保持明确边界。

后续改动应先验证硬件和协议行为，再更新公开能力说明。README 记录当前安装和功能契约；本文件记录架构及其设计理由，不保存个人部署凭据、生产设备清单或阶段测试附件。
