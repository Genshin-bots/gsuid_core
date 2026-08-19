"""第一方套件（Agent Kit）对外入口。

套件**不是** ``gsuid_core/plugins/`` 下的业务插件：它们随框架发布、不受 ``--dev``
目录名过滤、代码改动需重启进程。用户插件想换掉某个默认能力时，声明同一个 ``slot``
并把 ``kit_slots.<slot>`` 指向自己的 ``kit_id`` 即可，无需改 ``handle_ai.py``。
"""

from typing import List

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.kits.base import (
    OFF,
    KIT_SLOTS,
    STABLE_BLOCK_NAMES,
    CONTEXT_BLOCK_ORDER,
    KitSlot,
    AgentKit,
    slot_of,
    is_known_slot,
    is_known_block,
)
from gsuid_core.ai_core.kits.registry import (
    KitSlotError,
    get_kit,
    list_kits,
    clear_kits,
    enable_kit,
    disable_kit,
    slot_health,
    disable_slot,
    occupants_of,
    kits_for_slot,
    register_agent_kit,
    resolve_slot_config,
)

__all__ = [
    "CONTEXT_BLOCK_ORDER",
    "KIT_SLOTS",
    "OFF",
    "STABLE_BLOCK_NAMES",
    "AgentKit",
    "KitSlot",
    "KitSlotError",
    "clear_kits",
    "disable_kit",
    "disable_slot",
    "enable_kit",
    "get_kit",
    "is_known_block",
    "is_known_slot",
    "kits_for_slot",
    "list_kits",
    "load_enabled_kits",
    "occupants_of",
    "register_agent_kit",
    "resolve_slot_config",
    "slot_health",
    "slot_of",
    "unload_all_kits",
]


def _import_builtin_kits() -> None:
    """导入第一方套件模块并（重）登记它们的 ``KIT`` 实例。

    只登记不启用；启用由 ``load_enabled_kits`` 按 ``kit_slots.*`` 配置决定。
    **显式重登记**而不是靠 import 副作用：模块 import 有缓存，卸载注册表之后再
    import 是 no-op，注册表会一直空着。
    新增套件时在此追加一行——列表是有意手写的，便于按槽位排查加载顺序。
    """
    from gsuid_core.ai_core.kits.meme import kit as meme_kit
    from gsuid_core.ai_core.kits.mood import kit as mood_kit
    from gsuid_core.ai_core.kits.fileos import kit as fileos_kit
    from gsuid_core.ai_core.kits.memory import kit as memory_kit
    from gsuid_core.ai_core.kits.speech import kit as speech_kit
    from gsuid_core.ai_core.kits.quality import kit as quality_kit
    from gsuid_core.ai_core.kits.identity import kit as identity_kit
    from gsuid_core.ai_core.kits.scaffold import kit as scaffold_kit
    from gsuid_core.ai_core.kits.post_tool import kit as post_tool_kit
    from gsuid_core.ai_core.kits.classifier import kit as classifier_kit
    from gsuid_core.ai_core.kits.statistics import kit as statistics_kit
    from gsuid_core.ai_core.kits.favorability import kit as favorability_kit
    from gsuid_core.ai_core.kits.session_mute import kit as session_mute_kit
    from gsuid_core.ai_core.kits.group_profile import kit as group_profile_kit
    from gsuid_core.ai_core.kits.reactive_gate import kit as reactive_gate_kit
    from gsuid_core.ai_core.kits.tool_assembly import kit as tool_assembly_kit
    from gsuid_core.ai_core.kits.self_cognition import kit as self_cognition_kit
    from gsuid_core.ai_core.kits.decision_distill import kit as decision_distill_kit
    from gsuid_core.ai_core.kits.planning_context import kit as planning_context_kit

    for module in (
        memory_kit,
        meme_kit,
        favorability_kit,
        mood_kit,
        self_cognition_kit,
        group_profile_kit,
        planning_context_kit,
        decision_distill_kit,
        classifier_kit,
        reactive_gate_kit,
        scaffold_kit,
        session_mute_kit,
        statistics_kit,
        tool_assembly_kit,
        fileos_kit,
        post_tool_kit,
        quality_kit,
        speech_kit,
        identity_kit,
    ):
        register_agent_kit(module.KIT)


async def load_enabled_kits(*, run_init_steps: bool = True) -> List[str]:
    """按 ``kit_slots.*`` 配置启用套件，并跑各自的 ``init_step``。

    排在 ``_INIT_STEPS`` **最前**（只挂 hook，0.1s 级）。放后面会让启动窗口
    里的请求误判「套件没装好 = 用户关了槽」。AI 总开关关闭时整条不跑（D-21）。
    单个套件的注册或 init_step 失败**只降级它自己**（与 ``_INIT_STEPS`` 同口径）：
    记忆初始化炸了不该让 mood / 分类器一起没有。返回实际启用的 kit_id 列表。
    """
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return []
    _import_builtin_kits()

    allow_sealed = bool(ai_config.get_config("allow_replace_sealed").data)
    enabled: List[str] = []
    for slot in KIT_SLOTS:
        for kit_id in resolve_slot_config(slot.name):
            kit = get_kit(kit_id)
            if kit is None:
                logger.warning(t("log.agent.kits_configured_kit_missing", slot=slot.name, kit=kit_id))
                continue
            # 槽名与套件自报的 slot 必须一致：不校验会「配错槽却静默启用到别的槽」，
            # 被配的那个槽反而空着（关能力却看不出来）。
            if kit.slot != slot.name:
                logger.warning(t("log.agent.kits_slot_mismatch", slot=slot.name, kit=kit_id, own=kit.slot))
                continue
            try:
                if enable_kit(kit_id, allow_replace_sealed=allow_sealed):
                    enabled.append(kit_id)
            except Exception as e:
                logger.warning(t("log.agent.kits_enable_fail", kit=kit_id, e=e))

    if run_init_steps:
        for kit_id in enabled:
            kit = get_kit(kit_id)
            if kit is None or kit.init_step is None:
                continue
            try:
                await kit.init_step()
            except Exception as e:
                logger.warning(t("log.agent.kits_init_step_fail", kit=kit_id, e=e))
    logger.info(t("log.agent.kits_loaded_total", n=len(enabled)))
    return enabled


def unload_all_kits() -> int:
    """卸载全部已启用套件（进程关闭 / 测试隔离）。返回卸掉的数量。"""
    n = 0
    for slot in KIT_SLOTS:
        n += disable_slot(slot.name)
    return n
