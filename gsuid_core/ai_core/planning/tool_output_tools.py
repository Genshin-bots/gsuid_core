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
from gsuid_core.ai_core.buildin_tools.visibility import visible_to_capability_only
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
    if resolved.source == "knowledge":
        return True
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


def _extract_query_snippets(text: str, query: str, window: int = 300, max_snippets: int = 5) -> str:
    """在文本中检索 query，提取其前后各 window 字符的上下文段落窗口。"""
    if not query.strip():
        return text
    q_low = query.lower()
    t_low = text.lower()
    indices: list[int] = []
    start = 0
    while len(indices) < max_snippets:
        pos = t_low.find(q_low, start)
        if pos == -1:
            break
        indices.append(pos)
        start = pos + max(1, len(q_low))
    if not indices:
        return f"（未在内容中检索到关键词「{query}」，全文共 {len(text)} 字符）"
    snippets: list[str] = []
    for i, pos in enumerate(indices, 1):
        s = max(0, pos - window)
        e = min(len(text), pos + len(query) + window)
        prefix = "…" if s > 0 else ""
        suffix = "…" if e < len(text) else ""
        snippet = text[s:e].strip()
        snippets.append(f"【片段 {i} (字符 {s}-{e})】\n{prefix}{snippet}{suffix}")
    return "\n\n".join(snippets)


async def _read_single_handle(
    ctx: RunContext[ToolContext],
    hid: str,
    offset: int,
    limit: int,
    query: str = "",
) -> str:
    if is_delegation_handle(hid):
        deleg = await load_delegation(hid)
        if deleg is None:
            return f"⚠️ 委派句柄不存在: {hid}"
        formatted = format_delegation(deleg)
        if query:
            return _extract_query_snippets(formatted, query)
        return formatted
    resolved = await resolve_handle(hid)
    if resolved is None:
        return f"⚠️ 句柄不存在: {hid}"
    if not await _handle_access_allowed(resolved, ctx):
        return "⚠️ 无权限读取该句柄。"
    if query:
        text, err = load_payload_text(
            payload_inline=resolved.payload_inline,
            payload_path=resolved.payload_path,
        )
        if err:
            return f"handle {resolved.id} | ⚠️ {err}"
        if not text:
            return f"handle {resolved.id} | （无内容）"
        snippets = _extract_query_snippets(text, query)
        return f"handle {resolved.id} | query: {query}\n\n{snippets}"
    lim = max(1, min(int(limit), 32000))
    off = max(0, int(offset))
    return format_resolved(resolved, offset=off, limit=lim)


@ai_tools(category="buildin", capability_domain="产物", code_callable=True)
async def read_handle(
    ctx: RunContext[ToolContext],
    handle_id: str = "",
    offset: int = 0,
    limit: int = 8000,
    handle_ids: list[str] | None = None,
    query: str = "",
) -> str:
    """统一读句柄：to_/sa_/res_/img_/dlg_/kb_plugin/kb_kbdoc 均可；支持单句柄、多句柄与关键词窗口提取。

    长文按 **字符** offset/limit 分页（见返回文首【读窗口】）。
    支持多句柄批量读：传入 handle_ids 一次获取多个句柄内容，避免多次往返。
    支持关键词过滤提取：传入 query 只返回包含关键词的前后上下文段落窗口。
    ``dlg_`` 是委派句柄，返回该子任务的实时状态与产物（等价 check_delegation）。
    ``kb_plugin`` / ``kb_kbdoc`` 是公共知识全文，无属主校验。
    框架保底工具：折叠后的检索/产物必须用本工具取全文，禁止空口说「只有句柄」。
    """
    targets: list[str] = []
    if handle_ids:
        targets.extend(h.strip() for h in handle_ids if h and h.strip())
    if handle_id and handle_id.strip() and handle_id.strip() not in targets:
        targets.insert(0, handle_id.strip())
    if not targets:
        return "⚠️ 请提供 handle_id 或 handle_ids。"
    if len(targets) == 1:
        return await _read_single_handle(ctx, targets[0], offset, limit, query)
    results: list[str] = []
    for hid in targets[:16]:
        res = await _read_single_handle(ctx, hid, offset, limit, query)
        results.append(f"=== {hid} ===\n{res}")
    return "\n\n".join(results)


async def search_fileos_outputs(
    ctx: RunContext[ToolContext],
    query: str,
    scope: str = "auto",
    limit: int = 8,
    *,
    section_header: bool = True,
) -> str:
    """FileOS hybrid+SQL 融合检索（**非工具**，供 ``search_cognition`` 联邦）。

    已下线独立 agent 工具 ``search_persisted_outputs`` / ``search_handles``，
    避免与 ``search_cognition`` 双入口选型混乱。
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


@ai_tools(
    category="common",
    capability_domain="产物",
    visible_when=visible_to_capability_only,
    code_callable=True,
)
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


@ai_tools(
    category="common",
    capability_domain="产物",
    visible_when=visible_to_capability_only,
    code_callable=True,
)
async def grep_persisted_outputs(
    ctx: RunContext[ToolContext],
    keyword: str,
    tool_name: Optional[str] = None,
    limit: int = 20,
    days: int = 7,
) -> str:
    """检索近 N 天落盘全文（默认 7 天）。"""
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
