# RDK X5 安装与运维

本指南用于从源码部署 AIX5。所有应用运行、硬件编译和 systemd 命令均在 **RDK X5** 上执行；普通开发机的测试方法见 [README](../README.md)。示例中的 `<开发板IP>` 和 `<服务用户>` 请替换为实际值。

## 1. 准备环境

参考环境为 RDK OS 3.5.0 / Ubuntu 22.04.5、ARM64、Python 3.10。系统需要已经具备 RDK 多媒体驱动、`hbm_runtime` 与相匹配的 BPU 模型。

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential python3-dev python3-venv pkg-config curl \
  ffmpeg libavformat-dev libavcodec-dev libavutil-dev libswscale-dev
```

RDK 的 `hb_media_codec.h`、`hb_media_error.h`、`libmultimedia`、`hbm_runtime` 来自 RDK OS/SDK，不是上述通用 Ubuntu 软件包提供的。不要用普通 x86 的库或模型替换板端组件。

将仓库放在服务用户可访问的目录，例如 `/opt/aix5-ai` 或自己的主目录。安装脚本不支持路径中的空格和非 ASCII 字符；后续命令在项目根目录执行。

## 2. 编译原生媒体程序

默认硬件解码路径：

```bash
gcc -O2 -std=c11 -I/usr/include -L/usr/hobot/lib \
  -Wl,-rpath,/usr/hobot/lib \
  -o native/rdk_capture_mm native/rdk_capture_mm.c \
  -lmultimedia -lavformat -lavcodec -lavutil -lpthread
```

硬件 H.264 编码库与共享编码进程：

```bash
gcc -O2 -shared -fPIC -I/usr/include -L/usr/hobot/lib \
  -Wl,-rpath,/usr/hobot/lib \
  -o native/librdk_h264_encoder.so native/rdk_h264_encoder.c \
  -lmultimedia

gcc -O2 -DAIX5_STREAM_MAIN -I/usr/include -L/usr/hobot/lib \
  -Wl,-rpath,/usr/hobot/lib \
  -o native/rdk_video_stream native/rdk_h264_encoder.c \
  -lmultimedia -lswscale -lavutil -lpthread
```

部分摄像机的 `/stream/main` 兼容路径会使用 legacy 采集器。系统安装了 `sp_codec.h` 与 `libspcdev` 时，也应编译：

```bash
gcc -O2 -std=c11 -I/usr/include -I/usr/include/spcdev -L/usr/hobot/lib \
  -Wl,-rpath,/usr/hobot/lib \
  -o native/rdk_capture native/rdk_capture.c \
  -lspcdev -lavformat -lavcodec -lavutil -lpthread
```

头文件和动态库路径以实际 RDK SDK 为准；若 legacy SDK 不在示例路径，按系统安装位置调整 `-I` / `-L`。生成的可执行文件、共享库和模型均由 `.gitignore` 排除。

## 3. Python 环境、模型与 RTSP 服务

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
python -m pip install -e .
bash deploy/install-mediamtx.sh
```

`--system-site-packages` 用于访问 RDK OS 中的硬件 Python 运行时。MediaMTX 安装脚本下载固定的 **v1.21.2 Linux ARM64** 包并验证 SHA-256，安装到 `native/mediamtx`；应用负责管理它，不需要另建 MediaMTX 服务。

当前模型路径：

| 模型 | 默认位置 |
|---|---|
| YOLO11n | `/app/pydev_demo/02_detection_sample/02_ultralytics_yolo11/yolo11n_detect_bayese_640x640_nv12.bin` |
| YOLO11m | `/opt/hobot/model/x5/basic/yolo11m_detect_bayese_640x640_nv12_modified.bin` |

YOLO11n 可通过 `AIX5_MODEL_PATH` 指定另一份兼容模型。模型必须与输入尺寸、量化输出和本项目后处理对应；仅替换文件名不能自动适配其他模型。仓库不包含模型权重或 RDK SDK。

