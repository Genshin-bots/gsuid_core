"""HTTP Agent API 配置。``data/ai_core/http_agent_api.json``，按 mtime 热读。"""

from __future__ import annotations

import json
from typing import Dict, List
from dataclasses import dataclass

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import GSC, GsIntConfig, GsStrConfig, GsBoolConfig, GsListStrConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

DEFAULT_MAX_CONCURRENT = 8
DEFAULT_PER_KEY_CONCURRENT = 2
DEFAULT_RPM = 30
DEFAULT_MAX_BODY_BYTES = 2_097_152
DEFAULT_MAX_IMAGES = 8
DEFAULT_WALL_CLOCK = 600
DEFAULT_HARD_TIMEOUT = 660
DEFAULT_HEARTBEAT_SEC = 15
DEFAULT_QUEUE_MAX = 256
DEFAULT_IDEMPOTENCY_TTL = 600
DEFAULT_IDEMPOTENCY_CAP = 4096
DEFAULT_AUTH_FAIL_MAX = 10
DEFAULT_AUTH_BAN_SEC = 900
ATTACHMENT_FRAME_MAX = 256 * 1024
ATTACHMENT_RUN_MAX = 2 * 1024 * 1024

HTTP_AGENT_API_CONFIG: Dict[str, GSC] = {
    "enable_http_agent_api": GsBoolConfig(
        "启用 HTTP Agent API",
        "关闭时 Agent 面 404；还须 AI 总开关开启才会挂路由。Admin 建钥不受此开关影响",
        False,
    ),
    "http_agent_max_concurrent": GsIntConfig(
        "全局并发槽",
        "本面全局同时进行的 run 上限",
        DEFAULT_MAX_CONCURRENT,
    ),
    "http_agent_per_key_concurrent": GsIntConfig(
        "每钥并发槽",
        "同一 API key 同时进行的 run 上限",
        DEFAULT_PER_KEY_CONCURRENT,
    ),
    "http_agent_rate_limit_rpm": GsIntConfig(
        "每钥每分钟请求数",
        "滑动 60 秒窗口",
        DEFAULT_RPM,
    ),
    "http_agent_max_body_bytes": GsIntConfig(
        "请求体上限（字节）",
        "Content-Length 与实读计数",
        DEFAULT_MAX_BODY_BYTES,
    ),
    "http_agent_max_images": GsIntConfig(
        "单请求图片数上限",
        "超过则 413",
        DEFAULT_MAX_IMAGES,
    ),
    "http_agent_wall_clock": GsIntConfig(
        "HTTP 会话墙钟（秒）",
        "仅 HTTP_AGENT: session",
        DEFAULT_WALL_CLOCK,
    ),
    "http_agent_hard_timeout": GsIntConfig(
        "硬超时（秒）",
        "超时 cancel 本轮 Task",
        DEFAULT_HARD_TIMEOUT,
    ),
    "http_agent_heartbeat_sec": GsIntConfig(
        "SSE 心跳间隔（秒）",
        "comment 行 : ping",
        DEFAULT_HEARTBEAT_SEC,
    ),
    "http_agent_queue_max": GsIntConfig(
        "CaptureBot 队列上限",
        "满则丢弃后续出站帧",
        DEFAULT_QUEUE_MAX,
    ),
    "http_agent_idempotency_ttl": GsIntConfig(
        "幂等记录 TTL（秒）",
        "client_msg_id 冲突窗口",
        DEFAULT_IDEMPOTENCY_TTL,
    ),
    "http_agent_idempotency_cap": GsIntConfig(
        "幂等记录条数上限",
        "超出淘汰最旧",
        DEFAULT_IDEMPOTENCY_CAP,
    ),
    "http_agent_cors_origins": GsListStrConfig(
        "CORS Origin 白名单",
        "空则不发 Access-Control-Allow-Origin",
        [],
    ),
    "http_agent_default_persona": GsStrConfig(
        "默认人格名",
        "空表示不由框架填角色名，走会话匹配",
        "",
    ),
    "http_agent_auth_fail_max": GsIntConfig(
        "鉴权失败次数上限",
        "独立于 WS 封禁；成功则重置",
        DEFAULT_AUTH_FAIL_MAX,
    ),
    "http_agent_auth_ban_sec": GsIntConfig(
        "鉴权封禁秒数",
        "超过失败次数后封该 IP",
        DEFAULT_AUTH_BAN_SEC,
    ),
}

http_agent_api_config = StringConfig(
    "GsCore HTTP Agent API",
    get_res_path("ai_core") / "http_agent_api.json",
    HTTP_AGENT_API_CONFIG,
)


@dataclass(frozen=True)
class HttpAgentSettings:
    enable: bool
    max_concurrent: int
    per_key_concurrent: int
    rate_limit_rpm: int
    max_body_bytes: int
    max_images: int
    wall_clock: int
    hard_timeout: int
    heartbeat_sec: int
    queue_max: int
    idempotency_ttl: int
    idempotency_cap: int
    cors_origins: List[str]
    default_persona: str
    auth_fail_max: int
    auth_ban_sec: int


def _positive_int(raw: object, fallback: int) -> int:
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int) and raw > 0:
        return raw
    return fallback


def _nonneg_int(raw: object, fallback: int) -> int:
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int) and raw >= 0:
        return raw
    return fallback


def _str_list(raw: object) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            out.append(item)
    return out


def _as_str(raw: object) -> str:
    return raw if isinstance(raw, str) else ""


def _entry_data(parsed: object, key: str) -> object | None:
    if not isinstance(parsed, dict):
        return None
    if key not in parsed:
        return None
    entry = parsed[key]
    if not isinstance(entry, dict) or "data" not in entry:
        return None
    return entry["data"]


