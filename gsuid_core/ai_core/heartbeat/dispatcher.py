"""统一主动消息网关（Unified Proactive Dispatcher）

C8（plans/agent_design_review.md 建议二）：Heartbeat 定时巡检与定时任务播报
共用同一 APScheduler 实例（`gsuid_core.aps.scheduler`），但彼此完全不感知，
可能在数分钟内连发两条互不相关的主动消息（如"任务进度 + 闲聊打招呼"），
严重破坏拟人感。

本网关作为框架层的"主动输出协调器"——所有主动发送（Heartbeat / 定时任务）
发送后都向网关登记；Heartbeat 触发时向网关查询：

1. **防撞车**：同一目标在 ``MIN_GAP_SECONDS`` 内刚有任意主动输出 → 抑制本次
   巡检发言，避免双重打扰。
2. **合并语境**：取出 ``MERGE_WINDOW_SECONDS`` 窗口内的定时任务结果摘要，作为
   ``context_hook`` 合并进 Heartbeat 的决策 / 发言提示词，让 AI 自然地把任务
   进展融进闲聊（"趁刚睡醒看了眼，按计划平仓啦~"），而非生硬地另起一条播报。

网关只做协调，位于框架层，**不暴露为 LLM 工具**（建议八）。
状态为纯内存，进程重启即清空——属"重启即重置"内容，无需向后兼容。
"""

import re
import time
from typing import Dict, List, Tuple, Optional

# 防撞车最小间隔：窗口内有任意主动输出则抑制 Heartbeat 发言
MIN_GAP_SECONDS = 120
# 合并窗口：窗口内的定时任务结果可作为 Heartbeat 的 context_hook 合并
MERGE_WINDOW_SECONDS = 300

# C-3 主动发言频率硬上限（仅约束 Heartbeat 主动闲聊，不约束 task/kanban 关键播报）
MAX_PROACTIVE_PER_HOUR = 1
MAX_PROACTIVE_PER_DAY = 5
_HOUR_SECONDS = 3600
_DAY_SECONDS = 86400


def ngram_jaccard(a: str, b: str, n: int = 3) -> float:
    """字符 n-gram Jaccard；空串为 0。"""
    ta = re.sub(r"\s+", "", a or "")
    tb = re.sub(r"\s+", "", b or "")
    if not ta or not tb:
        return 0.0
    ga = {ta[i : i + n] for i in range(max(1, len(ta) - n + 1))} if len(ta) >= n else {ta}
    gb = {tb[i : i + n] for i in range(max(1, len(tb) - n + 1))} if len(tb) >= n else {tb}
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    union = len(ga | gb)
    return float(inter) / float(union) if union else 0.0


def texts_too_similar(a: str, b: str, *, threshold: float = 0.45) -> bool:
    return ngram_jaccard(a, b) >= threshold


def make_target_key(group_id: Optional[str], user_id: Optional[str]) -> str:
    """主动消息网关的统一目标键：群聊用 group_id，私聊用 user_id。

    登记（emitter）、限流校验（inspector）、计数注入（decision）三处必须用同一口径，
    否则计数/限流对不上号。此处只依赖原始字段（不引入 Event 类型），保持 dispatcher
    无 gsuid 依赖、可被冒烟测试独立加载。
    """
    if group_id:
        return str(group_id)
    if user_id:
        return str(user_id)
    return ""


