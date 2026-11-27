"""按需人物档案查询工具（category="common"，零每轮常驻 Schema 税）。

不进入 ``MAIN_AGENT_CORE_TOOLS``，靠 ``find_tools`` 按需召回：
- 群聊：按昵称 / 外号 / UID 精确解析同群成员，返回别名表、自身属性、人际关系、
  带来源口吻传闻（staging）、已否认矛盾对（contradicted）、可选历史变更链及关联故事线；
- 私聊：严格仅允许查询当前用户本人跨群聚合档案（附 ``[群:gid]``），严禁查看同群他人。
"""

from __future__ import annotations

from typing import Sequence
from datetime import datetime, timezone

from sqlmodel import col, or_, desc, select
from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.control.lines import (
    MEMORY_RUMOR_DENIED_TAG,
    MEMORY_RETRACTED_EDGE_TAG,
    MEMORY_SUPERSEDED_EDGE_TAG,
    MEMORY_RUMOR_UNCONFIRMED_TAG,
)
from gsuid_core.ai_core.memory.privacy import (
    prefix_group_tag,
    resolve_user_group_scopes,
)
from gsuid_core.ai_core.memory.identity import resolve_member_by_query
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.database.models import (
    AIMemEdge,
    AIMemEntity,
    AIMemThread,
)
from gsuid_core.ai_core.memory.retrieval.prompt_render import (
    _sensitive_fact_hit,
    _get_sensitive_extra_terms,
)

_SELF_QUERY_WORDS: frozenset[str] = frozenset({"我", "我自己", "自己", "本人", "me", "myself", "self"})


def _fmt_date(edge: AIMemEdge) -> str:
    dt = edge.event_at or edge.valid_at
    return dt.strftime("%Y-%m-%d") if dt is not None else ""


def _render_entity_dossier(
    entity: AIMemEntity,
    edges: Sequence[AIMemEdge],
    threads: Sequence[AIMemThread],
    *,
    is_self: bool,
    include_history: bool,
    is_private: bool,
    extra_sensitive: Sequence[str],
) -> str:
    aliases = entity.get_aliases()
    pending = [p["alias"] for p in entity.get_pending_aliases()]
    uid = (entity.user_id or "").strip()

    lines: list[str] = [f"### {entity.name}" + (f"（UID: {uid}）" if uid else "")]
    if entity.summary.strip():
        lines.append(f"- 简介：{entity.summary.strip()}")
    if aliases:
        lines.append(f"- 外号/曾用名：{'、'.join(aliases)}")
    if pending:
        lines.append(f"- 待证实称呼：{'、'.join(pending)}")

    attrs: list[str] = []
    rels: list[str] = []
    rumors: list[str] = []
    conflicts: list[str] = []
    histories: list[str] = []
    linked_tids: set[str] = set()

    now_utc = datetime.now(timezone.utc)
    for edge in edges:
        if edge.thread_id:
            linked_tids.add(edge.thread_id)
        fact = " ".join((edge.fact or "").split()).strip()
        if not fact:
            continue
        if not is_self and _sensitive_fact_hit(fact, extra_sensitive):
            continue
        rendered = prefix_group_tag(edge.scope_key, fact) if is_private else fact
        stamp = _fmt_date(edge)
        line_body = f"[{stamp}] {rendered}" if stamp else rendered
        state = edge.resolution_state
        exp = edge.expired_at
        is_expired = (
            exp is not None and (exp if exp.tzinfo is not None else exp.replace(tzinfo=timezone.utc)) <= now_utc
        )

        if state in ("superseded", "retracted") or edge.invalid_at is not None or is_expired:
            if include_history:
                tag = MEMORY_RETRACTED_EDGE_TAG if (state == "retracted" or is_expired) else MEMORY_SUPERSEDED_EDGE_TAG
                histories.append(f"  · {tag}{line_body}")
            continue
        if state == "staging":
            rumors.append(f"  · {MEMORY_RUMOR_UNCONFIRMED_TAG}{line_body}")
        elif state == "contradicted":
            conflicts.append(f"  · {MEMORY_RUMOR_DENIED_TAG}{line_body}")
        elif edge.source_entity_id == edge.target_entity_id or edge.edge_kind == "attribute":
            attrs.append(f"  · {line_body}")
        else:
            rels.append(f"  · {line_body}")

    if attrs:
        lines.append("- 自身属性与近况：\n" + "\n".join(attrs[:12]))
    if rels:
        lines.append("- 人际关系与事件：\n" + "\n".join(rels[:10]))
    if rumors:
        lines.append("- 待证实传闻（切勿当作实锤断言）：\n" + "\n".join(rumors[:6]))
    if conflicts:
        lines.append("- 存在矛盾/已否认记录：\n" + "\n".join(conflicts[:6]))
    if histories:
        lines.append("- 历史变更/已撤回记录：\n" + "\n".join(histories[:8]))

    matched_threads: list[str] = []
    for th in threads:
        if th.id in linked_tids or (uid and uid in th.get_participant_uids()):
            summary = f" — {th.summary.strip()}" if th.summary.strip() else ""
            matched_threads.append(f"  · [{th.status}] {th.title}{summary}")
            if len(matched_threads) >= 5:
                break
    if matched_threads:
        lines.append("- 关联故事线：\n" + "\n".join(matched_threads))

    if len(lines) == 1:
        lines.append("- 暂无已记录的结构化事实。")
    return "\n".join(lines)


