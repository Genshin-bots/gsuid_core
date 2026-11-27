"""Provider 并发/故障路由器。

为 high/low 两档任务提供「主配置 + 备用(2nd)配置」的运行期路由：

- **并发兜底**：每个配置文件有 ``max_concurrency``（默认 1，最大 12）。主配置在飞请求
  占满并发后，新请求自动路由到备用配置——两个 provider 可以同时跑，实现负载均衡。
- **故障切换**：请求在某配置上命中限流/连接类错误时，调用方通过 :func:`mark_failure`
  给该配置一段冷却期（期间新请求不再路由到它）；冷却结束自动恢复。
- **不可用兜底**：主配置冷却中则直接走备用配置；两者都满/都冷却时，短轮询等待任一
  配置释放槽位（避免无界排队把上游打爆）。

槽位用进程内计数器实现（非 Semaphore），因为并发上限可被网页控制台热改，需要每次
acquire 时重新读配置。所有状态仅存内存，重启即清零，天然与配置热切换兼容。
"""

from __future__ import annotations

import time
import asyncio
from typing import Dict, Literal
from weakref import WeakKeyDictionary
from contextlib import asynccontextmanager

from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .models import (
    get_config_name_for_task,
    get_2nd_config_name_for_task,
    get_max_concurrency_for_config,
)

# 命中故障后的默认冷却秒数：期间该配置不参与路由（到期自动恢复）
FAILURE_COOLDOWN_SECONDS = 60.0
# 前台交互 / 后台子任务默认等槽超时（秒）
INTERACTIVE_SLOT_TIMEOUT_SECONDS = 60.0
BACKGROUND_SLOT_TIMEOUT_SECONDS = 120.0

PriorityLane = Literal["interactive", "background"]

_INTERACTIVE_DIRECT_CREATE_BY: frozenset[str] = frozenset({"Chat", "Agent", "TEST", "Kanban_Relay"})


def priority_for_create_by(create_by: str, *, is_subagent: bool = False) -> PriorityLane:
    """按调用来源划分并发车道：前台群聊/私聊与分诊路由优先，后台子代理/巡检退让。"""
    if create_by == "TurnRouter":
        return "interactive"
    if not is_subagent and create_by in _INTERACTIVE_DIRECT_CREATE_BY:
        return "interactive"
    return "background"


class _Slot:
    __slots__ = (
        "in_flight_interactive",
        "in_flight_background",
        "waiting_interactive",
        "unavailable_until",
        "_conds",
    )

    def __init__(self) -> None:
        self.in_flight_interactive: int = 0
        self.in_flight_background: int = 0
        self.waiting_interactive: int = 0
        self.unavailable_until: float = 0.0
        self._conds: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Condition] = WeakKeyDictionary()

    @property
    def in_flight(self) -> int:
        return self.in_flight_interactive + self.in_flight_background

    def condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        cond = self._conds.get(loop)
        if cond is None:
            cond = asyncio.Condition()
            self._conds[loop] = cond
        return cond


def _sticky_key(session_id: str | None) -> str:
    """提取会话的粘性路由键，使同群/同任务/同能力的请求保持在同一 provider。"""
    if not session_id:
        return ""
    if session_id.startswith("heartbeat_"):
        parts = session_id.split("_")
        return f"heartbeat_{parts[2]}_{parts[3]}" if len(parts) >= 4 else parts[0]
    if session_id.startswith("capagent_"):
        parts = session_id.split("_")
        return "_".join(parts[:5]) if len(parts) >= 5 else session_id
    if session_id.startswith("auto_"):
        parts = session_id.split("_")
        return f"{parts[1]}_{parts[2]}" if len(parts) >= 4 else (parts[1] if len(parts) >= 2 else session_id)
    return session_id


