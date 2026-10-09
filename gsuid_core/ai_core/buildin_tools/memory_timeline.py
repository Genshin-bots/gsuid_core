"""Turn 级时间线工具：词面搜索 / 回看 session / 全量时间线 / 登记证据。"""

from __future__ import annotations

from typing import Protocol
from datetime import datetime, timedelta
from collections.abc import Sequence

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
    return "#" + eid[:8]


class _MarkedTurn(Protocol):
    id: str


def _locate_turn(rows: Sequence[_MarkedTurn], target: str) -> tuple[int, str]:
    """# 前缀对 episode_id。对上多条或没有时只回说明，不展开原文。"""
    raw = target.strip()
    if raw.startswith("#"):
        prefix = raw[1:]
        hits = [i for i, row in enumerate(rows) if prefix and row.id.startswith(prefix)]
    else:
        hits = [i for i, row in enumerate(rows) if row.id == raw]
    if len(hits) == 1:
        return hits[0], ""
    if len(hits) > 1:
        return -1, "这个标记对上了多条，请改用完整 episode_id。"
    return -1, "没有对上这个标记。"


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
    gist_by_eid = {g.episode_id: g.gist for g in gists}
    lines: list[str] = [f"【session {sid} gist】"]
    user_rows = [r for r in rows if not _assistant_turn(r.content or "")]
    for row in user_rows:
        day = row.valid_at.strftime("%Y-%m-%d") if row.valid_at else ""
        mark = _mark_for(row.id)
        body = gist_by_eid[row.id] if row.id in gist_by_eid else _speaker_stripped(row.content or "")[:160]
        lines.append(f"{mark} · {day} · {body.replace(chr(10), ' ').strip()}")
    target = (around or "").strip()
    if target:
        idx, note = _locate_turn(user_rows, target)
        if note:
            lines.append(note)
        elif idx >= 0:
            lo = max(0, idx - max(0, radius))
            hi = min(len(user_rows), idx + max(0, radius) + 1)
            lines.append("【原文】")
            for row in user_rows[lo:hi]:
                day = row.valid_at.strftime("%Y-%m-%d") if row.valid_at else ""
                body = _speaker_stripped(row.content or "").replace("\n", " ").strip()[:400]
                mark = _mark_for(row.id)
                lines.append(f"{mark} · {day} · {body}")
    return "\n".join(lines) if len(lines) > 1 else f"session {sid} 没有用户发言。"


@ai_tools(category="buildin", visible_when=visible_when_timeline_query, code_callable=False)
async def mark_evidence(ctx: RunContext[ToolContext], turn_ids: list[str]) -> str:
    """把本轮认定的首次子话题标记登记为证据。阶段一每找到一条就调用。

    Args:
        ctx: 工具执行上下文
        turn_ids: 时间线上的标记，如 ["#abcdef12"]
    """
    _ = ctx
    from gsuid_core.ai_core.agent_run.evidence_stage import mark_turn_ids

    n = mark_turn_ids(turn_ids)
    return f"已登记 {n} 条证据。"


@ai_tools(category="buildin", visible_when=visible_when_timeline_query)
async def recall_session(ctx: RunContext[ToolContext], session_id: str) -> str:
    """兼容旧名：等价 ``read_session``。

    Args:
        ctx: 工具执行上下文
        session_id: session id
    """
    return await read_session(ctx, session_id)
