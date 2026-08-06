"""FileOS / Artifact 统一句柄与分页读协议。

长结果进模型前只留短卡；全文经 ``read_handle`` / ``artifact_get`` 续读。
"""

from __future__ import annotations

from typing import Optional
from pathlib import Path
from dataclasses import dataclass


@dataclass(frozen=True)
class PersistedHandleCard:
    """模型可见的统一句柄卡（无全文）。"""

    id: str
    kind: str  # tool_output | artifact | image
    mime: str
    summary: str
    size_bytes: int
    read_tool: str = "read_handle"
    long_structured: bool = True

    def format(self) -> str:
        how = f"read_handle(handle_id={self.id!r}, offset=0, limit=12000)"
        lines = [
            f"[persisted id={self.id} kind={self.kind} mime={self.mime} size={self.size_bytes}]",
            f"summary: {self.summary[:200]}",
            f"how_to_read: {how}  # 续读增大 offset；禁止把全文念给用户",
        ]
        if self.long_structured:
            lines.append(
                "long_structured=true → 多项/长文请 create_subagent("
                'agent_profile="render_agent", task=本id或版式要求) 出图；短结论不必出图'
            )
        if self.mime.startswith("image/"):
            lines.append("image=true → send_message_by_ai(image_id=本id) 直发，勿 read_handle 当文本")
        return "\n".join(lines)


def format_paginated_body(
    *,
    head: str,
    text: str,
    offset: int = 0,
    limit: int = 12000,
    read_hint: str = "",
) -> str:
    """分页切片 + 统一续读提示。"""
    total = len(text)
    off = max(0, offset)
    lim = max(0, limit)
    sliced = text[off : off + lim]
    if total <= len(sliced) and off == 0:
        return head + f"payload:\n{sliced}"
    more = off + lim < total
    hint = ""
    if more and read_hint:
        hint = f"，{read_hint} offset={off + lim}"
    elif more:
        hint = f"，续读 offset={off + lim}"
    suffix = f"\n…[分页 {off}-{off + len(sliced)}/{total}{hint}]"
    if off > 0 and not more:
        suffix = f"\n[分页 {off}-{off + len(sliced)}/{total}]"
    return head + f"payload:\n{sliced}{suffix}"


def load_payload_text(
    *,
    payload_inline: Optional[str],
    payload_path: str,
) -> tuple[str, Optional[str]]:
    """返回 (text, error)。"""
    if payload_inline:
        return payload_inline, None
    if payload_path:
        try:
            return Path(payload_path).read_text(encoding="utf-8", errors="replace"), None
        except OSError as e:
            return "", f"读取落盘失败: {e}"
    return "", None


def rrf_fuse(
    ranked_lists: list[list[str]],
    *,
    k: int = 60,
    limit: int = 8,
) -> list[str]:
    """Reciprocal Rank Fusion：多路 id 列表融合。"""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, rid in enumerate(lst):
            if not rid:
                continue
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
    ordered = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return ordered[:limit]