@ai_tools(
    category="common",
    capability_domain="用户档案",
    code_callable=True,
)
async def lookup_person_memory(
    ctx: RunContext[ToolContext],
    person_query: str,
    include_history: bool = False,
) -> str:
    """按昵称、外号或账号查询群成员的结构化长期档案、外号表、传闻与关联故事线。

    群聊内仅查询本群成员（与他群物理隔离）；私聊内仅允许查询当前说话人本人的跨群自身档案。

    Args:
        ctx: 工具执行上下文
        person_query: 目标人物的昵称、外号或用户 ID（查自己可填「我」或自身昵称）
        include_history: 是否附带已更新/已撤回的历史变更记录，默认 False
    """
    ev = ctx.deps.ev
    if ev is None or not ev.user_id:
        return "⚠️ 缺少当前用户上下文，无法查询人物档案。"
    if async_maker is None:
        return "⚠️ 记忆数据库未初始化。"

    q = (person_query or "").strip()
    if not q:
        return "⚠️ 请提供要查询的人物昵称、外号或用户 ID。"

    current_uid = str(ev.user_id).strip()
    group_id = str(ev.group_id).strip() if ev.group_id else ""
    extra_sensitive = _get_sensitive_extra_terms()

    if group_id:
        scope_key = make_scope_key(ScopeType.GROUP, group_id)
        if q.casefold() in _SELF_QUERY_WORDS:
            me = await AIMemEntity.find_by_user_id(scope_key, current_uid)
            targets = [me] if me is not None else []
        else:
            targets = await resolve_member_by_query(scope_key, q, limit=3)
        if not targets:
            return f"本群档案中暂未找到与「{q}」匹配的成员记录。"

        target_ids = [t.id for t in targets]
        async with async_maker() as session:
            stmt = (
                select(AIMemEdge)
                .where(
                    col(AIMemEdge.scope_key) == scope_key,
                    or_(
                        col(AIMemEdge.source_entity_id).in_(target_ids),
                        col(AIMemEdge.target_entity_id).in_(target_ids),
                    ),
                )
                .order_by(desc(col(AIMemEdge.valid_at)))
                .limit(120)
            )
            all_edges = list((await session.execute(stmt)).scalars().all())
        threads = (
            await AIMemThread.list_by_scope(scope_key, limit=50)
            if include_history
            else await AIMemThread.list_active_or_dormant(scope_key, limit=30)
        )

        blocks: list[str] = []
        for ent in targets:
            ent_edges = [e for e in all_edges if e.source_entity_id == ent.id or e.target_entity_id == ent.id]
            blocks.append(
                _render_entity_dossier(
                    ent,
                    ent_edges,
                    threads,
                    is_self=bool(ent.user_id and ent.user_id == current_uid),
                    include_history=include_history,
                    is_private=False,
                    extra_sensitive=extra_sensitive,
                )
            )
        return "【人物档案】\n" + "\n\n".join(blocks)

    # 私聊：单向只读汇聚本人自身档案，严禁查询同群其他成员。
    ug_scope = make_scope_key(ScopeType.USER_GLOBAL, current_uid)
    bot_id = str(ev.bot_id or "")
    group_scopes = await resolve_user_group_scopes(current_uid, bot_id=bot_id, cap=25)
    all_scopes = [ug_scope, *group_scopes]

    async with async_maker() as session:
        own_rows = list(
            (
                await session.execute(
                    select(AIMemEntity)
                    .where(col(AIMemEntity.scope_key).in_(all_scopes), col(AIMemEntity.user_id) == current_uid)
                    .order_by(desc(col(AIMemEntity.is_speaker)), desc(col(AIMemEntity.updated_at)))
                )
            )
            .scalars()
            .all()
        )
    own_surfaces = {current_uid.casefold()} | _SELF_QUERY_WORDS
    for row in own_rows:
        own_surfaces.add(row.name.casefold())
        for al in row.get_aliases():
            own_surfaces.add(al.casefold())
    if q.casefold() not in own_surfaces:
        return "出于群聊隐私边界，私聊中只能查询你本人的跨群档案，不能调阅其他群友的档案。"
    if not own_rows:
        return "目前还没有记录下你的结构化个人档案。"

    own_ids = [r.id for r in own_rows]
    async with async_maker() as session:
        edge_rows = list(
            (
                await session.execute(
                    select(AIMemEdge)
                    .where(
                        col(AIMemEdge.scope_key).in_(all_scopes),
                        col(AIMemEdge.source_entity_id).in_(own_ids),
                    )
                    .order_by(desc(col(AIMemEdge.valid_at)))
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    own_threads: list[AIMemThread] = []
    for sk in all_scopes:
        th_rows = (
            await AIMemThread.list_by_scope(sk, limit=25)
            if include_history
            else await AIMemThread.list_active_or_dormant(sk, limit=10)
        )
        for th in th_rows:
            if sk == ug_scope or current_uid in th.get_participant_uids():
                own_threads.append(th)

    primary = own_rows[0]
    merged_aliases: list[str] = []
    seen_a: set[str] = {primary.name}
    for row in own_rows:
        for cand in [row.name, *row.get_aliases()]:
            val = cand.strip()
            if val and val != current_uid and val not in seen_a:
                seen_a.add(val)
                merged_aliases.append(val)
    proxy = AIMemEntity(
        id=primary.id,
        scope_key=ug_scope,
        name=primary.name,
        summary=primary.summary,
        user_id=current_uid,
        is_speaker=True,
        entity_kind="speaker",
        qdrant_id=primary.qdrant_id or primary.id,
    )
    proxy.set_aliases(merged_aliases)
    return "【个人跨群档案】\n" + _render_entity_dossier(
        proxy,
        edge_rows,
        own_threads,
        is_self=True,
        include_history=include_history,
        is_private=True,
        extra_sensitive=extra_sensitive,
    )


__all__ = ["lookup_person_memory"]
