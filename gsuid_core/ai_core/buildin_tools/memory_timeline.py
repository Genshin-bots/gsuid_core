"""Turn 级时间线工具：词面搜索 / 回看 session / 全量时间线 / 登记证据。"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key, scope_key_for_conversation
from gsuid_core.ai_core.buildin_tools.visibility import visible_when_timeline_query


def _scope_keys(ctx: ToolContext) -> list[str]:
    ev = ctx.ev
    if ev is None:
        return []
    user_id = ev.user_id
    group_id = ev.group_id if ev.group_id else None
    keys: list[str] = []
    if not user_id:
        return keys
    from gsuid_core.ai_core.memory.config import memory_config

    conv = scope_key_for_conversation(group_id, str(user_id))
    if conv:
        keys.append(conv)
    if not group_id or memory_config.enable_user_global_memory:
        ug = make_scope_key(ScopeType.USER_GLOBAL, user_id)
        if ug not in keys:
            keys.append(ug)
    return keys


def _parse_day(raw: str | None) -> datetime | None:
    if not raw or len(raw.strip()) < 10:
        return None
    try:
        return datetime.strptime(raw.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _mark_for(eid: str) -> str:
    from gsuid_core.ai_core.agent_run.order_answer import get_turn_ledger

    view = get_turn_ledger()
    if view is None:
        return "#" + eid[:8]
    for ln in view.lines:
        if ln.episode_id == eid:
            return ln.mark
    return "#" + eid[:8]


@ai_tools(category="buildin", visible_when=visible_when_timeline_query)
async def search_turns(
    ctx: RunContext[ToolContext],
    keywords: list[str],
    session_id: str = "",
    start: str = "",
    end: str = "",
    k: int = 20,
) -> str:
    """按关键词搜用户 turn。同 session 命中加分；可收窄 session 或日期。

    Args:
        ctx: 工具执行上下文
        keywords: 专名 / 工具 / 事件词，不要只写 in order
        session_id: 可选，只搜这一段
        start: 可选起始日 YYYY-MM-DD
        end: 可选结束日 YYYY-MM-DD
        k: 最多返回条数，默认 20
    """
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode
    from gsuid_core.ai_core.memory.retrieval.lexical import query_tokens, _speaker_stripped

    keys = _scope_keys(ctx.deps)
    toks = [t for t in keywords if t.strip()] if keywords else []
    if not toks:
        toks = query_tokens(" ".join(keywords))
    if not keys or not toks:
        return "没有可检索的范围或关键词为空。"
    lo = _parse_day(start)
    hi = _parse_day(end)
    hi_ex = (hi + timedelta(days=1)) if hi is not None else None
    scored: dict[str, tuple[float, AIMemEpisode]] = {}
    sid_hits: dict[str, int] = {}
    for sk in keys:
        rows = await AIMemEpisode.search_by_tokens(
            sk,
            toks[:12],
            limit=80,
            start=lo,
            end=hi_ex,
            user_only=True,
        )
        for row in rows:
            if session_id and row.session_id != session_id:
                continue
            sid = row.session_id or ""
            n = sid_hits[sid] if sid in sid_hits else 0
            sid_hits[sid] = n + 1
            prev = scored[row.id][0] if row.id in scored else 0.0
            scored[row.id] = (prev + 1.0, row)
    for sid, n in sid_hits.items():
        if n < 2:
            continue
        bonus = 0.15 * n
        for eid, (sc, row) in list(scored.items()):
            if row.session_id == sid:
                scored[eid] = (sc + bonus, row)

    def _at(row: AIMemEpisode) -> datetime:
        at = row.valid_at
        if at is None:
            return datetime(1970, 1, 1)
        return at.replace(tzinfo=None) if at.tzinfo is not None else at

    ranked = sorted(scored.values(), key=lambda p: (-p[0], _at(p[1])))
    lines: list[str] = []
    for _sc, row in ranked[: max(1, min(k, 40))]:
        day = row.valid_at.strftime("%Y-%m-%d") if row.valid_at else ""
        gist = _speaker_stripped(row.content or "").replace("\n", " ").strip()[:160]
        sid = row.session_id or "-"
        lines.append(f"{_mark_for(row.id)} · {day} · session={sid} · {gist}")
    if not lines:
        return "没有命中。换更具体的专名或放宽日期。"
    return "【search_turns】\n" + "\n".join(lines)


@ai_tools(category="buildin", visible_when=visible_when_timeline_query)
async def read_session(
    ctx: RunContext[ToolContext],
    session_id: str,
    around: str = "",
    radius: int = 6,
) -> str:
    """读一段 session 的 gist；可指定 #id 展开 ±radius 条原文。

    Args:
        ctx: 工具执行上下文
        session_id: session id
        around: 可选 #id 或 episode_id，展开附近原文
        radius: 展开半径，默认 6
    """
    from gsuid_core.ai_core.agent_run.order_answer import get_turn_ledger
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode, AIMemTurnGist
    from gsuid_core.ai_core.memory.retrieval.lexical import _assistant_turn, _speaker_stripped

    sid = (session_id or "").strip()
    if not sid:
        return "session_id 为空。"
    keys = set(_scope_keys(ctx.deps))
    if not keys:
        return "没有可检索的记忆范围。"
    gists = [g for g in await AIMemTurnGist.list_by_session(sid) if g.scope_key in keys]
    rows = [r for r in await AIMemEpisode.get_session(sid) if r.scope_key in keys]
    if not rows and not gists:
        return f"找不到 session {sid}。"
    view = get_turn_ledger()
    mark_by_eid: dict[str, str] = {}
    if view is not None:
        for ln in view.lines:
            mark_by_eid[ln.episode_id] = ln.mark
    gist_by_eid = {g.episode_id: g.gist for g in gists}
    lines: list[str] = [f"【session {sid} gist】"]
    user_rows = [r for r in rows if not _assistant_turn(r.content or "")]
    for row in user_rows:
        day = row.valid_at.strftime("%Y-%m-%d") if row.valid_at else ""
        mark = mark_by_eid[row.id] if row.id in mark_by_eid else _mark_for(row.id)
        body = gist_by_eid[row.id] if row.id in gist_by_eid else _speaker_stripped(row.content or "")[:160]
        lines.append(f"{mark} · {day} · {body.replace(chr(10), ' ').strip()}")
    target = (around or "").strip()
    if target:
        eid = ""
        if view is not None and target in view.ledger_ids:
            eid = view.ledger_ids[target]
        elif target.startswith("#") and view is not None:
            eid = view.ledger_ids[target] if target in view.ledger_ids else ""
        else:
            eid = target
        idx = -1
        for i, row in enumerate(user_rows):
            if row.id == eid:
                idx = i
                break
        if idx >= 0:
            lo = max(0, idx - max(0, radius))
            hi = min(len(user_rows), idx + max(0, radius) + 1)
            lines.append("【原文】")
            for row in user_rows[lo:hi]:
                day = row.valid_at.strftime("%Y-%m-%d") if row.valid_at else ""
                body = _speaker_stripped(row.content or "").replace("\n", " ").strip()[:400]
                mark = mark_by_eid[row.id] if row.id in mark_by_eid else _mark_for(row.id)
                lines.append(f"{mark} · {day} · {body}")
    return "\n".join(lines) if len(lines) > 1 else f"session {sid} 没有用户发言。"


@ai_tools(category="buildin", visible_when=visible_when_timeline_query)
async def timeline(
    ctx: RunContext[ToolContext],
    topic: str,
    start: str = "",
    end: str = "",
) -> str:
    """返回按时间排列的用户发言时间线，可按日期收窄。

    Args:
        ctx: 工具执行上下文
        topic: 主题词，用于裁尾打分
        start: 可选起始日 YYYY-MM-DD
        end: 可选结束日 YYYY-MM-DD
    """
    from gsuid_core.ai_core.memory.retrieval.ledger_timeline import build_ledger, format_ledger_block

    keys = _scope_keys(ctx.deps)
    if not keys:
        return "没有可检索的记忆范围。"
    view = await build_ledger(keys, topic or "timeline")
    lo = _parse_day(start)
    hi = _parse_day(end)
    if lo is not None or hi is not None:
        kept = []
        for ln in view.lines:
            if lo is not None and ln.valid_at < lo:
                continue
            if hi is not None and ln.valid_at.date() > hi.date():
                continue
            kept.append(ln)
        view.lines = kept
        view.inject_ids = [ln.episode_id for ln in kept]
        view.chars = 0
    return format_ledger_block(view, topic or "timeline")


@ai_tools(category="buildin", visible_when=visible_when_timeline_query)
async def mark_evidence(ctx: RunContext[ToolContext], turn_ids: list[str]) -> str:
    """把本轮认定的首次子话题 #id 登记为证据。阶段一每找到一条就调用。

    Args:
        ctx: 工具执行上下文
        turn_ids: 时间线上的 #id，如 ["#3", "#17"]
    """
    _ = ctx
    from gsuid_core.ai_core.agent_run.evidence_stage import mark_turn_ids

    n = mark_turn_ids(turn_ids)
    return f"已登记 {n} 条证据。"


@ai_tools(category="buildin", visible_when=visible_when_timeline_query)
async def recall_timeline(
    ctx: RunContext[ToolContext],
    topic: str,
    start: str = "",
    end: str = "",
    n: int = 12,
) -> str:
    """兼容旧名：等价 ``timeline``。

    Args:
        ctx: 工具执行上下文
        topic: 主题词
        start: 可选起始日
        end: 可选结束日
        n: 忽略，保留签名
    """
    _ = n
    return await timeline(ctx, topic, start, end)


@ai_tools(category="buildin", visible_when=visible_when_timeline_query)
async def recall_session(ctx: RunContext[ToolContext], session_id: str) -> str:
    """兼容旧名：等价 ``read_session``。

    Args:
        ctx: 工具执行上下文
        session_id: session id
    """
    return await read_session(ctx, session_id)
