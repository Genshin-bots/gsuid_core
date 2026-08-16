"""统一句柄解析：res_ / to_ / sa_ / img_ / dlg_ / kb_plugin / kb_kbdoc → 可读载荷。

``dlg_`` 是在途委派句柄（见 ``ai_core/control/delegation.py``）：接进本命名空间后，
模型用已在保底池的 ``read_handle`` 就能查委派状态，无需新增工具（INV-5）。
``kb_plugin`` 读插件注册表正文；``kb_kbdoc`` 按 doc_id 拼接 SQL 分片。公共知识无 owner。
"""

from __future__ import annotations

from typing import Any, Literal, Optional
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


HandleKind = Literal["tool_output", "artifact", "image", "delegation", "knowledge", "unknown"]

_PREFIX_KINDS: tuple[tuple[str, HandleKind], ...] = (
    ("kb_plugin:", "knowledge"),
    ("kb_kbdoc:", "knowledge"),
    ("to_", "tool_output"),
    ("sa_", "tool_output"),
    ("res_", "artifact"),
    ("img_", "image"),
    ("dlg_", "delegation"),
)


def handle_kind_of(handle_id: str) -> HandleKind:
    """按前缀判别句柄种类（不查库）。"""
    hid = (handle_id or "").strip()
    for prefix, kind in _PREFIX_KINDS:
        if hid.startswith(prefix):
            return kind
    return "unknown"


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


def _from_knowledge(handle_id: str, text: str) -> ResolvedHandle:
    return ResolvedHandle(
        id=handle_id,
        source="knowledge",
        mime="text/plain",
        summary="",
        owner_user_id="",
        scope_key="",
        payload_inline=text,
        payload_path="",
        size_bytes=len(text.encode("utf-8")),
    )


def _plugin_article_text(entity_id: str) -> Optional[str]:
    from gsuid_core.ai_core.register import _ENTITIES

    for item in _ENTITIES:
        if not isinstance(item, dict) or "id" not in item or "title" not in item:
            continue
        if str(item["id"]) != entity_id:
            continue
        if "content" not in item:
            return ""
        return str(item["content"])
    return None


async def _kbdoc_article_text(doc_id: str) -> Optional[str]:
    from gsuid_core.ai_core.database.models import AIKnowledgeChunk

    rows, _total = await AIKnowledgeChunk.list_page(source="all", doc_id=doc_id, offset=0, limit=10000)
    usable = [r for r in rows if r.source in ("manual", "agent")]
    if not usable:
        return None
    usable.sort(key=lambda r: int(r.chunk_index))
    return "\n".join(r.content for r in usable)


async def resolve_handle(handle_id: str) -> Optional[ResolvedHandle]:
    hid = (handle_id or "").strip()
    if not hid:
        return None
    if handle_kind_of(hid) == "delegation":
        return None
    if hid.startswith("kb_plugin:"):
        text = _plugin_article_text(hid[len("kb_plugin:") :])
        if text is None:
            return None
        return _from_knowledge(hid, text)
    if hid.startswith("kb_kbdoc:"):
        text = await _kbdoc_article_text(hid[len("kb_kbdoc:") :])
        if text is None:
            return None
        return _from_knowledge(hid, text)
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
    limit: int = 8000,
) -> str:
    if resolved.source == "image" or resolved.mime.startswith("image/"):
        return (
            f"handle {resolved.id} | kind=image | mime={resolved.mime}\n"
            f"summary: {resolved.summary}\n"
            "→ send_message_by_ai(image_id=本id) 直发；禁止当文本全文朗读。"
        )
    if resolved.source == "knowledge":
        text = resolved.payload_inline or ""
        if not text:
            return f"handle {resolved.id} | kind=knowledge\n（无内容）"
        return format_paginated_body(
            head=f"handle {resolved.id} | kind=knowledge\n",
            text=text,
            offset=offset,
            limit=limit,
            read_hint="read_handle(handle_id, offset, limit)",
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