## 4. 首次运行

```bash
export AIX5_DATA_DIR="$PWD/data"
export AIX5_BASE_URL="http://<开发板IP>:8080"
aix5-ai
```

另一个终端读取初始账号：

```bash
cat ./data/initial-admin.txt
```

在浏览器访问 `http://<开发板IP>:8080`。用户名为 `admin`，密码为文件中的随机值。登录后可在系统设置修改用户名和密码；已有安装升级时不会重置账号。

`AIX5_BASE_URL` 必须是其他设备可访问的地址，才能将网关媒体和事件提供给 HA。未设置时默认 `http://127.0.0.1:8080`，适合本机启动检查。环境变量只影响新数据目录的默认设置；已有数据目录使用数据库中保存的地址。

### 已有安装更换 IP

地址变更通过已登录会话更新设置 API，不需要重建数据库。以下示例在开发板本机运行，密码交互输入，Cookie 只在进程内存中保存：

```bash
venv/bin/python -c '
import getpass
import httpx

base = input("当前 Web 地址，例如 http://127.0.0.1:8080：").strip()
username = input("AIX5 用户名：").strip()
password = getpass.getpass("AIX5 密码：")
new_base = input("新的局域网地址，例如 http://<开发板IP>:8080：").strip()
with httpx.Client(base_url=base, timeout=15) as client:
    client.post("/api/auth/login", json={"username": username, "password": password}).raise_for_status()
    try:
        client.patch("/api/settings", json={"base_url": new_base}).raise_for_status()
        print("地址已更新")
    finally:
        client.post("/api/auth/logout")
'
```

如果先用 `./data` 试运行，再安装 systemd，请为服务指定同一数据目录，或在停机状态下迁移数据。使用空的新目录会生成新的账号与设备身份。

## 5. systemd 开机自启

安装脚本会检查代码、虚拟环境和必需的原生文件，然后渲染服务模板。它不会编译、安装 Python 依赖或下载模型。

```bash
# 仅查看生成的服务配置，不写文件、不操作 systemd
AIX5_BASE_URL="http://<开发板IP>:8080" bash deploy/install.sh --dry-run

# 使用当前非 root 用户作为服务用户
sudo env \
  AIX5_SERVICE_USER="$USER" \
  AIX5_BASE_URL="http://<开发板IP>:8080" \
  bash deploy/install.sh
```

脚本默认将数据写到 `/var/lib/aix5-ai`。若要沿用上一步的试运行数据，将两条命令都加上 `AIX5_DATA_DIR="$PWD/data"`。

| 安装参数 | 默认值 |
|---|---|
| `AIX5_SERVICE_USER` | `SUDO_USER`，否则当前非 root 用户；账号须已存在并有硬件访问权限 |
| `AIX5_APP_DIR` | 安装脚本所在仓库 |
| `AIX5_VENV_DIR` | `<应用目录>/venv` |
| `AIX5_DATA_DIR` | `/var/lib/aix5-ai` |
| `AIX5_PORT` | `8080` |
| `AIX5_BASE_URL` | `http://127.0.0.1:<端口>`；部署时设为可达的局域网地址 |

`deploy/aix5-ai.service` 是占位符模板，应由脚本渲染后安装。不要直接把未渲染模板复制给 systemd。安装器只设置数据目录自身的权限和属主，不递归改写已有文件所有权。

```bash
systemctl status aix5-ai
journalctl -u aix5-ai -f
sudo systemctl restart aix5-ai
```

升级代码或重新安装服务模板后，执行 `sudo systemctl restart aix5-ai` 让新版本生效。运行实例仍使用原持久数据。

## 网络与应用配置

| 端口 / 协议 | 用途 |
|---|---|
| `8080/TCP` | Web、设置 API、ONVIF SOAP |
| `8554/TCP` | MediaMTX RTSP 输出 |
| `3702/UDP` | ONVIF WS-Discovery 发现 |
| WebRTC 协商的 UDP 端口 | 浏览器实时音视频，要求局域网可达 |