def _settings_from_parsed(parsed: object) -> HttpAgentSettings:
    return HttpAgentSettings(
        enable=bool(_entry_data(parsed, "enable_http_agent_api") is True),
        max_concurrent=_positive_int(_entry_data(parsed, "http_agent_max_concurrent"), DEFAULT_MAX_CONCURRENT),
        per_key_concurrent=_positive_int(
            _entry_data(parsed, "http_agent_per_key_concurrent"), DEFAULT_PER_KEY_CONCURRENT
        ),
        rate_limit_rpm=_positive_int(_entry_data(parsed, "http_agent_rate_limit_rpm"), DEFAULT_RPM),
        max_body_bytes=_positive_int(_entry_data(parsed, "http_agent_max_body_bytes"), DEFAULT_MAX_BODY_BYTES),
        max_images=_positive_int(_entry_data(parsed, "http_agent_max_images"), DEFAULT_MAX_IMAGES),
        wall_clock=_positive_int(_entry_data(parsed, "http_agent_wall_clock"), DEFAULT_WALL_CLOCK),
        hard_timeout=_positive_int(_entry_data(parsed, "http_agent_hard_timeout"), DEFAULT_HARD_TIMEOUT),
        heartbeat_sec=_positive_int(_entry_data(parsed, "http_agent_heartbeat_sec"), DEFAULT_HEARTBEAT_SEC),
        queue_max=_positive_int(_entry_data(parsed, "http_agent_queue_max"), DEFAULT_QUEUE_MAX),
        idempotency_ttl=_positive_int(_entry_data(parsed, "http_agent_idempotency_ttl"), DEFAULT_IDEMPOTENCY_TTL),
        idempotency_cap=_positive_int(_entry_data(parsed, "http_agent_idempotency_cap"), DEFAULT_IDEMPOTENCY_CAP),
        cors_origins=_str_list(_entry_data(parsed, "http_agent_cors_origins")),
        default_persona=_as_str(_entry_data(parsed, "http_agent_default_persona")),
        auth_fail_max=_positive_int(_entry_data(parsed, "http_agent_auth_fail_max"), DEFAULT_AUTH_FAIL_MAX),
        auth_ban_sec=_nonneg_int(_entry_data(parsed, "http_agent_auth_ban_sec"), DEFAULT_AUTH_BAN_SEC),
    )


def _settings_from_memory() -> HttpAgentSettings:
    cfg = http_agent_api_config
    enable_item = cfg.get_config("enable_http_agent_api")
    persona_item = cfg.get_config("http_agent_default_persona")
    cors_item = cfg.get_config("http_agent_cors_origins")
    return HttpAgentSettings(
        enable=isinstance(enable_item, GsBoolConfig) and bool(enable_item.data),
        max_concurrent=_positive_int(cfg.get_config("http_agent_max_concurrent").data, DEFAULT_MAX_CONCURRENT),
        per_key_concurrent=_positive_int(
            cfg.get_config("http_agent_per_key_concurrent").data, DEFAULT_PER_KEY_CONCURRENT
        ),
        rate_limit_rpm=_positive_int(cfg.get_config("http_agent_rate_limit_rpm").data, DEFAULT_RPM),
        max_body_bytes=_positive_int(cfg.get_config("http_agent_max_body_bytes").data, DEFAULT_MAX_BODY_BYTES),
        max_images=_positive_int(cfg.get_config("http_agent_max_images").data, DEFAULT_MAX_IMAGES),
        wall_clock=_positive_int(cfg.get_config("http_agent_wall_clock").data, DEFAULT_WALL_CLOCK),
        hard_timeout=_positive_int(cfg.get_config("http_agent_hard_timeout").data, DEFAULT_HARD_TIMEOUT),
        heartbeat_sec=_positive_int(cfg.get_config("http_agent_heartbeat_sec").data, DEFAULT_HEARTBEAT_SEC),
        queue_max=_positive_int(cfg.get_config("http_agent_queue_max").data, DEFAULT_QUEUE_MAX),
        idempotency_ttl=_positive_int(cfg.get_config("http_agent_idempotency_ttl").data, DEFAULT_IDEMPOTENCY_TTL),
        idempotency_cap=_positive_int(cfg.get_config("http_agent_idempotency_cap").data, DEFAULT_IDEMPOTENCY_CAP),
        cors_origins=_str_list(cors_item.data if isinstance(cors_item, GsListStrConfig) else []),
        default_persona=persona_item.data.strip() if isinstance(persona_item, GsStrConfig) else "",
        auth_fail_max=_positive_int(cfg.get_config("http_agent_auth_fail_max").data, DEFAULT_AUTH_FAIL_MAX),
        auth_ban_sec=_nonneg_int(cfg.get_config("http_agent_auth_ban_sec").data, DEFAULT_AUTH_BAN_SEC),
    )


_cached_mtime: float = -1.0
_cached_settings: HttpAgentSettings | None = None


def load_http_agent_settings() -> HttpAgentSettings:
    """按配置文件 mtime 热读；解析失败回落内存副本。"""
    global _cached_mtime, _cached_settings
    path = http_agent_api_config.CONFIG_PATH
    try:
        mtime = path.stat().st_mtime
        if _cached_settings is not None and mtime == _cached_mtime:
            return _cached_settings
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _settings_from_memory()
    settings = _settings_from_parsed(parsed)
    _cached_mtime = mtime
    _cached_settings = settings
    return settings


def is_http_agent_enabled() -> bool:
    return load_http_agent_settings().enable


def reset_settings_cache_for_tests() -> None:
    global _cached_mtime, _cached_settings
    _cached_mtime = -1.0
    _cached_settings = None