class ProviderRouter:
    def __init__(self) -> None:
        self._slots: Dict[str, _Slot] = {}
        self._session_sticky: Dict[str, str] = {}

    def _slot(self, full_name: str) -> _Slot:
        if full_name not in self._slots:
            self._slots[full_name] = _Slot()
        return self._slots[full_name]

    def is_available(self, full_name: str) -> bool:
        return time.time() >= self._slot(full_name).unavailable_until

    def has_capacity(self, full_name: str, priority: PriorityLane = "interactive") -> bool:
        return self._can_acquire(self._slot(full_name), get_max_concurrency_for_config(full_name), priority)

    @staticmethod
    def _can_acquire(slot: _Slot, cap: int, priority: PriorityLane) -> bool:
        limit = max(1, cap)
        if priority == "interactive":
            return slot.in_flight < limit
        bg_cap = max(1, limit - 1)
        return slot.in_flight < limit and slot.in_flight_background < bg_cap and slot.waiting_interactive == 0

    def mark_failure(self, full_name: str, cooldown: float = FAILURE_COOLDOWN_SECONDS) -> None:
        """标记配置故障（限流/连接错误等），冷却期内不再路由新请求到它"""
        if not full_name:
            return
        self._slot(full_name).unavailable_until = time.time() + cooldown
        logger.warning(
            t(
                "log.ai.providerrouter_config_full_name_fail",
                full_name=full_name,
                cooldown=cooldown,
            )
        )

    def mark_success(self, full_name: str) -> None:
        """请求成功即解除冷却（provider 已恢复，不必等满冷却期）"""
        if full_name in self._slots:
            self._slots[full_name].unavailable_until = 0.0

    def _pick(
        self,
        primary: str,
        secondary: str,
        session_id: str | None = None,
        priority: PriorityLane = "interactive",
    ) -> str:
        """按 粘性/可用性 → 剩余容量 选路；都满返回空串。"""
        key = _sticky_key(session_id)
        if key and key in self._session_sticky:
            preferred = self._session_sticky[key]
            if preferred in (primary, secondary):
                if self.is_available(preferred) and self.has_capacity(preferred, priority):
                    return preferred

        p_ok = self.is_available(primary)
        s_ok = bool(secondary) and self.is_available(secondary)
        if p_ok and self.has_capacity(primary, priority):
            return primary
        if s_ok and self.has_capacity(secondary, priority):
            return secondary
        if not p_ok and not s_ok:
            return primary if self.has_capacity(primary, priority) else ""
        return ""

    def pick_route(
        self,
        task_level: Literal["high", "low"],
        session_id: str | None = None,
        task_kind: str | None = None,
        priority: PriorityLane = "interactive",
    ) -> str:
        """非阻塞选路：为一次 run 选定目标 provider（并发占槽下沉到单次请求）。"""
        primary = get_config_name_for_task(task_level, task_kind)
        secondary = get_2nd_config_name_for_task(task_level, task_kind)
        if not primary:
            return primary
        chosen = self._pick(primary, secondary, session_id=session_id, priority=priority)
        if not chosen:
            p_ok = self.is_available(primary)
            s_ok = bool(secondary) and secondary != primary and self.is_available(secondary)
            if s_ok and not p_ok:
                chosen = secondary
            elif p_ok and s_ok:
                p_load = self._slot(primary).in_flight / max(1, get_max_concurrency_for_config(primary))
                s_load = self._slot(secondary).in_flight / max(1, get_max_concurrency_for_config(secondary))
                chosen = secondary if s_load < p_load else primary
            else:
                chosen = primary
        sticky_k = _sticky_key(session_id)
        if sticky_k:
            if len(self._session_sticky) >= 2000:
                self._session_sticky.clear()
            self._session_sticky[sticky_k] = chosen
        if chosen != primary:
            logger.info(
                t(
                    "log.ai.providerrouter_task_level_chosen",
                    task_level=task_level,
                    chosen=chosen,
                    primary=primary,
                )
            )
        return chosen

    async def acquire_request_slot(
        self,
        full_name: str,
        *,
        priority: PriorityLane = "interactive",
        timeout: float | None = None,
    ) -> bool:
        """为单次 LLM 请求申请并发槽；前台优先，后台受 max(1, cap-1) 上限约束。"""
        if not full_name:
            return False
        wait_limit = (
            timeout
            if timeout is not None
            else (INTERACTIVE_SLOT_TIMEOUT_SECONDS if priority == "interactive" else BACKGROUND_SLOT_TIMEOUT_SECONDS)
        )
        slot = self._slot(full_name)
        cond = slot.condition()
        deadline = time.monotonic() + max(0.0, wait_limit)
        async with cond:
            if priority == "interactive":
                slot.waiting_interactive += 1
            try:
                while True:
                    cap = get_max_concurrency_for_config(full_name)
                    if self._can_acquire(slot, cap, priority):
                        if priority == "interactive":
                            slot.in_flight_interactive += 1
                        else:
                            slot.in_flight_background += 1
                        return True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if priority == "interactive":
                            slot.in_flight_interactive += 1
                        else:
                            slot.in_flight_background += 1
                        logger.warning(t("log.ai.providerrouter_wait_slot_timeout_fail", primary=full_name))
                        return True
                    try:
                        await asyncio.wait_for(cond.wait(), timeout=min(remaining, 1.0))
                    except asyncio.TimeoutError:
                        continue
            finally:
                if priority == "interactive":
                    slot.waiting_interactive -= 1
                    if slot.waiting_interactive == 0:
                        cond.notify_all()

    async def release_request_slot(self, full_name: str, *, priority: PriorityLane = "interactive") -> None:
        if not full_name or full_name not in self._slots:
            return
        slot = self._slots[full_name]
        cond = slot.condition()
        async with cond:
            if priority == "interactive":
                if slot.in_flight_interactive > 0:
                    slot.in_flight_interactive -= 1
            elif slot.in_flight_background > 0:
                slot.in_flight_background -= 1
            cond.notify_all()

    @asynccontextmanager
    async def request_slot(
        self,
        full_name: str,
        *,
        priority: PriorityLane = "interactive",
        timeout: float | None = None,
    ):
        """单次 LLM 请求级并发槽上下文管理器（工具执行/渲染期间不占槽）。"""
        acquired = await self.acquire_request_slot(full_name, priority=priority, timeout=timeout)
        try:
            yield full_name
        finally:
            if acquired:
                await self.release_request_slot(full_name, priority=priority)

    async def acquire(
        self,
        task_level: Literal["high", "low"],
        session_id: str | None = None,
        timeout: float = INTERACTIVE_SLOT_TIMEOUT_SECONDS,
        task_kind: str | None = None,
        priority: PriorityLane = "interactive",
    ) -> str:
        """兼容旧调用：选路并占用一个并发槽位。"""
        chosen = self.pick_route(task_level, session_id=session_id, task_kind=task_kind, priority=priority)
        if not chosen:
            return chosen
        await self.acquire_request_slot(chosen, priority=priority, timeout=timeout)
        return chosen

    def release(self, full_name: str, priority: PriorityLane | None = None) -> None:
        """同步兼容释放（供旧外部插件使用；异步调用方请用 request_slot，等待者至多 1s 内唤醒）。"""
        if full_name not in self._slots:
            return
        slot = self._slots[full_name]
        if priority == "interactive":
            if slot.in_flight_interactive > 0:
                slot.in_flight_interactive -= 1
        elif priority == "background":
            if slot.in_flight_background > 0:
                slot.in_flight_background -= 1
        elif slot.in_flight_interactive > 0:
            slot.in_flight_interactive -= 1
        elif slot.in_flight_background > 0:
            slot.in_flight_background -= 1

    @asynccontextmanager
    async def slot(
        self,
        task_level: Literal["high", "low"],
        session_id: str | None = None,
        task_kind: str | None = None,
        priority: PriorityLane = "interactive",
    ):
        """兼容上下文管理器：选路并在块生命周期内占槽。"""
        chosen = self.pick_route(task_level, session_id=session_id, task_kind=task_kind, priority=priority)
        async with self.request_slot(chosen, priority=priority):
            yield chosen


# 全局单例
provider_router = ProviderRouter()


def looks_like_provider_failure(err_text: str) -> bool:
    """粗判错误文本是否为 provider 级故障（限流/额度/连接），供调用方决定是否 mark_failure"""
    low = err_text.lower()
    return any(
        k in low
        for k in (
            "429",
            "rate_limit",
            "rate limit",
            "too many request",
            "connection",
            "timed out",
            "timeout",
            "502",
            "503",
            "unavailable",
            "quota",
        )
    ) or any(k in err_text for k in ("用量上限", "用量已达上限", "额度", "配额", "超时"))
