"""Provider 并发/故障路由器。

为 high/low 两档任务提供「主配置 + 备用(2nd)配置」的运行期路由：

- **并发兜底**：每个配置文件有 ``max_concurrency``（默认 1，最大 10）。主配置在飞请求
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
# 两个配置都满时的轮询间隔
_POLL_INTERVAL = 0.1


class _Slot:
    __slots__ = ("in_flight", "unavailable_until")

    def __init__(self) -> None:
        self.in_flight: int = 0
        self.unavailable_until: float = 0.0


class ProviderRouter:
    def __init__(self) -> None:
        self._slots: Dict[str, _Slot] = {}

    def _slot(self, full_name: str) -> _Slot:
        if full_name not in self._slots:
            self._slots[full_name] = _Slot()
        return self._slots[full_name]

    def is_available(self, full_name: str) -> bool:
        return time.time() >= self._slot(full_name).unavailable_until

    def has_capacity(self, full_name: str) -> bool:
        return self._slot(full_name).in_flight < get_max_concurrency_for_config(full_name)

    def mark_failure(self, full_name: str, cooldown: float = FAILURE_COOLDOWN_SECONDS) -> None:
        """标记配置故障（限流/连接错误等），冷却期内不再路由新请求到它"""
        if not full_name:
            return
        self._slot(full_name).unavailable_until = time.time() + cooldown
        logger.warning(
            t(
                "🧠 [ProviderRouter] 配置 {full_name} 标记为不可用，冷却 {cooldown:.0f}s",
                full_name=full_name,
                cooldown=cooldown,
            )
        )

    def mark_success(self, full_name: str) -> None:
        """请求成功即解除冷却（provider 已恢复，不必等满冷却期）"""
        if full_name in self._slots:
            self._slots[full_name].unavailable_until = 0.0

    def _pick(self, primary: str, secondary: str) -> str:
        """按 可用性 → 剩余容量 选路；都满返回空串（调用方轮询等待）。"""
        p_ok = self.is_available(primary)
        s_ok = bool(secondary) and self.is_available(secondary)
        if p_ok and self.has_capacity(primary):
            return primary
        if s_ok and self.has_capacity(secondary):
            return secondary
        # 主配置冷却且备用没容量（或没配备用）：若主也没容量则等待；
        # 两者都冷却时选主兜底（best-effort，总得有人接请求）
        if not p_ok and not s_ok:
            return primary if self.has_capacity(primary) else ""
        return ""

    async def acquire(self, task_level: Literal["high", "low"], timeout: float = 300.0) -> str:
        """为一次 LLM 请求选路并占用一个并发槽位，返回选中的配置全名。

        主配置可用且有并发余量 → 主；否则备用配置可用且有余量 → 备用；
        都满则轮询等待（上限 ``timeout`` 秒，超时强制走主配置，避免死等）。
        """
        primary = get_config_name_for_task(task_level)
        secondary = get_2nd_config_name_for_task(task_level)
        if not primary:
            return primary  # 未配置模型，交由上层报错
        deadline = time.time() + timeout
        while True:
            chosen = self._pick(primary, secondary)
            if chosen:
                slot = self._slot(chosen)
                slot.in_flight += 1
                if chosen != primary:
                    logger.info(
                        t(
                            "🧠 [ProviderRouter] {task_level} 级请求路由至备用配置"
                            " {chosen} (主配置 {primary} 并发满/冷却中)",
                            task_level=task_level,
                            chosen=chosen,
                            primary=primary,
                        )
                    )
                return chosen
            if time.time() >= deadline:
                slot = self._slot(primary)
                slot.in_flight += 1
                logger.warning(t("🧠 [ProviderRouter] 等待槽位超时，强制走主配置 {primary}", primary=primary))
                return primary
            await asyncio.sleep(_POLL_INTERVAL)

    def release(self, full_name: str) -> None:
        if full_name in self._slots and self._slots[full_name].in_flight > 0:
            self._slots[full_name].in_flight -= 1

    @asynccontextmanager
    async def slot(self, task_level: Literal["high", "low"]):
        """异步上下文管理器：``async with provider_router.slot("low") as full_name:``"""
        full_name = await self.acquire(task_level)
        try:
            yield full_name
        finally:
            self.release(full_name)


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
        )
    ) or ("用量上限" in err_text or "额度" in err_text)
