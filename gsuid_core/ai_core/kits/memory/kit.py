"""``gscore.memory``：记忆套件。

记忆的读路径已收敛成**一个** ``cognition`` 门面调用，所以本套件包的是一处，而不是历史上
的四处（寒暄门 + 双路检索 + 预算格式化 + 装配层再硬截一刀）。
实现仍在 ``ai_core/memory/`` 与 ``ai_core/cognition/``。

挂点：H00 入站观察 · H05 检索
· H06 注入 · H18 工具轨迹。
关槽 = 不注册 = 自然跳过；内核里**不写** ``if enable_memory``（闸门应过滤，不该整轮跳过）。

记忆子系统的 bring-up 归 ``startup._INIT_STEPS``（它要排在 RAG 之后拿 Embedding），
本套件不带 ``init_step``（否则同一个初始化每次启动跑两遍）。

评测与生产统一走 dual_route + to_prompt_text，不走旁路转储。
评测 ``create_by`` 必须是 Chat；TEST 会改装配/闸门，不能当评测入口。
"""

from typing import TYPE_CHECKING, Set, List, TypedDict

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit

if TYPE_CHECKING:
    from gsuid_core.ai_core.cognition import CogScope
    from gsuid_core.ai_core.classifier.turn_router import MemoryIntent
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

# 记忆检索强度全部由前置路由的 memory_intent 决定；本模块不再扫文本猜语义。


def _format_memory_catalog(mem: "MemoryContext", _query: str = "") -> str:
    """标题目录卡：偏好极性 + episode 标题 + 边摘要，不灌 dual_route 正文。"""
    from gsuid_core.ai_core.memory.retrieval.lexical import query_overlaps_text

    lines = ["[记忆目录]"]
    shown = 0
    cap = 6

    def _add(text: str) -> bool:
        nonlocal shown
        body = text.replace("\n", " ").strip()
        if not body:
            return True
        shown += 1
        lines.append(f"{shown}. {body[:64]}")
        return shown < cap

    matched_prefs: List[str] = []
    unmatched_prefs: List[str] = []
    for pref in mem.preferences:
        rule = pref["preference_rule"].replace("\n", " ").strip()
        if not rule:
            continue
        pol = pref["polarity"]
        tag = "勿" if pol == "dont" else ("须" if pol == "do" else "")
        corr = "纠正过" if pref["is_correction"] else ""
        mark = "/".join(part for part in (tag, corr) if part)
        item = f"[{mark}] {rule}" if mark else rule
        if _query and not query_overlaps_text(_query, rule):
            unmatched_prefs.append(item)
        else:
            matched_prefs.append(item)
    for item in matched_prefs:
        if not _add(item):
            break
    if shown < cap:

        def _asst(raw: str) -> bool:
            low = raw.lstrip().lower()
            return low.startswith("assistant:") or raw.lstrip().startswith("[我此前说过]")

        user_eps = [e for e in mem.episodes if not _asst(e["content"] or "")]
        asst_eps = [e for e in mem.episodes if _asst(e["content"] or "")]
        for ep in user_eps + asst_eps:
            ts = (ep["valid_at"] or "").strip()[:10]
            body = (ep["content"] or "").strip()
            if not _add(f"{ts} {body}" if ts else body):
                break
    if shown < cap:
        for item in unmatched_prefs:
            if not _add(item):
                break
    if shown < cap:
        for edge in mem.edges:
            fact = edge["fact"].replace("\n", " ").strip()
            if not fact:
                continue
            if not _add(fact.split("。", 1)[0]):
                break
    if shown == 0:
        return ""
    lines.append("（详情 search_cognition / read_handle）")
    return "\n".join(lines)


def retrieve_query_for_search(query: str) -> str:
    """检索用 query：剥墙钟行和 eval「当前时间」前缀。"""
    from gsuid_core.ai_core.interaction_scaffold import extract_message_body
    from gsuid_core.ai_core.memory.retrieval.lexical import strip_clock_lines

    body = strip_clock_lines(extract_message_body(query))
    return body or query.strip()


def memory_intent_from_ctx(ctx: AgentHookContext) -> "MemoryIntent":
    """读取前置路由结论；缺失时按点查处理（不是按闲聊跳过）。"""

    decision = ctx.route_decision
    if decision is None:
        return "point_latest"
    if ctx.memory_eval and decision.memory_intent == "none":
        return "point_latest"
    return decision.memory_intent


