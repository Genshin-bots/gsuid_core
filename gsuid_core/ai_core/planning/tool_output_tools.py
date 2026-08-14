"""供 agent 检索/列举/全文 grep/统一句柄读。"""

from __future__ import annotations

from typing import Optional
from datetime import datetime, timedelta

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.control.delegation import (
    load_delegation,
    format_delegation,
    is_delegation_handle,
)
from gsuid_core.ai_core.planning.handle_resolver import (
    ResolvedHandle,
    resolve_handle,
    format_resolved,
)
from gsuid_core.ai_core.planning.tool_output_store import AIToolOutputRecord
from gsuid_core.ai_core.planning.tool_output_protocol import rrf_fuse, load_payload_text


def _owner_scope(ctx: RunContext[ToolContext], scope: str = "auto") -> tuple[Optional[str], Optional[str]]:
    ev = ctx.deps.ev
    owner = str(ev.user_id) if ev and ev.user_id else None
    scope_key: Optional[str] = None
    if scope != "auto" and scope:
        scope_key = scope
    elif ev is not None and ev.group_id:
        scope_key = str(ev.group_id)
    return owner, scope_key


def _require_owner(
    ctx: RunContext[ToolContext],
    scope: str = "auto",
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """检索类入口：无 owner 时 fail-closed，避免全局扫表。"""
    owner, scope_key = _owner_scope(ctx, scope)
    if not owner:
        return None, None, "⚠️ 无用户上下文，拒绝检索落盘记录。"
    return owner, scope_key, None


def _tool_output_access_allowed(
    resolved: ResolvedHandle,
    ctx: RunContext[ToolContext],
) -> bool:
    """FileOS 行级 ACL：同 owner 或同群 scope；无 owner 且无 ev 才放行（系统路径）。"""
    ev = ctx.deps.ev
    if ev is None:
        return True
    owner = (resolved.owner_user_id or "").strip()
    if not owner:
        return False
    if str(ev.user_id) == owner:
        return True
    if ev.group_id and resolved.scope_key and str(ev.group_id) == resolved.scope_key:
        return True
    return False


async def _handle_access_allowed(
    resolved: ResolvedHandle,
    ctx: RunContext[ToolContext],
) -> bool:
    if resolved.source == "tool_output":
        return _tool_output_access_allowed(resolved, ctx)
    if resolved.source in {"artifact", "image"}:
        from gsuid_core.ai_core.planning.models import AIAgentArtifact
        from gsuid_core.ai_core.planning.runtime import get_plan_context
        from gsuid_core.ai_core.planning.kanban_tools import _artifact_access_allowed

        art = await AIAgentArtifact.get_by_id(resolved.id)
        if art is None:
            return False
        plan_ctx = get_plan_context()
        return await _artifact_access_allowed(art=art, plan_ctx=plan_ctx, ctx=ctx)
    return False


def _line(rec: AIToolOutputRecord) -> str:
    label = rec.tool_name or rec.profile or "-"
    return f"- {rec.id} | {label} | {rec.date_str} | {rec.summary[:80]}"


@ai_tools(category="buildin", capability_domain="产物")
async def read_handle(
    ctx: RunContext[ToolContext],
    handle_id: str,
    offset: int = 0,
    limit: int = 8000,
) -> str:
    """统一读句柄：to_/sa_/res_/img_/dlg_ 均可；图片只返回发送提示。

    长文按 **字符** offset/limit 分页（见返回文首【读窗口】）。
    续读请把 offset 设为上一页提示的 next（如 got 段末），勿重复 offset=0。
    ``dlg_`` 是委派句柄，返回该子任务的实时状态与产物（等价 check_delegation）。
    框架保底工具：折叠后的检索/产物必须用本工具取全文，禁止空口说「只有句柄」。
    """
    if is_delegation_handle(handle_id):
        deleg = await load_delegation(handle_id)
        if deleg is None:
            return f"⚠️ 委派句柄不存在: {handle_id}"
        return format_delegation(deleg)
    resolved = await resolve_handle(handle_id)
    if resolved is None:
        return f"⚠️ 句柄不存在: {handle_id}"
    if not await _handle_access_allowed(resolved, ctx):
        return "⚠️ 无权限读取该句柄。"
    # 单次读窗上限；全文靠 offset 续读
    lim = max(1, min(int(limit), 32000))
    off = max(0, int(offset))
    return format_resolved(resolved, offset=off, limit=lim)


async def search_fileos_outputs(
    ctx: RunContext[ToolContext],
    query: str,
    scope: str = "auto",
    limit: int = 8,
    *,
    section_header: bool = True,
) -> str:
    """FileOS hybrid+SQL 融合检索（**非工具**，供 ``search_knowledge`` 联邦）。

    已下线独立 agent 工具 ``search_persisted_outputs`` / ``search_handles``，
    避免与 ``search_knowledge`` 双入口选型混乱。
    """
    owner, scope_key, err = _require_owner(ctx, scope)
    if err:
        return err
    hybrid_ids: list[str] = []
    hybrid_meta: dict[str, dict] = {}
    try:
        from gsuid_core.ai_core.planning.tool_output_index import search_tool_outputs

        hits = await search_tool_outputs(
            query=query,
            limit=limit * 2,
            scope_key=scope_key,
            owner_user_id=owner,
        )
        for h in hits:
            rid = str(h["id"]) if "id" in h else ""
            if rid:
                hybrid_ids.append(rid)
                hybrid_meta[rid] = h
    except Exception as e:
        logger.debug(t("log.ai.tool_output_hybrid_search_skip", e=e))

    sql_rows = await AIToolOutputRecord.search(
        owner_user_id=owner,
        scope_key=scope_key,
        keyword=query,
        limit=limit * 2,
    )
    sql_ids = [r.id for r in sql_rows]
    sql_map = {r.id: r for r in sql_rows}

    fused = rrf_fuse([hybrid_ids, sql_ids], limit=limit)
    if not fused:
        return ""
    lines: list[str] = []
    if section_header:
        lines.append(f"【近期检索落盘】融合命中 {len(fused)} 条（历史工具材料，数字可能过时）：")
    else:
        lines.append(f"📚 融合检索 {len(fused)} 条：")
    for rid in fused:
        if rid in sql_map:
            lines.append(_line(sql_map[rid]))
        elif rid in hybrid_meta:
            h = hybrid_meta[rid]
            sm = str(h["summary"])[:80] if "summary" in h else ""
            lines.append(f"- {rid} | hybrid | {sm}")
        else:
            lines.append(f"- {rid}")
    lines.append("需要全文时用 read_handle(handle_id=…) 分页取。")
    return "\n".join(lines)


@ai_tools(category="common", capability_domain="产物")
async def list_persisted_outputs(
    ctx: RunContext[ToolContext],
    session_id: str = "",
    task_id: str = "",
    root_task_id: str = "",
    limit: int = 20,
) -> str:
    """按 session / task / root_task 列举最近落盘。"""
    owner, _scope_key, err = _require_owner(ctx)
    if err:
        return err
    sid = session_id.strip()
    if not sid and ctx.deps.parent_session_id:
        sid = str(ctx.deps.parent_session_id)
    rows = await AIToolOutputRecord.list_recent(
        owner_user_id=owner,
        session_id=sid or None,
        task_id=task_id.strip() or None,
        root_task_id=root_task_id.strip() or None,
        limit=limit,
    )
    if not rows:
        return "ℹ️ 当前过滤条件下无落盘记录。"
    lines = [f"📋 最近 {len(rows)} 条："]
    for r in rows:
        lines.append(_line(r))
    return "\n".join(lines)


@ai_tools(category="common", capability_domain="产物")
async def grep_persisted_outputs(
    ctx: RunContext[ToolContext],
    keyword: str,
    tool_name: Optional[str] = None,
    limit: int = 20,
    days: int = 7,
) -> str:
    """近 N 天落盘全文 grep（默认 7 天）。"""
    if not keyword.strip():
        return "⚠️ keyword 不能为空。"
    owner, _scope, err = _require_owner(ctx)
    if err:
        return err
    candidates = await AIToolOutputRecord.search(
        owner_user_id=owner,
        tool_name=tool_name,
        limit=max(limit * 8, 60),
    )
    # 日期窗口
    cut = (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    hits: list[str] = []
    kw = keyword
    for r in candidates:
        if r.date_str and r.date_str < cut:
            continue
        text, load_err = load_payload_text(
            payload_inline=r.payload_inline,
            payload_path=r.payload_path,
        )
        blob = text if not load_err and text else (r.summary or "")
        if kw not in blob:
            continue
        idx = blob.find(kw)
        start = max(0, idx - 40)
        end = min(len(blob), idx + len(kw) + 40)
        snip = blob[start:end].replace("\n", " ")
        hits.append(f"- {r.id} | …{snip}…")
        if len(hits) >= limit:
            break
    if not hits:
        return f"ℹ️ 近 {days} 天全文未命中该关键词。"
    return f"🔎 全文命中 {len(hits)} 条（近{days}天）：\n" + "\n".join(hits)


@ai_tools(category="common", capability_domain="产物")
async def read_persisted_output(
    ctx: RunContext[ToolContext],
    record_id: str,
    offset: int = 0,
    limit: int = 8000,
) -> str:
    """兼容旧名：等价 read_handle。"""
    return await read_handle(ctx, handle_id=record_id, offset=offset, limit=limit)
