from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, Field, field_validator, model_validator
from .labels import COCO_LABELS
from .config import DEFAULT_DETECTION_CONFIDENCE, DEFAULT_SETTINGS


def validate_labels(values):
    labels = [x.strip().lower() for x in values if x.strip()]
    if not labels or len(labels) != len(set(labels)):
        raise ValueError("至少选择一个不重复的检测类别")
    if any(label not in COCO_LABELS for label in labels):
        raise ValueError("包含不支持的检测类别")
    return labels


class Login(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class AccountChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    username: str | None = Field(default=None, min_length=1, max_length=80)
    new_password: str | None = Field(default=None, min_length=8, max_length=256)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value):
        if value is not None:
            value = value.strip()
            if not value or ":" in value or any(ord(char) < 32 for char in value):
                raise ValueError("请输入有效的用户名，不能包含冒号或控制字符")
        return value

    @model_validator(mode="after")
    def requires_change(self):
        if self.username is None and self.new_password is None:
            raise ValueError("请填写新用户名或新密码")
        return self


class CameraConfig(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    rtsp_url: str = Field(min_length=1, max_length=2048)
    transport: Literal["tcp", "udp"] = "tcp"
    enabled: bool = True
    model_id: Literal["yolo11n", "yolo11m"] = "yolo11n"
    analysis_fps: float = Field(default=5, ge=1, le=10)
    preview_fps: float = Field(default=5, ge=1, le=10)
    enabled_labels: list[str] = Field(
        default=["person", "cat", "dog"], min_length=1, max_length=80
    )

    @field_validator("rtsp_url")
    @classmethod
    def valid_url(cls, v):
        u = urlsplit(v)
        if (
            u.scheme not in ("rtsp", "rtsps")
            or not u.hostname
            or any(c in v for c in "\n\r\0")
        ):
            raise ValueError("请输入有效的 RTSP 地址")
        if u.port is not None and not 1 <= u.port <= 65535:
            raise ValueError("RTSP 端口无效")
        return v

    @field_validator("enabled_labels")
    @classmethod
    def valid_enabled_labels(cls, v):
        return validate_labels(v)


class CameraLabels(BaseModel):
    enabled_labels: list[str] = Field(min_length=1, max_length=80)

    @field_validator("enabled_labels")
    @classmethod
    def valid_labels(cls, v):
        return validate_labels(v)


class CameraOrder(BaseModel):
    camera_ids: list[str] = Field(max_length=4)

    @field_validator("camera_ids")
    @classmethod
    def unique_ids(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("设备顺序中不能包含重复设备")
        return values


class CameraPatch(BaseModel):
    """PATCH fields are validated after merging with the saved configuration."""

    name: str | None = None
    rtsp_url: str | None = None
    transport: Literal["tcp", "udp"] | None = None
    enabled: bool | None = None
    model_id: Literal["yolo11n", "yolo11m"] | None = None
    analysis_fps: float | None = None
    preview_fps: float | None = None
    enabled_labels: list[str] | None = None

    @model_validator(mode="after")
    def no_null_fields(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("配置字段不能为 null")
        return self


class RuleConfig(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    target_labels: list[str] = Field(default=["person"], min_length=1, max_length=80)
    confidence_threshold: float = Field(default=DEFAULT_DETECTION_CONFIDENCE, ge=0.1, le=0.99)
    cooldown_seconds: float = Field(default=60, ge=1, le=86400)
    min_hits: int = Field(default=2, ge=1, le=10)
    clear_seconds: float = Field(default=3, ge=1, le=60)
    zone: list[tuple[float, float]] = Field(default=[], max_length=32)

    @field_validator("zone")
    @classmethod
    def valid_zone(cls, v):
        if v and len(v) < 3:
            raise ValueError("区域至少需要 3 个顶点")
        if any(not 0 <= a <= 1 for p in v for a in p):
            raise ValueError("区域坐标必须在 0～1 之间")
        if v:
            area = (
                abs(
                    sum(
                        v[i][0] * v[(i + 1) % len(v)][1]
                        - v[(i + 1) % len(v)][0] * v[i][1]
                        for i in range(len(v))
                    )
                )
                / 2
            )
            if area < 0.0001:
                raise ValueError("区域面积过小")
        return v

    @field_validator("target_labels")
    @classmethod
    def valid_target_labels(cls, v):
        return validate_labels(v)


class RulePatch(BaseModel):
    """Only supplied fields change; merged rules use RuleConfig validation."""

    name: str | None = None
    enabled: bool | None = None
    target_labels: list[str] | None = None
    confidence_threshold: float | None = None
    cooldown_seconds: float | None = None
    min_hits: int | None = None
    clear_seconds: float | None = None
    zone: list[tuple[float, float]] | None = None

    @model_validator(mode="after")
    def no_null_fields(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("规则字段不能为 null")
        return self


class MQTTConfig(BaseModel):
    enabled: bool = False
    host: str = Field(default="", max_length=253)
    port: int = Field(default=1883, ge=1, le=65535)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)
    tls: bool = False

    @model_validator(mode="after")
    def enabled_host(self):
        self.host = self.host.strip()
        if self.enabled and not self.host:
            raise ValueError("启用 MQTT 时必须填写 Broker 地址")
        return self


class SettingsConfig(BaseModel):
    timezone: str = Field(default=DEFAULT_SETTINGS["timezone"], min_length=1, max_length=64)
    allow_software_fallback: bool = False
    preview_resolution: Literal["1920x1080", "960x540"] = "960x540"
    retention_days: Literal[3, 7, 30] = 7
    max_event_storage_mb: int = Field(default=2048, ge=16, le=8192)
    base_url: str = Field(max_length=255)
    mqtt: MQTTConfig

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("请输入有效的 IANA 时区，例如 Asia/Shanghai") from exc
        return value

    @field_validator("base_url")
    @classmethod
    def valid_base(cls, v):
        u = urlsplit(v)
        if (
            u.scheme not in ("http", "https")
            or not u.hostname
            or u.username
            or u.query
            or u.fragment
        ):
            raise ValueError("请输入开发板 HTTP/HTTPS 地址")
        return v.rstrip("/")


class SettingsPatch(BaseModel):
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    allow_software_fallback: bool | None = None
    preview_resolution: Literal["1920x1080", "960x540"] | None = None
    retention_days: Literal[3, 7, 30] | None = None
    max_event_storage_mb: int | None = Field(default=None, ge=16, le=8192)
    base_url: str | None = Field(default=None, max_length=255)
    mqtt: MQTTConfig | None = None

    @model_validator(mode="after")
    def no_null_fields(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("配置字段不能为 null")
        return self


class ONVIFProbe(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)
