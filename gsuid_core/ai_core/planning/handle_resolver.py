"""统一句柄解析：res_ / to_ / sa_ / img_ → 可读载荷。"""

from __future__ import annotations

from typing import Any, Optional
from dataclasses import dataclass

from gsuid_core.ai_core.planning.tool_output_store import AIToolOutputRecord
from gsuid_core.ai_core.planning.tool_output_protocol import (
    load_payload_text,
    format_paginated_body,
)


@dataclass(frozen=True)
class ResolvedHandle:
    id: str
    source: str  # tool_output | artifact | image | unknown
    mime: str
    summary: str
    owner_user_id: str
    scope_key: str
    payload_inline: Optional[str]
    payload_path: str
    size_bytes: int
    root_task_id: str = ""
    task_id: str = ""


def _mime_for_tool_output(payload_path: str) -> str:
    return "text/markdown" if (payload_path or "").endswith(".md") else "text/plain"


def _from_tool_output(rec: AIToolOutputRecord) -> ResolvedHandle:
    path = rec.payload_path or ""
    return ResolvedHandle(
        id=rec.id,
        source="tool_output",
        mime=_mime_for_tool_output(path),
        summary=rec.summary or "",
        owner_user_id=rec.owner_user_id or "",
        scope_key=rec.scope_key or "",
        payload_inline=rec.payload_inline,
        payload_path=path,
        size_bytes=rec.size_bytes,
        root_task_id=rec.root_task_id or "",
        task_id=rec.task_id or "",
    )


async def _from_artifact(art: Any) -> ResolvedHandle:
    from gsuid_core.ai_core.planning.models import AIAgentTask

    mime = art.mime or "text/plain"
    src = "image" if mime.startswith("image/") else "artifact"
    size = 0
    if art.payload_inline:
        size = len(art.payload_inline.encode("utf-8"))
    owner = ""
    scope = ""
    # owner/scope 落在任务节点上，供 ACL 与跨树校验
    task = await AIAgentTask.get_by_id(art.root_task_id or art.task_id)
    if task is not None:
        owner = task.owner_user_id or ""
        scope = task.scope_key or ""
    return ResolvedHandle(
        id=art.id,
        source=src,
        mime=mime,
        summary=art.summary or "",
        owner_user_id=owner,
        scope_key=scope,
        payload_inline=art.payload_inline,
        payload_path=art.payload_path or "",
        size_bytes=size,
        root_task_id=art.root_task_id or "",
        task_id=art.task_id or "",
    )


async def resolve_handle(handle_id: str) -> Optional[ResolvedHandle]:
    hid = (handle_id or "").strip()
    if not hid:
        return None
    # FileOS
    if hid.startswith("to_") or hid.startswith("sa_"):
        rec = await AIToolOutputRecord.get_by_id(hid)
        if rec is None:
            return None
        return _from_tool_output(rec)
    # Artifact / image res_
    if hid.startswith("res_") or hid.startswith("img_"):
        from gsuid_core.ai_core.planning.models import AIAgentArtifact

        art = await AIAgentArtifact.get_by_id(hid)
        if art is None:
            return None
        return await _from_artifact(art)
    # 裸 id 先试 FileOS 再 artifact
    rec = await AIToolOutputRecord.get_by_id(hid)
    if rec is not None:
        return _from_tool_output(rec)
    from gsuid_core.ai_core.planning.models import AIAgentArtifact

    art = await AIAgentArtifact.get_by_id(hid)
    if art is None:
        return None
    return await _from_artifact(art)


def format_resolved(
    resolved: ResolvedHandle,
    *,
    offset: int = 0,
    limit: int = 12000,
) -> str:
    if resolved.source == "image" or resolved.mime.startswith("image/"):
        return (
            f"handle {resolved.id} | kind=image | mime={resolved.mime}\n"
            f"summary: {resolved.summary}\n"
            "→ send_message_by_ai(image_id=本id) 直发；禁止当文本全文朗读。"
        )
    head = f"handle {resolved.id} | kind={resolved.source} | mime={resolved.mime}\nsummary: {resolved.summary}\n"
    text, err = load_payload_text(
        payload_inline=resolved.payload_inline,
        payload_path=resolved.payload_path,
    )
    if err:
        return head + f"⚠️ {err}"
    if not text:
        return head + "（无内容）"
    return format_paginated_body(
        head=head,
        text=text,
        offset=offset,
        limit=limit,
        read_hint="read_handle(handle_id, offset, limit)",
    )
