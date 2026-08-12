"""上游调用方归属透传（end-user attribution）。

OpenAI 协议为「终端用户标识」定义了标准请求字段 ``user``(end-user IDs, 见 OpenAI
safety best practices)：请求带上它，上游网关就能把用量、日志与滥用监控按调用方聚合，
而不必给每个调用方单独签发密钥。本模块把这一能力接到框架既有的归属链路上。

归属来源复用调用归属三元组 ``(group_id, user_id, bot_id)``——交互链路由 ``Event``
解析；后台自主调用（巡检 / 主动发言 / 记忆摄入等）经 ``bind_budget_scope`` 或
``set_budget_scope_context`` 绑定；嵌套调用沿 contextvar 自动继承。也就是说本模块
不新增身份通道，只是把已有的归属信息按需带给上游。

是否透传由 provider 配置项 ``forward_end_user_id`` 决定，默认 ``off``（行为与不存在
本模块完全一致）；``hashed`` 发送加盐摘要，``raw`` 发送原始标识。开关放在 provider
配置文件而非全局配置，因为「该不该把标识发给这个上游」是逐上游的判断——发给自建网关
与发给第三方官方端点显然不是一回事。

仅 OpenAI 兼容 provider 支持：``user`` 是 OpenAI 协议字段，Anthropic / Gemini 没有
对等的标准字段。

宿主可注册 :func:`register_attribution_resolver` 接管解析结果，把框架内部标识映射为
上游认得的主体、或附加自定义请求头。本模块是只读旁路：不接触任何凭据，解析不出结果
一律降级为「不透传」，绝不打断一次真实的 run。
"""

from __future__ import annotations

import hmac
import hashlib
from typing import Tuple, Mapping, Callable, Optional
from dataclasses import dataclass

from pydantic_ai.models.openai import OpenAIChatModelSettings

from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .models import PROVIDER_CONFIG_SEPARATOR
from .openai_config import get_openai_config

#: ``forward_end_user_id`` 的合法取值
FORWARD_OFF = "off"
FORWARD_HASHED = "hashed"
FORWARD_RAW = "raw"
FORWARD_MODES: Tuple[str, ...] = (FORWARD_OFF, FORWARD_HASHED, FORWARD_RAW)

#: 唯一支持终端用户标识的 provider（``user`` 是 OpenAI 协议字段）
ATTRIBUTION_PROVIDER = "openai"

#: 摘要标识保留长度（32 hex = 128 bit，碰撞概率可忽略且日志里不至于太长）
_HASH_HEX_LEN = 32


@dataclass(frozen=True)
class AttributionRequest:
    """一次归属解析的输入快照（只读，供宿主解析器决策）。"""

    provider: str
    config_name: str
    task_level: str
    forward_mode: str
    group_id: str
    user_id: str
    bot_id: str
    session_id: str
    create_by: str


@dataclass(frozen=True)
class CallAttribution:
    """归属解析结果：透传给上游的终端用户标识 + 附加请求头。

    两者都可为空——``end_user_id`` 为空表示只加请求头，全空等价于不透传。
    """

    end_user_id: Optional[str] = None
    extra_headers: Optional[Mapping[str, str]] = None


#: 宿主解析器：返回 ``None`` 表示本次弃权（不透传）
AttributionResolver = Callable[[AttributionRequest], Optional[CallAttribution]]

_resolver: Optional[AttributionResolver] = None


def register_attribution_resolver(resolver: AttributionResolver) -> None:
    """注册归属解析器，接管默认的「按配置模式取归属 user_id」行为。

    同一时刻只有一个解析器生效，重复注册覆盖前者。解析器须是纯粹的同步旁路逻辑
    （建议只做映射与缓存查询），返回 ``None`` 即本次不透传。
    """
    global _resolver
    _resolver = resolver
    logger.info(t("log.ai.attribution_resolver_registered"))


def unregister_attribution_resolver() -> None:
    """移除已注册的解析器，回落到默认解析行为。"""
    global _resolver
    _resolver = None


def get_attribution_resolver() -> Optional[AttributionResolver]:
    """当前生效的解析器；``None`` 表示走默认行为。"""
    return _resolver


def hash_end_user_id(raw_id: str, salt: str) -> str:
    """HMAC-SHA256(salt, raw_id) 的前 32 位十六进制。

    salt 留空即无密钥摘要：标识空间小（纯数字账号 / QQ 号）时可被枚举反查，只起混淆
    作用；需要抗反查必须配置 salt。
    """
    digest = hmac.new(salt.encode("utf-8"), raw_id.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:_HASH_HEX_LEN]


