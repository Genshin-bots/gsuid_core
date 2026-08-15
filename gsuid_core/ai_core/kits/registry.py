"""套件注册表：kit_id → AgentKit，slot → 当前占用者。

互斥槽同时只有一个占用者：启用新套件前先 ``unregister`` 旧的，不允许静默双挂。
密封槽默认拒绝替换，除非 ``allow_replace_sealed=true``。
"""

from typing import Dict, List, Tuple, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.kits.base import OFF, AgentKit, slot_of, is_known_slot

_KITS: Dict[str, AgentKit] = {}
_OCCUPANTS: Dict[str, List[str]] = {}


class KitSlotError(RuntimeError):
    """槽位违规：未知槽名 / 未注册 kit_id / 拒绝替换密封槽。"""


def register_agent_kit(kit: AgentKit) -> AgentKit:
    """登记一个套件（不等于启用）。可直接当类装饰器用于 ``AgentKit`` 子类实例。"""
    if not is_known_slot(kit.slot):
        raise KitSlotError(f"未知槽名 {kit.slot!r}（须在 KIT_SLOTS 内）")
    if kit.kit_id in _KITS and _KITS[kit.kit_id] is not kit:
        logger.debug(t("log.agent.kits_overwrite_registration", kit=kit.kit_id))
    _KITS[kit.kit_id] = kit
    return kit


def get_kit(kit_id: str) -> Optional[AgentKit]:
    return _KITS[kit_id] if kit_id in _KITS else None


def list_kits() -> Tuple[AgentKit, ...]:
    return tuple(_KITS.values())


def kits_for_slot(slot: str) -> Tuple[AgentKit, ...]:
    """该槽位所有**已登记**的候选套件（含未启用者）。"""
    return tuple(k for k in _KITS.values() if k.slot == slot)


def occupants_of(slot: str) -> Tuple[str, ...]:
    """该槽位当前**已启用**的占用者 kit_id。"""
    return tuple(_OCCUPANTS[slot]) if slot in _OCCUPANTS else ()


def slot_health() -> Dict[str, List[str]]:
    """WebConsole 槽位健康视图：槽名 → 占用者列表（空 list = off）。"""
    from gsuid_core.ai_core.kits.base import KIT_SLOTS

    return {s.name: list(occupants_of(s.name)) for s in KIT_SLOTS}


def enable_kit(kit_id: str, *, allow_replace_sealed: bool = False) -> bool:
    """启用一个套件；互斥槽先卸旧占用者。返回是否真的完成了注册。"""
    kit = get_kit(kit_id)
    if kit is None:
        raise KitSlotError(f"kit_id {kit_id!r} 未登记")
    slot = slot_of(kit.slot)
    current = occupants_of(kit.slot)
    if kit_id in current:
        return False

    if slot.exclusive and current:
        if slot.sealed and not allow_replace_sealed:
            raise KitSlotError(f"槽 {slot.name!r} 已密封，替换需 allow_replace_sealed=true")
        for old_id in current:
            disable_kit(old_id)
        logger.info(t("log.agent.kits_replace_slot", slot=slot.name, old=",".join(current), new=kit_id))
    else:
        logger.info(t("log.agent.kits_load_slot", slot=slot.name, kit=kit_id))

    kit.register()
    _OCCUPANTS.setdefault(kit.slot, []).append(kit_id)
    return True


def disable_kit(kit_id: str) -> bool:
    """卸载一个套件（摘 hook + 卸 owns_tools）。返回是否原本处于启用态。"""
    kit = get_kit(kit_id)
    if kit is None:
        return False
    bucket = _OCCUPANTS[kit.slot] if kit.slot in _OCCUPANTS else []
    if kit_id not in bucket:
        return False
    kit.unregister()
    bucket.remove(kit_id)
    logger.info(t("log.agent.kits_unload_slot", slot=kit.slot, kit=kit_id))
    return True


def disable_slot(slot: str) -> int:
    """把整槽置为 ``off``：卸掉全部占用者。返回卸掉的数量。"""
    n = 0
    for kit_id in occupants_of(slot):
        if disable_kit(kit_id):
            n += 1
    return n


def resolve_slot_config(slot: str) -> Tuple[str, ...]:
    """读 ``kit_slots.<slot>`` 配置，解析出该槽应启用的 kit_id 列表。

    ``off`` → 空元组（该槽无占用者，对应能力自然跳过，不在内核写 ``if enable_x``）。
    非互斥槽支持逗号分隔多占用者。
    """
    from gsuid_core.ai_core.configs.ai_config import ai_config

    spec = slot_of(slot)
    raw = str(ai_config.get_config(f"kit_slots.{slot}").data or "").strip()
    if not raw:
        raw = spec.default_kit_id
    if raw == OFF:
        return ()
    ids = tuple(part.strip() for part in raw.split(",") if part.strip())
    return ids[:1] if spec.exclusive else ids


def clear_kits() -> None:
    """仅供测试：清空注册表与占用表。"""
    _KITS.clear()
    _OCCUPANTS.clear()
