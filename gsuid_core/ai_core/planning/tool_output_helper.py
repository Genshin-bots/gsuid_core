"""落盘帮助：达标 ToolReturn / 子代理终态 → SQL + 可选折叠句柄卡。"""

from __future__ import annotations

import uuid
import asyncio
import hashlib
from typing import Any, Optional
from pathlib import Path
from datetime import datetime, timedelta

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.memory.scope import scope_key_for_conversation
from gsuid_core.ai_core.planning.models import AIAgentTask
from gsuid_core.ai_core.planning.workspace import ARTIFACT_ROOT
from gsuid_core.ai_core.planning.tool_output_store import AIToolOutputRecord
from gsuid_core.ai_core.planning.tool_output_metrics import fileos_metrics
from gsuid_core.ai_core.planning.tool_output_protocol import (
    PersistedHandleCard,
    extract_inline_head,
    extract_info_summary,
    extract_persist_title,
    looks_like_handle_card,
)
from gsuid_core.ai_core.planning.tool_output_sanitize import sanitize_for_persist

_MIN_PERSIST_CHARS = 800
_MIN_SEARCH_PERSIST_CHARS = 40
_SEARCH_ERROR_PREFIXES = ("错误：", "错误:", "error:")
_INLINE_MAX = 4096
_TTL_DAYS = 30
# 自适应折叠：私聊 / 群聊 / 能力代理
_FOLD_PRIVATE = 1200
_FOLD_GROUP = 900
# 折叠卡内嵌要点上限（仅私聊；群聊主人格只留 summary，不当事实总线）
_INLINE_HEAD_PRIVATE = 1400
# 只读/回读类：内容已在 artifact/FileOS 真身里，禁止再落一份 tool_output
_SKIP_PERSIST_TOOLS = frozenset(
    {
        "artifact_get",
        "artifact_get_recent",
        "artifact_list",
        "read_handle",
        "list_persisted_outputs",
        "grep_persisted_outputs",
        "search_cognition",  # 只读联邦检索，不落盘
        "read_image",  # 句柄读图，非新材料
        "list_my_kanban_tasks",
        "list_my_tasks",
        "search_handles",
        "search_persisted_outputs",
    }
)


def _tool_output_dir() -> Path:
    p = ARTIFACT_ROOT / "_tool_outputs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fold_threshold(*, is_group: bool = False) -> int:
    return _FOLD_GROUP if is_group else _FOLD_PRIVATE


def should_persist_tool_return(tool_name: str, content: str) -> bool:
    """结构门：过短 / 挂起 ack / 只读回读工具 / 已是句柄卡 不落盘。"""
    tn = (tool_name or "").strip()
    if tn in _SKIP_PERSIST_TOOLS:
        return False
    body = (content or "").strip()
    if looks_like_handle_card(body):
        return False
    if "后台执行" in body and "自动回灌" in body:
        return False
    if "仍在执行" in body:
        return False
    if is_searchish_tool(tn):
        lowered = body[:24].lower()
        if any(body.startswith(p) or lowered.startswith(p.lower()) for p in _SEARCH_ERROR_PREFIXES):
            return False
        return len(body) >= _MIN_SEARCH_PERSIST_CHARS
    if len(body) < _MIN_PERSIST_CHARS:
        return False
    return True


def should_fold_for_model(
    content: str,
    *,
    tool_name: str = "",
    is_group: bool = False,
) -> bool:
    """主人格折叠门：句柄卡 / 只读工具 / 过短不折。长委派回执同样折成卡。"""
    tn = (tool_name or "").strip()
    if tn in _SKIP_PERSIST_TOOLS:
        return False
    body = (content or "").strip()
    if looks_like_handle_card(body):
        return False
    return len(body) >= fold_threshold(is_group=is_group)