class UnifiedProactiveDispatcher:
    """主动消息协调网关（单进程内存单例）。"""

    def __init__(self) -> None:
        # target_key -> (timestamp, source)；source: "heartbeat" | "task"
        self._last_send: Dict[str, Tuple[float, str]] = {}
        # target_key -> 待合并的定时任务结果摘要
        self._pending_merge: Dict[str, str] = {}
        # C-3：target_key -> Heartbeat 主动发言时间戳历史（用于按小时/天限流）
        self._heartbeat_history: Dict[str, List[float]] = {}
        # target_key -> 最近主动正文（语义去重）
        self._recent_texts: Dict[str, List[str]] = {}

    def register_send(self, target_key: str, source: str, summary: str = "") -> None:
        """登记一次主动发送。

        Args:
            target_key: 目标标识（群号或用户号）
            source:     来源，"heartbeat" 或 "task"
            summary:    heartbeat 近窗去重正文；task 则为合并语境摘要
        """
        if not target_key:
            return
        now = time.time()
        self._last_send[target_key] = (now, source)
        if source == "task" and summary:
            self._pending_merge[target_key] = summary.strip()[:500]
        # 仅记录 Heartbeat 主动闲聊到频率历史，并顺手裁掉 1 天前的旧记录
        if source == "heartbeat":
            hist = [t for t in self._heartbeat_history[target_key]] if target_key in self._heartbeat_history else []
            hist = [t for t in hist if now - t < _DAY_SECONDS]
            hist.append(now)
            self._heartbeat_history[target_key] = hist
            if summary.strip():
                self.remember_heartbeat_text(target_key, summary)

    def should_suppress_heartbeat(self, target_key: str) -> bool:
        """同一目标在 MIN_GAP_SECONDS 内刚有主动输出时，抑制本次 Heartbeat 发言。"""
        if not target_key or target_key not in self._last_send:
            return False
        ts, _source = self._last_send[target_key]
        return (time.time() - ts) < MIN_GAP_SECONDS

    def get_recent_heartbeat_count(self, target_key: str, window_seconds: float = _HOUR_SECONDS) -> int:
        """统计 target_key 在最近 window_seconds 内的 Heartbeat 主动发言次数（C-5 供决策注入）。"""
        if not target_key or target_key not in self._heartbeat_history:
            return 0
        now = time.time()
        return sum(1 for t in self._heartbeat_history[target_key] if now - t < window_seconds)

    def should_suppress_by_rate(self, target_key: str) -> bool:
        """C-3 频率硬上限：超过每小时 / 每天主动发言次数则抑制本次 Heartbeat。"""
        if not target_key:
            return False
        if self.get_recent_heartbeat_count(target_key, _HOUR_SECONDS) >= MAX_PROACTIVE_PER_HOUR:
            return True
        if self.get_recent_heartbeat_count(target_key, _DAY_SECONDS) >= MAX_PROACTIVE_PER_DAY:
            return True
        return False

    def _repeat_window(self, window: Optional[int]) -> int:
        if window is not None and window > 0:
            return window
        from gsuid_core.ai_core.configs.ai_config import ai_config

        n = int(ai_config.get_config("heartbeat_repeat_window").data)
        return n if n > 0 else 8

    def remember_heartbeat_text(self, target_key: str, text: str, *, window: Optional[int] = None) -> None:
        if not target_key:
            return
        body = text.strip()
        if not body:
            return
        cap = self._repeat_window(window)
        hist = list(self._recent_texts[target_key]) if target_key in self._recent_texts else []
        hist.append(body)
        self._recent_texts[target_key] = hist[-max(1, cap) :]

    def would_repeat_heartbeat(self, target_key: str, text: str, *, window: Optional[int] = None) -> bool:
        if not target_key or not text.strip():
            return False
        cap = self._repeat_window(window)
        hist = self._recent_texts[target_key] if target_key in self._recent_texts else []
        recent = hist[-max(1, cap) :]
        for prev in recent:
            if texts_too_similar(prev, text):
                return True
        return False

    def peek_merge_context(self, target_key: str) -> str:
        """读合并语境但不弹出；过期则清掉。开口成功后再 consume。"""
        if not target_key or target_key not in self._pending_merge:
            return ""
        summary = self._pending_merge[target_key]
        if not summary or target_key not in self._last_send:
            return ""
        ts, _source = self._last_send[target_key]
        if (time.time() - ts) < MERGE_WINDOW_SECONDS:
            return summary
        self._pending_merge.pop(target_key, None)
        return ""

    def consume_merge_context(self, target_key: str) -> str:
        """取出并清除窗口内待合并的定时任务结果摘要。

        供 Heartbeat 开口成功后丢弃已用过的合并语境。超出合并窗口则视为过期。
        """
        if not target_key or target_key not in self._pending_merge:
            return ""
        summary = self._pending_merge.pop(target_key)
        if not summary or target_key not in self._last_send:
            return ""
        ts, _source = self._last_send[target_key]
        if (time.time() - ts) < MERGE_WINDOW_SECONDS:
            return summary
        return ""


_dispatcher = UnifiedProactiveDispatcher()


def get_dispatcher() -> UnifiedProactiveDispatcher:
    """获取统一主动消息网关单例。"""
    return _dispatcher
