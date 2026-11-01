"""多人格隔离一次性迁移（multi_persona_scope_v1）。

把迁移前不带 persona 维度的存量状态一次性归入**默认归属人格**：

1. ``userfavorability`` 中 ``persona_name=""`` 的行 → 默认人格；
2. ``AIPersistentState`` 中 ``self:{bot_id}``（无第二段）的 ``self_model`` →
   ``self:{bot_id}:{default_persona}``；
3. ``aside_recall.json`` 中无人格条目 → 默认人格前缀键。

标记位 ``system:migrations / multi_persona_scope_v1`` 保证只执行一次；
``get_legacy_owner_persona()`` 供读侧决定谁能看到旧 ``self:{bot_id}`` 下的记忆。
"""

import time
from typing import Mapping, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import StringConfig

_MARKER_SCOPE = "system:migrations"
_MARKER_KEY = "multi_persona_scope_v1"
_SELF_MODEL_KEY = "self_model"

_legacy_owner: str = ""


def get_legacy_owner_persona() -> str:
    """存量无人格数据的归属人格。迁移未跑或无人格启用时为空串。"""
    return _legacy_owner


def _scope_of(config: StringConfig) -> str:
    scope = config.get_config("scope").data
    return str(scope) if isinstance(scope, str) else ""


def _target_groups_of(config: StringConfig) -> list[str]:
    raw = config.get_config("target_groups").data
    return [str(g) for g in raw] if isinstance(raw, list) else []


def pick_default_persona(configs: Mapping[str, StringConfig]) -> Optional[str]:
    """选存量数据归属人格：global → global_group → global_private → 绑定群最多的 specific。"""
    enabled = {name: cfg for name, cfg in configs.items() if _scope_of(cfg) != "disabled"}
    for scope in ("global", "global_group", "global_private"):
        for name in sorted(enabled):
            if _scope_of(enabled[name]) == scope:
                return name
    specific = [name for name in enabled if _scope_of(enabled[name]) == "specific"]
    if specific:
        specific.sort(key=lambda n: (-len(_target_groups_of(enabled[n])), n))
        return specific[0]
    return None


async def _migrate_self_models(default_persona: str) -> int:
    """``self:{x}`` 旧键的 self_model 复制到 ``self:{x}:{default}`` 后删旧键。"""
    from gsuid_core.ai_core.state_store import state_get_value, state_set_value, state_delete_value
    from gsuid_core.ai_core.state_store.store import state_list_scopes_with_key

    scopes = await state_list_scopes_with_key(_SELF_MODEL_KEY, scope_prefix="self:")
    migrated = 0
    for scope in scopes:
        # 旧键形如 self:{bot_id}（恰好一段冒号）；self:x:persona 已是新格式
        if scope.count(":") != 1:
            continue
        target = f"{scope}:{default_persona}"
        if await state_get_value(target, _SELF_MODEL_KEY) is None:
            value = await state_get_value(scope, _SELF_MODEL_KEY)
            if value is not None:
                await state_set_value(target, _SELF_MODEL_KEY, value)
        await state_delete_value(scope, _SELF_MODEL_KEY)
        migrated += 1
    return migrated


async def migrate_legacy_persona_scopes() -> None:
    """启动期幂等迁移。AI 总开关关闭时跳过。"""
    global _legacy_owner
    from gsuid_core.ai_core.state_store import state_get_value, state_set_value
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return

    marker = await state_get_value(_MARKER_SCOPE, _MARKER_KEY)
    if isinstance(marker, dict):
        _legacy_owner = str(marker["default_persona"]) if "default_persona" in marker else ""
        return

    from gsuid_core.ai_core.persona.config import persona_config_manager

    default_persona = pick_default_persona(persona_config_manager.get_all_configs())
    if default_persona is None:
        logger.info(t("log.ai.migrations_persona_scope_skip"))
        return

    from gsuid_core.ai_core.database.models import UserFavorability

    try:
        renamed, dropped = await UserFavorability.reassign_legacy_persona(default_persona)
    except Exception as e:
        logger.warning(t("log.ai.migrations_persona_scope_fail", e=e))
        return

    self_models = await _migrate_self_models(default_persona)

    from gsuid_core.ai_core.command_aside.recall import migrate_legacy_items

    aside_items = migrate_legacy_items(default_persona)

    await state_set_value(
        _MARKER_SCOPE,
        _MARKER_KEY,
        {"default_persona": default_persona, "done_at": time.time()},
    )
    _legacy_owner = default_persona
    logger.info(
        t(
            "log.ai.migrations_persona_scope_done",
            persona_name=default_persona,
            favor=renamed,
            favor_dropped=dropped,
            self_models=self_models,
            aside=aside_items,
        )
    )
