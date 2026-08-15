"""Hook 注册表：``on_agent_hook`` 装饰器 + 按模块 / 套件摘钩。

同优先级串行按注册序（环内有变异，不能像启动钩子那样并发）。
第一方套件占 100–399，第三方建议 400+。
"""

import inspect
from typing import Dict, List, Tuple, Callable, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.hooks.models import HookFn, HookRegistration
from gsuid_core.ai_core.hooks.points import WIRED_POINTS, AgentHookPoint, spec_for

_REGISTRY: Dict[AgentHookPoint, List[HookRegistration]] = {}
_ORDER_SEQ = 0


def on_agent_hook(
    point: AgentHookPoint,
    /,
    priority: int = 0,
    *,
    create_by: Optional[Tuple[str, ...]] = None,
    personas: Optional[Tuple[str, ...]] = None,
    include_subagent: bool = False,
    include_correction: bool = False,
    include_framework: bool = False,
    timeout_ms: Optional[int] = None,
    kit_id: Optional[str] = None,
) -> Callable[[HookFn], HookFn]:
    """把一个函数挂到某个 Agent 环点位上。

    ``priority`` 越小越先。``create_by=None`` 表示用默认过滤器（Chat/Agent/TEST）。
    ``timeout_ms=None`` 取点位默认预算（见 ``HOOK_POINT_SPECS``）。

    挂到 ``wired=False`` 的点位上会**告警**：契约已定但内核还没在那儿开火，回调永远
    不会执行。静默接受这种注册最坑——插件作者按发布的点位表写了 `veto_tool`，
    上线后既没效果也没有任何错误信息。
    """

    def decorator(func: HookFn) -> HookFn:
        global _ORDER_SEQ
        _ORDER_SEQ += 1
        module = func.__module__ if hasattr(func, "__module__") else ""
        if point not in WIRED_POINTS:
            # 第一方套件在未接线点位上放的是**占位**（handler 多为空实现），接线时一并生效，
            # 每次启动刷 warning 只会训练大家忽略它；第三方则必须响一声——它按发布的
            # 点位表挂了 veto_tool / replace_text，上线后既没效果也没有任何错误信息。
            emit = logger.debug if kit_id else logger.warning
            emit(t("log.agent.hooks_point_not_wired", point=point.name, owner=kit_id or module or "?"))
        reg = HookRegistration(
            point=point,
            func=func,
            priority=priority,
            order=_ORDER_SEQ,
            module=module,
            kit_id=kit_id,
            create_by=create_by,
            personas=personas,
            include_subagent=include_subagent,
            include_correction=include_correction,
            include_framework=include_framework,
            timeout_ms=timeout_ms if timeout_ms is not None else spec_for(point).default_timeout_ms,
        )
        bucket = _REGISTRY.setdefault(point, [])
        bucket.append(reg)
        bucket.sort(key=lambda r: (r.priority, r.order))
        logger.debug(t("log.agent.hooks_registered_point", point=point.name, owner=reg.label))
        return func

    return decorator


def hooks_for(point: AgentHookPoint) -> List[HookRegistration]:
    """取该点位的注册列表（已按 priority/order 排好）。空表时返回空 list。"""
    return _REGISTRY[point] if point in _REGISTRY else []


def hooks_registered(point: AgentHookPoint) -> bool:
    """空表快速判定：dispatcher 据此跳过 Context 构造。"""
    return bool(_REGISTRY[point]) if point in _REGISTRY else False


def hook_count() -> int:
    return sum(len(v) for v in _REGISTRY.values())


def list_hooks() -> Dict[str, List[str]]:
    """WebConsole / 诊断用：点位名 → owner 标签列表。"""
    return {p.name: [r.label for r in regs] for p, regs in _REGISTRY.items() if regs}


def drop_hooks_for_module(module_prefix: str) -> int:
    """按模块前缀摘钩（插件热重载）。返回摘掉的条数。"""
    removed = 0
    for point, regs in _REGISTRY.items():
        keep = [r for r in regs if not r.module.startswith(module_prefix)]
        removed += len(regs) - len(keep)
        _REGISTRY[point] = keep
    if removed:
        logger.info(t("log.agent.hooks_dropped_module", module=module_prefix, n=removed))
    return removed


def drop_hooks_for_kit(kit_id: str) -> int:
    """按 kit_id 摘钩（套件卸载 / 同槽替换）。返回摘掉的条数。"""
    removed = 0
    for point, regs in _REGISTRY.items():
        keep = [r for r in regs if r.kit_id != kit_id]
        removed += len(regs) - len(keep)
        _REGISTRY[point] = keep
    if removed:
        logger.debug(t("log.agent.hooks_dropped_kit", kit=kit_id, n=removed))
    return removed


def clear_hooks() -> None:
    """仅供测试：清空全表。"""
    _REGISTRY.clear()


def is_async_hook(func: HookFn) -> bool:
    return inspect.iscoroutinefunction(func)