ONVIF 服务路径包括 `device_service`、`media_service`、`analytics_service`、`events_service`，统一位于 `/onvif/` 下。HA 添加时填写 HTTP 端口，不填 RTSP 端口。

| 应用环境变量 | 默认值 / 作用 |
|---|---|
| `AIX5_DATA_DIR` | `./data`；应用直接运行时的数据目录 |
| `AIX5_HOST` | `0.0.0.0`；CLI 监听地址 |
| `AIX5_PORT` | `8080`；CLI 端口及初始 URL 端口 |
| `AIX5_BASE_URL` | `http://127.0.0.1:<端口>`；首次初始化地址 |
| `AIX5_CAPTURE_BIN` | `native/rdk_capture_mm` |
| `AIX5_LEGACY_CAPTURE_BIN` | `native/rdk_capture` |
| `AIX5_ENCODER_LIB` | `native/librdk_h264_encoder.so` |
| `AIX5_MEDIAMTX_BIN` | `native/mediamtx` |
| `AIX5_MODEL_PATH` | 默认 YOLO11n 模型路径 |

安装器生成的 unit 显式设置自身支持的路径和地址变量；其他模型或运行时变量需要写入 systemd override 才会传给服务。例如：

```bash
sudo systemctl edit aix5-ai
```

填写：

```ini
[Service]
Environment=AIX5_MODEL_PATH=/path/to/compatible-yolo11n.bin
```

然后重启服务。

## 备份与恢复

为了得到一致的数据库和截图集合，先停止应用，再复制整个数据目录：

```bash
sudo systemctl stop aix5-ai
sudo tar -czf /path/to/private-backup/aix5-data.tgz -C /var/lib/aix5-ai .
sudo systemctl start aix5-ai
```

若使用自定义数据目录，请替换命令中的路径，并提前创建备份目录。备份包含摄像机配置和账号信息，应放在私有位置，不能加入 Git 仓库。

恢复时保持数据库、事件目录与 `.onvif-secret.key` 配套，并确认服务用户可以访问。仅恢复数据库但丢失密钥时，严格 ONVIF Digest 不会接受未验证的摘要；成功网页登录可重新初始化当前凭据。`mediamtx.json` 是运行时生成文件，应用会按保存的账号重新生成。

## 验证与排错

```bash
# 普通开发机或板端：回归测试
python -m pip install -e '.[dev]'
python -m pytest -q

# 板端：纯合成输入，不读取摄像机图像
python tools/verify_native_stream.py
python tools/verify_native_backpressure.py
python tools/verify_native_backpressure.py --shutdown-while-blocked
```

其他可复用工具：

- `tools/verify_onvif_rule_events.py`：独立临时 ONVIF + RuleEngine 网关；先阅读脚本帮助，使用临时测试账号，不操作生产数据库。
- `tools/verify_ha_rule_sync.py`：在 HA 的 Python 环境中验证注册表兼容性，说明见 [HA 组件文档](../home_assistant/README.md)。

常见问题：

| 现象 | 排查方向 |
|---|---|
| 找不到 `hbm_runtime` | 核对 RDK OS 镜像、虚拟环境的 `--system-site-packages` 和模型安装 |
| 硬件编解码不可用 | 查看日志中的 native 阶段和错误码，核对输入 Profile、SDK 与驱动；系统设置可显式选择软件回退 |
| HA 能添加设备但不能播放 | 核对保存的 `base_url`、`8554/TCP` 可达性和 MediaMTX 状态 |
| HA 传感器仍叫 Field Detection | 安装并关联 Edge AI ONVIF Rule Sync，详见 HA 组件说明 |
| 升级后 ONVIF 认证失败 | 先用有效应用账号登录网页，检查两端时钟和数据库/密钥配对 |
| 多路预览掉帧 | 同时检查输入 FPS、编码 FPS、网页接收帧率和 CPU；降低输入负载或观看连接数 |