def split_provider_config_name(full_name: str) -> Tuple[str, str]:
    """``provider++name`` → (provider, name)；无分隔符按 openai 处理（兼容旧格式）。

    与 ``models.parse_provider_config_name`` 的区别：未知 provider 原样返回而不抛错
    ——归属透传是旁路，不该因为配置名异常而中断 run。
    """
    if PROVIDER_CONFIG_SEPARATOR in full_name:
        provider, config_name = full_name.split(PROVIDER_CONFIG_SEPARATOR, 1)
        return provider, config_name
    return ATTRIBUTION_PROVIDER, full_name


def read_forward_config(config_name: str) -> Tuple[str, str]:
    """读取 (forward_mode, salt)；旧配置文件缺 key 时 get_config 自动补模板默认值。"""
    oconfig = get_openai_config(config_name)
    mode = str(oconfig.get_config("forward_end_user_id").data).strip().lower()
    salt = str(oconfig.get_config("end_user_id_salt").data)
    return (mode if mode in FORWARD_MODES else FORWARD_OFF), salt


def default_attribution(req: AttributionRequest, salt: str) -> Optional[CallAttribution]:
    """默认解析：把归属三元组里的 ``user_id`` 按模式转成终端用户标识。

    无 user_id（真正无主的后台调用，如共享素材库打标）返回 None，让上游归入匿名桶。
    """
    if not req.user_id:
        return None
    if req.forward_mode == FORWARD_RAW:
        return CallAttribution(end_user_id=req.user_id)
    if req.forward_mode == FORWARD_HASHED:
        return CallAttribution(end_user_id=hash_end_user_id(req.user_id, salt))
    return None


def default_end_user_id(req: AttributionRequest) -> str:
    """按配置模式算出本次该透传的标识；不透传时返回空串。

    给宿主解析器用：只想追加请求头、归属仍沿用配置语义（raw/hashed）时调它，
    这样 salt 不必交到解析器手里。
    """
    _mode, salt = read_forward_config(req.config_name)
    attribution = default_attribution(req, salt)
    if attribution is None or not attribution.end_user_id:
        return ""
    return attribution.end_user_id


def resolve_attribution_settings(
    *,
    config_full_name: str,
    task_level: str,
    scope: Optional[Tuple[str, str, str]],
    session_id: str,
    create_by: str,
) -> Optional[OpenAIChatModelSettings]:
    """解析本次 run 需要叠加的 ModelSettings 增量；不透传时返回 ``None``。

    返回 None 的全部情形：无激活配置 / 非 openai provider / 开关 off / 无归属可用 /
    解析器弃权或抛错。调用方按 ``merge_model_settings`` 叠加即可，None 是空操作。
    """
    if not config_full_name:
        return None

    provider, config_name = split_provider_config_name(config_full_name)
    if provider != ATTRIBUTION_PROVIDER:
        return None

    # 白名单而非 != off：任何非法/未知模式都按不透传处理（宁可少发，不可误发）
    forward_mode, salt = read_forward_config(config_name)
    if forward_mode not in (FORWARD_HASHED, FORWARD_RAW):
        return None

    group_id, user_id, bot_id = scope if scope is not None else ("", "", "")
    req = AttributionRequest(
        provider=provider,
        config_name=config_name,
        task_level=task_level,
        forward_mode=forward_mode,
        group_id=group_id,
        user_id=user_id,
        bot_id=bot_id,
        session_id=session_id,
        create_by=create_by,
    )

    resolver = _resolver
    if resolver is None:
        attribution = default_attribution(req, salt)
    else:
        # 宿主回调是旁路：任何异常都降级为不透传，绝不带崩一次真实 run（同 on_trace 约定）
        try:
            attribution = resolver(req)
        except Exception as e:  # noqa: BLE001
            logger.warning(t("log.ai.attribution_resolver_fail", error=str(e)))
            return None

    if attribution is None:
        return None

    settings = OpenAIChatModelSettings()
    if attribution.end_user_id:
        settings["openai_user"] = attribution.end_user_id
    headers = dict(attribution.extra_headers) if attribution.extra_headers else {}
    if headers:
        settings["extra_headers"] = headers
    if not settings:
        return None

    logger.debug(
        t(
            "log.ai.attribution_forwarded",
            config_name=config_name,
            mode=forward_mode,
            end_user=attribution.end_user_id or "-",
            headers=len(headers),
        )
    )
    return settings
