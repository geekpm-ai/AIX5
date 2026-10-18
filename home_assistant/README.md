# Edge AI ONVIF 规则同步

这个自定义集成补充 Home Assistant 原生 ONVIF 集成的规则名称与删除同步。视频、音频、传感器创建和告警状态仍由原生 ONVIF 处理；无需 MQTT、HA API Token 或重复填写设备密码。

它通过 ONVIF Media `GetVideoAnalyticsConfigurations` 和 Analytics `GetRules` 读取完整规则目录，复用所选原生 ONVIF 配置条目的设备账号。每条界面规则对应一个传感器，规则内不同目标类别由 AIX5 聚合。传感器显示名与 AIX5 中的规则名称一致，改名保持原实体 ID，已有自动化仍引用同一个实体。

## 安装

1. 把 `custom_components/edge_ai_onvif` 复制到 HA 配置目录的 `custom_components/edge_ai_onvif`，重启 Home Assistant。
2. 先通过原生 ONVIF 集成添加 `Edge-AI-IPC`。
3. 添加“Edge AI ONVIF Rule Sync”，选择已有的 ONVIF 网关。安装程序也可以通过配置流 `import` 一次性建立关联，数据为 `{"onvif_entry_id":"原生 ONVIF 配置条目 ID"}`。

选择后自动工作，无需为每条规则手动设置名称。规则目录每 2 秒读取一次；新增传感器会立即请求一次目录。删除需要连续两次完整目录确认，通常数秒内完成，本次实测约 2–5 秒。网络失败、认证错误或目录不完整时保留现有实体；停用原生 ONVIF 集成后暂停查询。

## 已有部署迁移

旧版本按“规则 + 目标类别”建立身份，新版本按“规则”建立身份。一个规则只有一个旧传感器时，会保留原 `entity_id`、区域、标签等注册信息，仅迁移内部唯一 ID。

升级时先暂时停用对应的原生 ONVIF 条目，再部署新版 AIX5 与本集成，建立规则同步条目完成身份迁移后，恢复原生 ONVIF。同步集成支持在这个停用窗口内迁移，且不会自行启用被停用的条目。

如果一条规则已经存在多个旧传感器，或新旧身份同时存在，自动迁移会保留这些实体并显示修复提示，避免静默破坏引用不同实体 ID 的自动化。需要先确定保留的实体；完成后可重载同步条目继续。

删除 AIX5 规则会删除其原生 ONVIF 传感器。同步范围严格限定为所选 ONVIF 配置条目、对应设备身份及 AIX5 的规则事件主题；其他摄像头、按钮、传感器和集成不会被清理。

## 验证

已在 HA Core 2026.10.0、onvif-zeep-async 4.3.0、onvif-parsers 2.5.0 环境验证公共 EntityRegistry 接口及隔离迁移流程。规则目录解析和身份迁移测试位于 `tests/test_ha_rule_sync.py`。

在 HA Python 环境内可运行 `tools/verify_ha_rule_sync.py --package-root /path/to/home_assistant`。该工具创建临时 Home Assistant 对象和空注册表，不连接网关，不改动正在运行的 HA 配置。
