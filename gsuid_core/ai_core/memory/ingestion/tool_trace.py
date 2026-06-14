"""程序性记忆：近期工具调用轨迹的轻量记录（gated / bounded / best-effort）。

偏好蒸馏（设计 §4.2）需要"上一轮 Agent 实际工具调用 + 关键参数"作为背景，才能把
用户那句"你参数传错了"蒸成"调用 generate_image 时 orientation 应为 portrait"这种带具体
参数的规则。但工具调用轨迹在 gs_agent 的执行图里（CallToolsNode 的 ToolCallPart），
**不在 observe() 摄入管道**。本模块提供一个按 user_id 分桶的有界 ring buffer：

- gs_agent 在 CallToolsNode 命中工具时（**仅 enable_preference_memory 开启时**）记一笔；
- 偏好蒸馏（worker._extract_and_upsert_preferences）读取最近若干笔作为背景上下文。

纯内存、零持久化、有界（每用户最多 N 笔 + TTL 过期），关闭偏好记忆时完全不被写入。
"""

import time
from typing import Any, NamedTuple
from collections import deque

# 每用户保留最近 N 笔工具调用
_MAX_PER_USER = 8
# 超过此秒数的旧记录视为过期，不再用作背景（避免把很久前的调用误当"上一轮"）
_TTL_SECONDS = 1800
# 单笔参数摘要字符上限，防 Token 膨胀
_ARGS_MAX_CHARS = 300
# 全局用户桶上限，防内存无界增长（超限时丢弃最早被写入的用户桶）
_MAX_USERS = 512


class ToolCallRecord(NamedTuple):
    """单笔工具调用轨迹（ring buffer 元素）。

    用 NamedTuple 而非裸 tuple，让``record_tool_call`` / ``get_recent_tool_calls``
    的字段访问自文档化（``rec.tool_name`` 而非 ``rec[1]``）。
    """

    timestamp: float  # 调用时刻（time.time()）
    tool_name: str
    args_summary: str  # 截断后的参数摘要（≤ _ARGS_MAX_CHARS）


# {user_id: deque[ToolCallRecord]}
_recent: dict[str, deque[ToolCallRecord]] = {}


def record_tool_call(user_id: str, tool_name: str, args: Any) -> None:
    """记录一笔工具调用（best-effort，绝不抛出）。"""
    if not user_id or not tool_name:
        return
    try:
        args_str = str(args)
        if len(args_str) > _ARGS_MAX_CHARS:
            args_str = args_str[:_ARGS_MAX_CHARS] + "...[截断]"
        if user_id not in _recent:
            if len(_recent) >= _MAX_USERS:
                # 丢弃最早插入的用户桶（dict 有序），保持有界
                oldest = next(iter(_recent))
                del _recent[oldest]
            _recent[user_id] = deque(maxlen=_MAX_PER_USER)
        _recent[user_id].append(ToolCallRecord(time.time(), tool_name, args_str))
    except Exception:
        pass


def get_recent_tool_calls(user_ids: list[str], limit: int = 6) -> list[str]:
    """取若干用户最近、未过期的工具调用摘要（新→旧），供偏好蒸馏作背景。

    返回形如 ``["generate_image(args={...})", ...]`` 的字符串列表（最多 limit 条）。
    """
    now = time.time()
    collected: list[tuple[float, str]] = []
    for uid in user_ids:
        bucket = _recent[uid] if uid in _recent else None
        if bucket is None:
            continue
        for rec in bucket:
            if now - rec.timestamp <= _TTL_SECONDS:
                collected.append((rec.timestamp, f"{rec.tool_name}(args={rec.args_summary})"))
    collected.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in collected[:limit]]
