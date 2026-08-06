"""落盘帮助：达标 ToolReturn / 子代理终态 → SQL + 可选折叠句柄卡。"""

from __future__ import annotations

import uuid
import asyncio
import hashlib
from typing import Optional
from pathlib import Path
from datetime import datetime, timedelta

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.planning.models import AIAgentTask
from gsuid_core.ai_core.planning.workspace import ARTIFACT_ROOT
from gsuid_core.ai_core.planning.tool_output_store import AIToolOutputRecord
from gsuid_core.ai_core.planning.tool_output_metrics import fileos_metrics
from gsuid_core.ai_core.planning.tool_output_protocol import PersistedHandleCard
from gsuid_core.ai_core.planning.tool_output_sanitize import sanitize_for_persist

_MIN_PERSIST_CHARS = 800
_INLINE_MAX = 4096
_TTL_DAYS = 30
# 自适应折叠：私聊 / 群聊 / 能力代理
_FOLD_PRIVATE = 1200
_FOLD_GROUP = 900
_NEVER_FOLD_TOOLS = frozenset({"create_subagent"})


def _tool_output_dir() -> Path:
    p = ARTIFACT_ROOT / "_tool_outputs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fold_threshold(*, is_group: bool = False) -> int:
    return _FOLD_GROUP if is_group else _FOLD_PRIVATE


def should_persist_tool_return(tool_name: str, content: str) -> bool:
    """结构门：过短或异步挂起 ack 不落盘。"""
    body = (content or "").strip()
    if len(body) < _MIN_PERSIST_CHARS:
        return False
    if "后台执行" in body and "自动回灌" in body:
        return False
    if "仍在执行" in body:
        return False
    return True


def should_fold_for_model(
    content: str,
    *,
    tool_name: str = "",
    is_group: bool = False,
) -> bool:
    """主人格折叠门：create_subagent 永不折；长度按私聊/群聊阈值。"""
    if (tool_name or "") in _NEVER_FOLD_TOOLS:
        return False
    return len((content or "").strip()) >= fold_threshold(is_group=is_group)


def _scope_from_ev(ev: Optional[Event]) -> tuple[str, str]:
    if ev is None:
        return "", ""
    owner = str(ev.user_id or "")
    scope = str(ev.group_id or ev.session_id or "")
    return owner, scope


def _card_for_record(rec: AIToolOutputRecord, *, long_structured: bool = True) -> PersistedHandleCard:
    mime = "text/markdown" if (rec.payload_path or "").endswith(".md") else "text/plain"
    return PersistedHandleCard(
        id=rec.id,
        kind="tool_output",
        mime=mime,
        summary=rec.summary or "",
        size_bytes=rec.size_bytes,
        read_tool="read_handle",
        long_structured=long_structured,
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
    owner, scope = _scope_from_ev(ev)
    return await _write_record(
        rid_prefix="to",
        content=content,
        root_task_id=root_task_id or "",
        task_id=task_id or "",
        session_id=session_id or "",
        owner_user_id=owner,
        scope_key=scope,
        tool_name=tool_name or "",
        profile="",
        res_handle="",
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
        tool_name="",
        profile=profile or "",
        res_handle=res_handle or "",
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
    """主人格热路径：落盘并返回句柄卡；create_subagent / 过短不折。"""
    tn = tool_name or ""
    if tn in _NEVER_FOLD_TOOLS:
        # 仍可旁路落盘终态长文，但不折叠回执
        if should_persist_tool_return(tn, content):
            schedule_persist_tool_return(
                tool_name=tn,
                content=content,
                ev=ev,
                session_id=session_id,
                task_id=task_id,
                root_task_id=root_task_id,
            )
        return None
    if not should_persist_tool_return(tn, content):
        return None
    if not should_fold_for_model(content, tool_name=tn, is_group=is_group):
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
    return card.format()


async def _write_record(
    *,
    rid_prefix: str,
    content: str,
    root_task_id: str,
    task_id: str,
    session_id: str,
    owner_user_id: str,
    scope_key: str,
    tool_name: str,
    profile: str,
    res_handle: str,
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
        return _card_for_record(existing)

    date_str = datetime.now().strftime("%Y-%m-%d")
    summary = clean[:512].replace("\n", " ")
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
            return _card_for_record(existing2)
        raise
    fileos_metrics.inc_write(size, redacted=n_redact)
    asyncio.create_task(_index_chunks_safe(rid, clean, scope_key, owner_user_id, tool_name or profile, date_str))
    return _card_for_record(rec)


async def _index_chunks_safe(
    rid: str,
    content: str,
    scope_key: str,
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
        "summary": content[:512].replace("\n", " "),
    }
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            await index_tool_output_chunks(chunks, payload=payload)
            fileos_metrics.inc_index(True)
            return
        except Exception as e:
            last_err = e
            if attempt < retries:
                await asyncio.sleep(0.3 * (attempt + 1))
    fileos_metrics.inc_index(False)
    logger.debug(t("log.ai.tool_output_index_skip_after_retry", e=last_err))


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
