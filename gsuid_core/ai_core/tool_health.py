"""工具健康度滑窗统计与自动冻结（方案九）。

坏工具常驻池的代价：每轮白耗 schema token、调用失败污染上下文、模型学到
「工具不可靠」。本模块给每个工具维护一个小滑窗，连续失败达阈值即**临时冻结**
（跳过执行、直接回不可用文案），窗口过期自动解冻。纯进程内、无业务特判：
失败口径只有两条结构信号——执行抛异常、返回以 ``❌`` 开头。

被冻结的工具仍保留在注册表/工具池（schema 可见），只是执行短路——避免动
装配层引入前缀缓存抖动。webconsole 可经 ``get_tool_health_snapshot`` 展示。
"""

from __future__ import annotations

import time
from typing import Any, Dict
from dataclasses import field, dataclass

# 滑窗内连续失败达到该次数即冻结
_FREEZE_AFTER_CONSECUTIVE_FAILS = 3
# 冻结持续时长（秒）：到期自动解冻，给上游恢复的机会
_FREEZE_SECONDS = 1800.0
# 成功一次即清零失败计数（间歇性失败不冻结）


@dataclass
class _ToolHealth:
    fails: int = 0
    frozen_until: float = 0.0
    total_calls: int = 0
    total_fails: int = 0
    last_error: str = ""
    last_ts: float = field(default=0.0)


_HEALTH: Dict[str, _ToolHealth] = {}


def _now() -> float:
    return time.time()


def record_tool_success(tool_name: str) -> None:
    """工具成功执行：清零连续失败计数，累计调用数。"""
    if not tool_name:
        return
    h = _HEALTH.setdefault(tool_name, _ToolHealth())
    h.fails = 0
    h.total_calls += 1
    h.last_ts = _now()


def record_tool_failure(tool_name: str, error_preview: str = "") -> None:
    """工具失败（异常或 ❌ 返回）：累计失败，达阈值即冻结。"""
    if not tool_name:
        return
    h = _HEALTH.setdefault(tool_name, _ToolHealth())
    h.fails += 1
    h.total_calls += 1
    h.total_fails += 1
    h.last_error = error_preview[:120]
    h.last_ts = _now()
    if h.fails >= _FREEZE_AFTER_CONSECUTIVE_FAILS:
        h.frozen_until = _now() + _FREEZE_SECONDS


def is_tool_frozen(tool_name: str) -> bool:
    """工具是否处于冻结期（到期自动解冻）。"""
    if not tool_name:
        return False
    h = _HEALTH.get(tool_name)
    if h is None or h.frozen_until <= 0:
        return False
    if _now() >= h.frozen_until:
        h.frozen_until = 0.0
        h.fails = 0
        return False
    return True


_FROZEN_CALLS: Dict[str, int] = {}


def frozen_tool_message(tool_name: str) -> str:
    """冻结期的统一短路文案（不泄漏内部机制，给模型换路指引）。"""
    return f"⚠️ {tool_name} 已临时停用（连续失败，稍后自动恢复）。请换其他工具；不要再硬调这个工具。"


async def frozen_tool_reply(tool_name: str, extra: Dict[str, Any], need: str) -> str:
    """第 1 次只回停用；第 2 次框架代查替代，无命中则登记缺口并止损。"""
    n = _FROZEN_CALLS.get(tool_name, 0) + 1
    _FROZEN_CALLS[tool_name] = n
    base = frozen_tool_message(tool_name)
    if n < 2:
        return base + "可用 find_tools 找替代。"
    from gsuid_core.ai_core.rag.tools import search_tools_with_entity_routing

    query = (need or tool_name).strip() or tool_name
    found = await search_tools_with_entity_routing(
        query=query,
        route_text=query,
        limit=4,
        non_category=["self", "buildin"],
        threshold=0.2,
        scope_key="",
    )
    names = [t.name for t in found if t.name != tool_name][:3]
    if names:
        return base + "替代建议：" + "、".join(names) + "。不要再调已停用的工具。"
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import _record_capability_gap

    _record_capability_gap(query)
    return base + "该能力暂缺，不要再绕、不要再调。"


def get_tool_health_snapshot(limit: int = 30) -> list[dict[str, object]]:
    """按总失败数降序返回工具健康快照（webconsole 展示用）。"""
    # 先用强类型的 (total_fails, name, _ToolHealth) 排序，避免对 dict[str, object] 取值做 int()
    ranked: list[tuple[int, str, _ToolHealth]] = [
        (h.total_fails, name, h) for name, h in _HEALTH.items() if h.total_fails > 0
    ]
    ranked.sort(key=lambda item: item[0], reverse=True)
    items: list[dict[str, object]] = []
    for _fails, name, h in ranked[:limit]:
        items.append(
            {
                "name": name,
                "total_calls": h.total_calls,
                "total_fails": h.total_fails,
                "frozen": is_tool_frozen(name),
                "last_error": h.last_error,
            }
        )
    return items