def _scope_from_ev(ev: Optional[Event]) -> tuple[str, str, str]:
    """返回 ``(owner, fileos_scope, cognition_scope)``。

    两套 scope 口径故意分开：FileOS 自己存裸 ``group_id`` / ``session_id``（历史口径，
    它的检索也用同一个值）；而认知节点表查的是 ``group:{gid}`` / ``user_global:{uid}``。
    过去把 FileOS 的裸值直接当节点 scope 写，节点永远匹配不上——表只涨不召回。
    """
    if ev is None:
        return "", "", ""
    owner = str(ev.user_id or "")
    fileos_scope = str(ev.group_id or ev.session_id or "")
    return owner, fileos_scope, scope_key_for_conversation(ev.group_id, owner)


def is_searchish_tool(name: str) -> bool:
    """检索/抓取类：恒落盘（当轮可回想），折叠卡不默认催出图。"""
    tn = (name or "").lower()
    return any(h in tn for h in ("search", "web_", "fetch"))


def payload_is_long_structured(content: str) -> bool:
    """正文形态是多点对照/表，与工具名无关。"""
    body = (content or "").strip()
    if len(body) < 80:
        return False
    if "|" in body and body.count("\n") >= 3:
        return True
    from gsuid_core.ai_core.capability_agents.delegation_contracts import fact_pack_is_multi_point

    return fact_pack_is_multi_point(body)


def _searchish_tool(name: str) -> bool:
    return is_searchish_tool(name)


def fold_card_for_main_prompt(
    card: PersistedHandleCard,
    *,
    content: str,
    is_group: bool,
) -> PersistedHandleCard:
    """群聊：summary + 句柄，无 inline。私聊：可带要点供当面问答。"""
    if is_group:
        return PersistedHandleCard(
            id=card.id,
            kind=card.kind,
            mime=card.mime,
            summary=card.summary,
            size_bytes=card.size_bytes,
            read_tool=card.read_tool,
            long_structured=card.long_structured,
            inline_head="",
            speech_expand=False,
        )
    return PersistedHandleCard(
        id=card.id,
        kind=card.kind,
        mime=card.mime,
        summary=card.summary,
        size_bytes=card.size_bytes,
        read_tool=card.read_tool,
        long_structured=card.long_structured,
        inline_head=extract_inline_head(content, max_chars=_INLINE_HEAD_PRIVATE),
        speech_expand=True,
    )


def _card_for_record(
    rec: AIToolOutputRecord,
    *,
    long_structured: bool = False,
    inline_head: str = "",
) -> PersistedHandleCard:
    mime = "text/markdown" if (rec.payload_path or "").endswith(".md") else "text/plain"
    return PersistedHandleCard(
        id=rec.id,
        kind="tool_output",
        mime=mime,
        summary=rec.summary or "",
        size_bytes=rec.size_bytes,
        read_tool="read_handle",
        long_structured=long_structured,
        inline_head=inline_head,
    )


async def persist_tool_return(
    tool_name: str,
    content: str,
    ev: Optional[Event],
    session_id: str,
    task_id: str,
    root_task_id: str,
) -> Optional[PersistedHandleCard]:
    if not should_persist_tool_return(tool_name, content):
        return None
    owner, scope, cog_scope = _scope_from_ev(ev)
    return await _write_record(
        rid_prefix="to",
        content=content,
        root_task_id=root_task_id or "",
        task_id=task_id or "",
        session_id=session_id or "",
        owner_user_id=owner,
        scope_key=scope,
        cog_scope_key=cog_scope,
        tool_name=tool_name or "",
        profile="",
        res_handle="",
        long_structured=(not _searchish_tool(tool_name)) or payload_is_long_structured(content),
    )


async def persist_subagent_result(
    profile: str,
    content: str,
    task: AIAgentTask,
    res_handle: str,
) -> Optional[PersistedHandleCard]:
    body = content or ""
    if len(body.strip()) < 40:
        return None
    root = task.root_task_id or task.id
    return await _write_record(
        rid_prefix="sa",
        content=body,
        root_task_id=root,
        task_id=task.id,
        session_id=task.session_id or "",
        owner_user_id=task.owner_user_id or "",
        scope_key=task.scope_key or "",
        # Kanban 侧的 scope_key 由 make_scope_key 生成，已是认知层口径
        cog_scope_key=task.scope_key or "",
        tool_name="",
        profile=profile or "",
        res_handle=res_handle or "",
        long_structured=True,
    )


