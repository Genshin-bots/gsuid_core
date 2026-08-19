"""前缀缓存失配探针：对比相邻 run 的 history hash 链，标注首个失配类别。

类别：``none`` / ``system`` / ``tools`` / ``user_lean`` / ``tool_return`` / ``history_mid``。
只观测、不改请求。结果进 session log 与进程内计数，供缓存治理验收。
"""

from __future__ import annotations

import hashlib
from typing import List, Literal, Optional, Sequence
from dataclasses import dataclass

from pydantic_ai.messages import (
    TextPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
)

PrefixBreakReason = Literal["none", "system", "tools", "user_lean", "tool_return", "history_mid"]

_PREFIX_BREAK_COUNTS: dict[str, int] = {}


@dataclass
class PrefixSnapshot:
    """上一 run 发送时的前缀指纹。"""

    history_hashes: List[str]
    tools_hash: str
    system_hash: str
    payloads: List[str]


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def hash_system_prompt(system_prompt: str) -> str:
    return hash_text(system_prompt or "")


def hash_tool_names(names: Sequence[str]) -> str:
    return hash_text("\n".join(names))


def _part_payload(part: object) -> str:
    if isinstance(part, UserPromptPart):
        content = part.content
        if isinstance(content, str):
            return f"user:{content}"
        return f"user:{str(content)[:200]}"
    if type(part) is ToolReturnPart:
        body = part.content if isinstance(part.content, str) else str(part.content)
        return f"tool_return:{part.tool_name}:{body}"
    if isinstance(part, TextPart):
        return f"text:{part.content}"
    return f"{type(part).__name__}:{str(part)[:200]}"


def hash_history_messages(messages: Sequence[ModelMessage], limit: int = 32) -> List[str]:
    """头 N 条消息的稳定 hash 链（只看内容，不看对象 id）。"""
    out: List[str] = []
    for msg in messages[:limit]:
        if isinstance(msg, ModelRequest):
            blob = "|".join(_part_payload(p) for p in msg.parts)
            kind = "req"
        elif isinstance(msg, ModelResponse):
            blob = "|".join(_part_payload(p) for p in msg.parts)
            kind = "resp"
        else:
            blob = str(msg)[:400]
            kind = type(msg).__name__
        out.append(hash_text(f"{kind}:{blob}"))
    return out


def _looks_like_lean_vs_full(prev_payload: str, curr_payload: str) -> bool:
    """上一轮 full user（含动态块）被换成 lean 时，两边都是 user: 前缀且一边明显更短。"""
    if not prev_payload.startswith("user:") or not curr_payload.startswith("user:"):
        return False
    a, b = prev_payload[5:], curr_payload[5:]
    if a == b:
        return False
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    if len(longer) < 80:
        return False
    return len(shorter) * 2 < len(longer) and (
        "[历史对话]" in longer or "[长期记忆" in longer or "【当前群聊语境】" in longer
    )


def classify_prefix_break(
    prev: Optional[PrefixSnapshot],
    *,
    history_hashes: Sequence[str],
    tools_hash: str,
    system_hash: str,
    prev_payloads: Sequence[str] = (),
    curr_payloads: Sequence[str] = (),
) -> PrefixBreakReason:
    if prev is None:
        return "none"
    if prev.system_hash != system_hash:
        return "system"
    if prev.tools_hash != tools_hash:
        return "tools"
    n = min(len(prev.history_hashes), len(history_hashes))
    for i in range(n):
        if prev.history_hashes[i] == history_hashes[i]:
            continue
        if i < len(prev_payloads) and i < len(curr_payloads):
            if _looks_like_lean_vs_full(prev_payloads[i], curr_payloads[i]):
                return "user_lean"
            if prev_payloads[i].startswith("tool_return:") or curr_payloads[i].startswith("tool_return:"):
                return "tool_return"
        return "history_mid"
    if len(prev.history_hashes) != len(history_hashes):
        return "history_mid"
    return "none"


def record_prefix_break(reason: PrefixBreakReason) -> None:
    _PREFIX_BREAK_COUNTS[reason] = _PREFIX_BREAK_COUNTS.get(reason, 0) + 1


def get_prefix_break_counts() -> dict[str, int]:
    return dict(_PREFIX_BREAK_COUNTS)


def reset_prefix_break_counts() -> None:
    _PREFIX_BREAK_COUNTS.clear()


def history_payloads(messages: Sequence[ModelMessage], limit: int = 32) -> List[str]:
    out: List[str] = []
    for msg in messages[:limit]:
        if isinstance(msg, ModelRequest):
            out.append("|".join(_part_payload(p) for p in msg.parts))
        elif isinstance(msg, ModelResponse):
            out.append("|".join(_part_payload(p) for p in msg.parts))
        else:
            out.append(str(msg)[:400])
    return out