def refine_retrieved_memory(mem: "MemoryContext", intent: "MemoryIntent", query: str = "") -> None:
    """保留检索和重排得到的真实相关度排序。"""
    del mem, intent, query


def wants_evidence_injection(ctx: AgentHookContext) -> bool:
    """有历史依赖或问答/工具意图就注入正文；纯寒暄只给目录卡。"""
    if ctx.memory_eval:
        return True
    decision = ctx.route_decision
    if decision is None:
        return True
    if decision.is_live_lookup:
        return False
    if decision.memory_intent != "none":
        return True
    return ctx.intent in ("问答", "工具") or bool(decision.needs_committed_work)


def format_retrieved_memory(ctx: AgentHookContext, mem: "MemoryContext") -> str:
    """Chat 短闲聊目录卡；问答/长问句走统一的 ``to_prompt_text``。"""
    ctx.memory_covered = mem.covered
    q = retrieve_query_for_search(ctx.query)
    if not wants_evidence_injection(ctx):
        return _format_memory_catalog(mem, q)
    from gsuid_core.ai_core.kits.base import inject_memory_cap

    intent = memory_intent_from_ctx(ctx)
    cap = inject_memory_cap(q, covered=mem.covered, memory_intent=intent)
    speakers = ctx.priority_speakers if ctx.priority_speakers else None
    current = {ctx.user_id} if ctx.user_id else None
    return mem.to_prompt_text(
        max_chars=max(512, cap - 240),
        query=q,
        priority_speakers=speakers,
        current_speaker_ids=current,
        now=ctx.clock_at,
        memory_intent=intent,
    )


def should_prefetch_memory(ctx: AgentHookContext) -> bool:
    """实时查数跳过个人历史预灌；其余交给 memory_intent 决定检索深度。"""
    decision = ctx.route_decision
    if decision is not None and decision.is_live_lookup:
        return False
    tg = ctx.turn_graph
    if tg is None or not tg.is_group:
        return True
    if not tg.call_to_self and not tg.needs_task_tools and not tg.soft_triggered:
        return False
    return True


def relevant_preference_contexts(query: str) -> List[str]:
    """按 query 文本近似匹配本轮相关的能力域 / 工具名（选择性偏好注入的一半信号）。

    ``general`` 与纠错规则由检索侧永远保留。能力域多为短中文词按子串命中；工具名多为
    英文按小写子串命中，覆盖「本轮新意图但工具尚未装配进池」的能力域。另一半信号是
    上一轮**实际装配**工具的能力域，由内核写入 ctx.assembled_domains。
    """
    matched: Set[str] = set()
    try:
        from gsuid_core.ai_core.register import get_registered_tools

        low = query.lower()
        for cat_tools in get_registered_tools().values():
            for name, tb in cat_tools.items():
                dom = tb.capability_domain
                if dom and dom in query:
                    matched.add(dom)
                if name and name.lower() in low:
                    matched.add(name)
    except Exception as e:
        logger.debug(t("log.ai.memory_compute_preference_related_fail", e=e))
    return list(matched)


def cog_scope_from_ctx(ctx: AgentHookContext) -> "CogScope":
    """本轮的认知检索 scope。**私聊 group_id 必须 None**（幻影 scope 防回归）。"""
    from gsuid_core.ai_core.cognition import CogScope
    from gsuid_core.ai_core.memory.config import memory_config

    enable_system2 = memory_config.enable_system2 if ctx.enable_system2 is None else ctx.enable_system2
    return CogScope(
        user_id=ctx.user_id,
        bot_id=ctx.bot_id,
        bot_self_id=ctx.bot_self_id,
        group_id=ctx.group_id,
        persona_name=ctx.persona_name or "",
        enable_system2=enable_system2,
        enable_user_global=memory_config.enable_user_global_memory,
        memory_eval=ctx.memory_eval,
        clock_at=ctx.clock_at,
    )


def _in_observe_scope(session_id: str, memory_session: str, ev: Event | None = None) -> bool:
    """被动感知范围：全部群聊 = 全记；按人格配置 = 只记人格覆盖的 session。"""
    if ev is not None:
        from gsuid_core.ai_core.command_aside.runner import _list_allows

        if not _list_allows(ev):
            return False
    if memory_session == "全部群聊":
        return True
    if memory_session != "按人格配置":
        return False
    from gsuid_core.ai_core.persona.config import persona_config_manager

    # 返回非 None 说明该 session 已匹配人格范围
    return persona_config_manager.get_persona_for_session(session_id) is not None