async def _remember_short_tool_fact(tool_name: str, content: str, ev: Optional[Event], session_id: str) -> None:
    body = (content or "").strip()
    if not body or looks_like_handle_card(body):
        return
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember

    trunc = int(ai_config.get_config("remember_fact_trunc").data)
    owner, _fileos, cog_scope = _scope_from_ev(ev)
    digest = content_sha256(body)[:16]
    await remember(
        MemoryWrite(
            kind=CogKind.FACT,
            ref=f"toolshort:{session_id}:{digest}",
            scope_key=cog_scope,
            owner_user_id=owner,
            title=(tool_name or "工具")[:60],
            summary=body[:trunc],
            source="tool",
        )
    )


async def persist_and_fold_tool_return(
    tool_name: str,
    content: str,
    ev: Optional[Event],
    session_id: str,
    task_id: str = "",
    root_task_id: str = "",
    *,
    is_group: bool = False,
) -> Optional[str]:
    """主人格热路径：落盘并返回句柄卡；群聊不内嵌要点。"""
    tn = tool_name or ""
    if not should_persist_tool_return(tn, content):
        await _remember_short_tool_fact(tn, content, ev, session_id)
        return None
    if not should_fold_for_model(content, tool_name=tn, is_group=is_group):
        if is_searchish_tool(tn):
            await persist_tool_return(
                tool_name=tn,
                content=content,
                ev=ev,
                session_id=session_id,
                task_id=task_id,
                root_task_id=root_task_id,
            )
            return None
        schedule_persist_tool_return(
            tool_name=tn,
            content=content,
            ev=ev,
            session_id=session_id,
            task_id=task_id,
            root_task_id=root_task_id,
        )
        return None
    card = await persist_tool_return(
        tool_name=tn,
        content=content,
        ev=ev,
        session_id=session_id,
        task_id=task_id,
        root_task_id=root_task_id,
    )
    if card is None:
        return None
    fileos_metrics.inc_fold()
    return fold_card_for_main_prompt(card, content=content, is_group=is_group).format()


async def _write_record(
    *,
    rid_prefix: str,
    content: str,
    root_task_id: str,
    task_id: str,
    session_id: str,
    owner_user_id: str,
    scope_key: str,
    cog_scope_key: str,
    tool_name: str,
    profile: str,
    res_handle: str,
    long_structured: bool = False,
) -> Optional[PersistedHandleCard]:
    clean, n_redact = sanitize_for_persist(content)
    chash = content_sha256(clean)
    # 去重：owner+scope+hash+tool_name（tool 空时只按 hash）
    existing = await AIToolOutputRecord.get_by_hash(
        content_hash=chash,
        owner_user_id=owner_user_id,
        scope_key=scope_key,
        tool_name=tool_name or "",
    )
    if existing is not None:
        fileos_metrics.inc_dedup()
        return _card_for_record(existing, long_structured=long_structured)

    date_str = datetime.now().strftime("%Y-%m-%d")
    summary = extract_info_summary(clean, max_len=512)
    rid = f"{rid_prefix}_{uuid.uuid4().hex[:12]}"
    payload_inline: Optional[str] = clean if len(clean) <= _INLINE_MAX else None
    payload_path = ""
    size = len(clean.encode("utf-8"))
    expires = datetime.now() + timedelta(days=_TTL_DAYS)
    if payload_inline is None:
        fp = _tool_output_dir() / f"{rid}.md"
        fp.write_text(clean, encoding="utf-8")
        payload_path = str(fp)
    rec = AIToolOutputRecord(
        id=rid,
        root_task_id=root_task_id,
        task_id=task_id or rid,
        session_id=session_id,
        owner_user_id=owner_user_id,
        scope_key=scope_key,
        tool_name=tool_name,
        profile=profile,
        summary=summary,
        date_str=date_str,
        res_handle=res_handle,
        content_hash=chash,
        payload_inline=payload_inline,
        payload_path=payload_path,
        size_bytes=size,
        expires_at=expires,
    )
    try:
        await AIToolOutputRecord.batch_insert_data([rec])
    except Exception as e:
        # 并发去重：唯一约束冲突时回读既有行
        from sqlalchemy.exc import IntegrityError

        if not isinstance(e, IntegrityError) and "UNIQUE" not in str(e).upper() and "unique" not in str(e).lower():
            if payload_path:
                Path(payload_path).unlink(missing_ok=True)
            raise
        existing2 = await AIToolOutputRecord.get_by_hash(
            content_hash=chash,
            owner_user_id=owner_user_id,
            scope_key=scope_key,
            tool_name=tool_name or "",
        )
        if payload_path:
            Path(payload_path).unlink(missing_ok=True)
        if existing2 is not None:
            fileos_metrics.inc_dedup()
            return _card_for_record(existing2, long_structured=long_structured)
        raise
    fileos_metrics.inc_write(size, redacted=n_redact)
    asyncio.create_task(
        _index_chunks_safe(rid, clean, scope_key, cog_scope_key, owner_user_id, tool_name or profile, date_str)
    )
    return _card_for_record(rec, long_structured=long_structured)


