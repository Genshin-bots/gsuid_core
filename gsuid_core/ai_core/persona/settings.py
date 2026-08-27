"""每个人格目录下的 ``persona.json``：称呼与用户可见短句。

与 ``config.json``（启用范围 / 巡检 / 工具装配）分开：这边只放「这个人格怎么叫主人、
失败时说什么」。模板是 ``Dict[str, GSC]``，控制台按插件配置同构渲染，加项不用改前端。
"""

from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.utils.path_safety import PathEscapeError, is_safe_filename
from gsuid_core.utils.plugins_config.models import GSC, GsDivider, GsStrConfig
from gsuid_core.utils.plugins_config.gs_config import ConfigSetManager

from ..resource import PERSONA_PATH

DEFAULT_MASTER_TITLE = "主人"
DEFAULT_ERROR_GENERIC = "这条消息我处理失败了，稍后再试一次吧"
DEFAULT_ERROR_TIMEOUT = "刚才网络太慢处理超时了，稍后再试试吧"
DEFAULT_ERROR_CONTENT_POLICY = "这条消息触发了内容安全策略，我没法处理"
DEFAULT_FALLBACK_OOC = "这个不太想说呢。"
DEFAULT_FALLBACK_MACHINE = "额…出错了，稍后再试"

DEFAULT_PERSONA_SETTINGS: dict[str, GSC] = {
    "_AddressDivider": GsDivider("称呼", "人格对特定对象的口头称呼", "称呼"),
    "master_title": GsStrConfig(
        "对主人的称呼",
        "对配置里 masters 用户的口头称呼。提示词、巡检台词都会用这个词，不要用这个称呼叫其他人。",
        DEFAULT_MASTER_TITLE,
    ),
    "_ErrorDivider": GsDivider(
        "失败与拦截",
        "失败或拦截时直接发给用户的台词，保持角色口吻、不要写内部错误细节。",
        "失败与拦截",
    ),
    "error_generic": GsStrConfig(
        "处理失败",
        "Agent 执行失败或没有有效结果时发给用户的短句。",
        DEFAULT_ERROR_GENERIC,
    ),
    "error_timeout": GsStrConfig(
        "处理超时",
        "请求超时或网络过慢时发给用户的短句。",
        DEFAULT_ERROR_TIMEOUT,
    ),
    "error_content_policy": GsStrConfig(
        "内容安全拦截",
        "命中模型内容安全策略时发给用户的短句。",
        DEFAULT_ERROR_CONTENT_POLICY,
    ),
    "fallback_ooc": GsStrConfig(
        "出戏拦截兜底",
        "回复命中出戏红线且无法重说时发给用户的中性短句（不要写死某个人格的口癖）。",
        DEFAULT_FALLBACK_OOC,
    ),
    "fallback_machine": GsStrConfig(
        "技术堆栈熔断",
        "回复像技术堆栈或状态 JSON 时发给用户的短句。",
        DEFAULT_FALLBACK_MACHINE,
    ),
    "task_ack": GsStrConfig(
        "接任务应",
        "点名办事且本轮要调工具时，模型没写短应则发这句。空则用当前人格卡语气词拼一句；还没有才用中性「收到。」。",
        "",
    ),
}


class PersonaSettingsManager(ConfigSetManager):
    """每个人格一份 ``persona.json``，schema 与插件配置相同。"""

    def __init__(self) -> None:
        super().__init__(
            base_path=PERSONA_PATH,
            config_template=DEFAULT_PERSONA_SETTINGS,
            name_suffix="Settings",
        )

    def _get_config_path(self, config_name: str) -> Path:
        from gsuid_core.utils.path_safety import safe_join

        return safe_join(self._base_path, config_name, "persona.json")

    def drop_cache(self, persona_name: str) -> None:
        if persona_name in self._cache:
            del self._cache[persona_name]


persona_settings_manager = PersonaSettingsManager()


def default_phrase(key: str) -> str:
    """模板默认文案；未知键或分割线返回空串。"""
    if key not in DEFAULT_PERSONA_SETTINGS:
        return ""
    item = DEFAULT_PERSONA_SETTINGS[key]
    if isinstance(item, GsStrConfig):
        return item.data
    return ""


def get_persona_setting(persona_name: str | None, key: str) -> str:
    """读 ``persona.json`` 字符串；人格不存在、键缺失或值为空时回退模板默认。"""
    fallback = default_phrase(key)
    if not persona_name or not is_safe_filename(persona_name):
        return fallback
    persona_dir = PERSONA_PATH / persona_name
    if not persona_dir.is_dir():
        return fallback
    try:
        cfg = persona_settings_manager.get_config(persona_name)
    except (ValueError, PathEscapeError, OSError) as e:
        logger.warning(t("log.persona.personasettings_read_fail", persona_name=persona_name, e=e))
        return fallback
    item = cfg.get_config(key)
    if isinstance(item, GsStrConfig):
        data = item.data.strip()
        if data:
            return data
    return fallback


def get_master_title(persona_name: str | None) -> str:
    return get_persona_setting(persona_name, "master_title")


def get_fallback_ooc(persona_name: str | None) -> str:
    return get_persona_setting(persona_name, "fallback_ooc")


def get_fallback_machine(persona_name: str | None) -> str:
    return get_persona_setting(persona_name, "fallback_machine")


def persona_name_from_event(ev: Event | None) -> str | None:
    """从会话事件解析当前人格；session_id 非法时返回 None，不抛。"""
    if ev is None:
        return None
    session_id = ev.session_id
    if not session_id:
        return None
    try:
        from gsuid_core.ai_core.persona.config import persona_config_manager

        return persona_config_manager.get_persona_for_session(session_id)
    except ValueError:
        return None