def _speaker_name(ev: Event, fallback: str) -> str:
    sender = ev.sender if isinstance(ev.sender, dict) else {}
    for key in ("card", "nickname", "name"):
        value = sender[key] if key in sender else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


class ObserveMeta(TypedDict):
    speaker_name: str
    reply_to_msg_id: str
    reply_preview: str
    at_targets: tuple[str, ...]


def _observe_meta(ev: Event) -> ObserveMeta:
    reply_id = str(ev.reply_id).strip() if ev.reply_id else ""
    reply_preview = (ev.reply or "").strip()[:120]
    at_targets = tuple(str(item) for item in (ev.at_list or []) if str(item).strip())
    return {
        "speaker_name": _speaker_name(ev, str(ev.user_id or "")),
        "reply_to_msg_id": reply_id,
        "reply_preview": reply_preview,
        "at_targets": at_targets,
    }


def _image_urls(ev: Event) -> List[str]:
    """本条消息的图片 URL（去重）。``ev.image`` 通常已是 image_list 末项。"""
    candidates = [ev.image] + list(ev.image_list or [])
    return list(dict.fromkeys(url for url in candidates if isinstance(url, str) and url))


class MemoryKit(AgentKit):
    """记忆：入站观察 + 认知检索 + 注入 + 工具轨迹。"""

    def register(self) -> None:
        on_agent_hook(AgentHookPoint.ON_INBOUND, priority=110, kit_id=self.kit_id, timeout_ms=500)(self.observe)
        on_agent_hook(AgentHookPoint.AFTER_SESSION, priority=150, kit_id=self.kit_id)(self.observe_active_session)
        on_agent_hook(AgentHookPoint.RETRIEVE_CONTEXT, priority=110, kit_id=self.kit_id)(self.retrieve)
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=150, kit_id=self.kit_id)(self.inject)
        on_agent_hook(AgentHookPoint.ON_TOOL_CALL, priority=110, kit_id=self.kit_id)(self.trace_tool)

    async def observe(self, ctx: AgentHookContext) -> None:
        """入站被动感知（原 ``handler.py`` 的 Memory Observer Hook）。

        **私聊 group_id 必须 None**——observer 按 ``GROUP if group_id else USER_GLOBAL``
        定 scope，回退成 user_id 会把私聊写进幻影 ``group:{user_id}``，而偏好只存
        USER_GLOBAL，于是偏好记忆永远存不进去。
        """
        from gsuid_core.ai_core.memory import observe
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.configs.ai_config import ai_config

        ev = ctx.ev
        if ev is None or not ai_config.get_config("enable_memory").data:
            return
        if not memory_config.observer_enabled or "被动感知" not in memory_config.memory_mode:
            return
        if not _in_observe_scope(ev.session_id, memory_config.memory_session, ev):
            return
        if ev.user_type != "direct" and not ctx.group_id:
            return

        has_text = bool(ev.raw_text and ev.raw_text.strip())
        image_urls = _image_urls(ev)
        if has_text:
            await observe(
                content=ev.raw_text,
                speaker_id=ctx.user_id,
                group_id=ctx.group_id,
                bot_self_id=str(ev.bot_self_id),
                observer_blacklist=memory_config.observer_blacklist,
                message_type="group_msg" if ctx.group_id else "private_msg",
                bot_id=str(ev.bot_id),
                persona_name=ctx.persona_name or "",
                message_id=str(ev.msg_id) if ev.msg_id else None,
                **_observe_meta(ev),
            )
        # 默认关闭：仅当「图片记忆」与「被动感知」同时勾选才静默读图入记忆，
        # 避免后台对每张群图都发起一次视觉模型调用（Token + 日志噪声）。
        if image_urls and "图片记忆" in memory_config.memory_mode:
            from gsuid_core.ai_core.memory.ingestion.multimodal import submit_image_observation

            submit_image_observation(
                image_urls=image_urls,
                speaker_id=ctx.user_id,
                group_id=ctx.group_id,
                bot_self_id=str(ev.bot_self_id),
                observer_blacklist=memory_config.observer_blacklist,
                message_type="group_msg" if ctx.group_id else "private_msg",
            )

    async def observe_active_session(self, ctx: AgentHookContext) -> None:
        """主动会话模式的观察：只在**未开被动感知**时补这一次，防双写。"""
        from gsuid_core.ai_core.memory import observe
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.configs.ai_config import ai_config

        ev = ctx.ev
        if ev is None or not ai_config.get_config("enable_memory").data:
            return
        modes = memory_config.memory_mode
        if "主动会话" not in modes or "被动感知" in modes:
            return
        await observe(
            content=ev.raw_text,
            speaker_id=ctx.user_id,
            group_id=ctx.group_id,
            bot_self_id=str(ev.bot_self_id),
            observer_blacklist=memory_config.observer_blacklist,
            message_type="group_msg" if ctx.group_id else "private_msg",
            bot_id=str(ev.bot_id),
            persona_name=ctx.persona_name or "",
            message_id=str(ev.msg_id) if ev.msg_id else None,
            **_observe_meta(ev),
        )

    async def retrieve(self, ctx: AgentHookContext) -> None:
        """H05：旁观不预灌；Chat 短闲聊给目录卡；问答/长记忆检索全量证据。"""
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.configs.ai_config import ai_config
        from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

        if not ai_config.get_config("enable_memory").data or not memory_config.enable_retrieval:
            return
        if ctx.skip_memory:
            return
        if not should_prefetch_memory(ctx):
            return
        search_q = retrieve_query_for_search(ctx.query)
        memory_intent = memory_intent_from_ctx(ctx)

        pref_contexts: List[str] = []
        if ctx.intent != "闲聊":
            domains: Set[str] = set(relevant_preference_contexts(search_q))
            domains.update(ctx.assembled_domains)
            pref_contexts = list(domains)
        scope = cog_scope_from_ctx(ctx)
        top_k = int(memory_config.retrieval_top_k)
        mem = await dual_route_retrieve(
            search_q,
            ctx.user_id,
            enable_system2=scope.enable_system2,
            group_id=scope.group_id,
            top_k=top_k,
            enable_user_global=scope.enable_user_global,
            inject_preferences=True,
            preference_contexts=pref_contexts,
            bot_id=scope.bot_id,
            bot_self_id=scope.bot_self_id,
            include_self=True,
            persona_name=scope.persona_name,
            memory_intent=memory_intent,
            clock_at=ctx.clock_at,
        )
        from gsuid_core.ai_core.memory.retrieval.types import Episode
        from gsuid_core.ai_core.memory.retrieval.lexical import expand_lexical_recall

        reserved_turns: list[Episode] = []
        if mem.covered or memory_intent == "none":
            mem.reserved_episodes = reserved_turns
        else:
            mem.episodes = await expand_lexical_recall(
                mem.episodes,
                query=search_q,
                user_id=ctx.user_id,
                group_id=ctx.group_id,
                clock=ctx.clock_at,
                reserved=reserved_turns,
                memory_intent=memory_intent,
            )
            mem.reserved_episodes = reserved_turns
        if wants_evidence_injection(ctx):
            from gsuid_core.ai_core.memory.retrieval.lexical import (
                looks_like_count_query,
                expand_episode_neighbors,
                expand_topic_session_turns,
                looks_like_personal_upkeep_query,
            )

            # 跨会话演进与时间线模态优先保留广度，不在单会话邻条上消耗预算。
            if (
                memory_intent not in ("timeline_order", "broad_summary")
                and looks_like_count_query(search_q)
                and not mem.temporal_mode
            ):
                mem.episodes = await expand_episode_neighbors(mem.episodes)
            elif looks_like_personal_upkeep_query(search_q) and not mem.temporal_mode:
                mem.episodes = await expand_topic_session_turns(mem.episodes, search_q)
            refine_retrieved_memory(mem, memory_intent, search_q)
        text = format_retrieved_memory(ctx, mem)
        if text:
            ctx.stash_retrieved("memory", text)
            logger.debug(t("log.ai.memory_retrieved_context_characters", p0=len(text)))
            from gsuid_core.ai_core.statistics import statistics_manager

            statistics_manager.record_memory_retrieval()

        await self._prefetch_cognition(ctx)

    async def _prefetch_cognition(self, ctx: AgentHookContext) -> None:
        """框架代模型预取一次全联邦认知检索（H05 的设计目的）。

        **与 D-11 的差别**（否则会被当「强制前置 RAG 回潮」打回）：
        ① 有门，不是每轮——只在问答/工具意图或回指词命中时跑；
        ② 闲聊仍 0 检索；
        ③ 注入的是**目录卡 + 句柄**，不是全文（深读仍走 ``read_handle``）。

        默认关（``cognition_prefetch_enable``），灰度后再翻。
        """
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if not ai_config.get_config("cognition_prefetch_enable").data:
            return
        intent = ctx.intent or ""
        memory_dependent = ctx.route_decision is not None and ctx.route_decision.memory_intent != "none"
        if intent not in ("问答", "工具") and not memory_dependent:
            logger.debug(t("log.ai.cognition_prefetch_skip", reason=f"intent={intent or '-'} 且无历史依赖"))
            return

        from gsuid_core.ai_core.cognition import ALL_KINDS, search_cognition
        from gsuid_core.ai_core.cognition.facade import render_cognition_block

        hits = await search_cognition(
            ctx.query,
            kinds=ALL_KINDS,
            scope=cog_scope_from_ctx(ctx),
            limit=8,
            memory_intent=memory_intent_from_ctx(ctx),
        )
        if not hits:
            return
        block = render_cognition_block(ctx.query, hits, header="已检索·目录")
        ctx.stash_retrieved("cognition_prefetch", block)
        logger.info(t("log.ai.cognition_prefetch", intent=intent or "-", n=len(hits)))

    async def inject(self, ctx: AgentHookContext) -> None:
        """把 H05 暂存的检索结果写成正式 ``memory`` 块（预算已在检索侧生效）。"""
        parts: List[str] = []
        text = ctx.retrieved["memory"] if "memory" in ctx.retrieved else ""
        if text:
            parts.append(f"[长期记忆]\n{text}")
            guide = ctx.memory_guide or ""
            if guide:
                parts.append(guide)
        prefetch = ctx.retrieved["cognition_prefetch"] if "cognition_prefetch" in ctx.retrieved else ""
        if prefetch:
            parts.append(prefetch)
        meme_block = await self._meme_preinject(ctx)
        if meme_block:
            parts.append(meme_block)
        if not parts:
            return
        parts.append("（需要更多细节请调 search_cognition / read_handle）")
        ctx.set_context_block("memory", "\n".join(parts))

    async def _meme_preinject(self, ctx: AgentHookContext) -> str:
        """装配期梗触发词精确匹配，最多 2 条。"""
        from gsuid_core.ai_core.meme.database_model import AiMemeKnowledge

        if not ctx.query.strip():
            return ""
        scope_key = f"group:{ctx.group_id}" if ctx.group_id else ""
        try:
            rows = await AiMemeKnowledge.match_terms(
                ctx.query,
                bot_id=ctx.bot_id,
                scope_key=scope_key,
                limit=2,
            )
        except Exception as e:
            logger.debug(t("log.ai.meme_preinject_skip", e=e))
            return ""
        if not rows:
            return ""
        blocked: set[str] = set()
        if ctx.persona_name:
            from gsuid_core.ai_core.memory.group_profile import collect_persona_surfaces

            blocked = {s.casefold() for s in collect_persona_surfaces(ctx.persona_name)}
        lines = ["[群聊黑话]"]
        hit_ids: list[int] = []
        for row in rows:
            term = (row.term or "").strip()
            if term.casefold() in blocked:
                continue
            meaning = (row.meaning or "")[:80]
            src = row.source or "未知"
            lines.append(f'"{term}"：{meaning}（来源：{src}）')
            if row.id is not None:
                hit_ids.append(int(row.id))
        if len(lines) == 1:
            return ""
        for hid in hit_ids:
            await AiMemeKnowledge.bump_hit(hid)
        return "\n".join(lines)

    async def trace_tool(self, ctx: AgentHookContext) -> None:
        """工具调用轨迹入记忆（供偏好蒸馏作背景，判「刚纠正完」）。"""
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.memory.ingestion.tool_trace import record_tool_call

        if not ctx.tool_name or not ctx.user_id or not memory_config.enable_preference_memory:
            return
        bot_id = ctx.ev.bot_id if ctx.ev is not None else ""
        record_tool_call(ctx.user_id, ctx.tool_name, ctx.tool_args, bot_id=bot_id)


KIT = register_agent_kit(
    MemoryKit(
        kit_id="gscore.memory",
        slot="memory",
        display_name="长期记忆",
        owns_tools=(),
    )
)