async def _index_chunks_safe(
    rid: str,
    content: str,
    scope_key: str,
    cog_scope_key: str,
    owner_user_id: str,
    tool_name: str,
    date_str: str,
    *,
    retries: int = 2,
) -> None:
    from gsuid_core.ai_core.rag.chunking import split_text
    from gsuid_core.ai_core.planning.tool_output_index import index_tool_output_chunks

    chunks = split_text(content, max_chars=400, overlap=60)
    payload = {
        "id": rid,
        "scope_key": scope_key,
        "owner_user_id": owner_user_id,
        "tool_name": tool_name,
        "date_str": date_str,
        "summary": extract_info_summary(content, max_len=512),
        "title": extract_persist_title(content),
    }
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            await index_tool_output_chunks(chunks, payload=payload)
            fileos_metrics.inc_index(True)
            # 回流认知层：只登记节点 + 摘要，正文仍住 FileOS（索引层，不是第二份正文）
            await _distill_to_cognition(rid, payload, cog_scope_key, owner_user_id, tool_name, date_str)
            return
        except Exception as e:
            last_err = e
            if attempt < retries:
                await asyncio.sleep(0.3 * (attempt + 1))
    fileos_metrics.inc_index(False)
    logger.debug(t("log.ai.tool_output_index_skip_after_retry", e=last_err))


async def _distill_to_cognition(
    rid: str,
    payload: dict[str, Any],
    scope_key: str,
    owner_user_id: str,
    tool_name: str,
    date_str: str,
) -> None:
    """FileOS 写入成功后的认知层回流（失败只丢节点，不影响落盘真身）。

    ``scope_key`` 必须**换算成认知层口径**再写：FileOS 自己存的是裸 group_id /
    session_id，而 ``AICogNode.search`` 查的是 ``group:{gid}`` / ``user_global:{uid}``。
    直接透传会让每一条 tool_output 节点永远匹配不上，表只涨不召回。
    """
    from gsuid_core.ai_core.cognition.distill import distill_tool_output

    summary = str(payload["summary"]) if "summary" in payload else ""
    persist_title = str(payload["title"]) if "title" in payload and payload["title"] else ""
    await distill_tool_output(
        record_id=rid,
        tool_name=tool_name,
        summary=summary,
        scope_key=scope_key,
        owner_user_id=owner_user_id,
        as_of=date_str,
        persist_title=persist_title,
    )


def schedule_persist_tool_return(
    tool_name: str,
    content: str,
    ev: Optional[Event],
    session_id: str,
    task_id: str,
    root_task_id: str,
) -> None:
    if not should_persist_tool_return(tool_name, content):
        return

    async def _job() -> None:
        try:
            await persist_tool_return(
                tool_name=tool_name,
                content=content,
                ev=ev,
                session_id=session_id,
                task_id=task_id,
                root_task_id=root_task_id,
            )
        except Exception as e:
            logger.debug(t("log.ai.tool_output_persist_skip", e=e))

    asyncio.create_task(_job())
