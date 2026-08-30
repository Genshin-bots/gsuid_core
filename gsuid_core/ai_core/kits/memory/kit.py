"""``gscore.memory``：记忆套件。

记忆的读路径已收敛成**一个** ``cognition`` 门面调用，所以本套件包的是一处，而不是历史上
的四处（寒暄门 + 双路检索 + 预算格式化 + 装配层再硬截一刀）。
实现仍在 ``ai_core/memory/`` 与 ``ai_core/cognition/``。

挂点：H00 入站观察 · H05 检索（唯一允许的长超时 15s）· H06 注入 · H18 工具轨迹。
关槽 = 不注册 = 自然跳过；内核里**不写** ``if enable_memory``（闸门应过滤，不该整轮跳过）。

记忆子系统的 bring-up 归 ``startup._INIT_STEPS``（它要排在 RAG 之后拿 Embedding），
本套件不带 ``init_step``（否则同一个初始化每次启动跑两遍）。

LongMem 证据转储不在本文件：见 ``eval_protocol.py``，仅 ``memory_eval`` 时懒加载。
评测 ``create_by`` 必须是 Chat；TEST 会改装配/闸门，不能当评测入口。
"""

import re
from typing import TYPE_CHECKING, Set, List

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit

if TYPE_CHECKING:
    from gsuid_core.ai_core.cognition import CogScope
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

# C4 寒暄门控：回指 / 实体 / 任务引用词，命中则强制检索
_FORCE_RETRIEVE_RE = re.compile(
    r"(之前|上次|上回|那个|那次|昨天|前几天|你说过|你不是说|记不记得|还记得|提到过|任务|计划|进度)"
)
# C4 / C3-c：明显情绪词，命中则强制检索（避免错过用户昨日事件背景）
_EMOTION_RETRIEVE_RE = re.compile(r"(难过|崩溃|沉船|破防|开心死|伤心|焦虑|想哭|绝望|委屈|孤独)")
# 可能含实体的特征（英文词 / 引号内容 / 长串中文）
_ENTITY_HINT_RE = re.compile(r"([A-Za-z]{3,}|[「『\"“].+|[一-鿿]{6,})")
# 「短寒暄」的长度上限，与关系温度的 meaningful 判据同源
_CHITCHAT_SHORT_LEN = 12
# LongMem inject_date 前缀；剥掉后再检索，避免日期词带偏向量。
_EVAL_NOW_PREFIX_RE = re.compile(r"^(?:当前时间[：:]\s*[^\n]+\n+)+")


def _format_memory_catalog(mem: "MemoryContext") -> str:
    """标题目录卡：偏好极性 + episode 标题 + 边摘要，不灌 dual_route 正文。"""
    lines = ["[记忆目录]"]
    shown = 0

    def _add(text: str) -> bool:
        nonlocal shown
        body = text.replace("\n", " ").strip()
        if not body:
            return True
        shown += 1
        lines.append(f"{shown}. {body[:40]}")
        return shown < 8

    for pref in mem.preferences:
        rule = pref["preference_rule"].replace("\n", " ").strip()
        if not rule:
            continue
        pol = pref["polarity"]
        tag = "勿" if pol == "dont" else ("须" if pol == "do" else "")
        corr = "纠正过" if pref["is_correction"] else ""
        mark = "/".join(part for part in (tag, corr) if part)
        item = f"[{mark}] {rule}" if mark else rule
        if not _add(item):
            break
    if shown < 8:
        for ep in mem.episodes:
            if not _add(ep["content"]):
                break
    if shown < 8:
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

    body = extract_message_body(query)
    body = _EVAL_NOW_PREFIX_RE.sub("", body).strip()
    return body or query.strip()


def format_retrieved_memory(ctx: AgentHookContext, mem: "MemoryContext") -> str:
    """Chat 默认目录卡；仅 ``memory_eval`` 灌完整证据片段（create_by 应是 Chat）。"""
    if not ctx.memory_eval:
        return _format_memory_catalog(mem)
    from gsuid_core.ai_core.kits.memory.eval_protocol import format_eval_memory

    return format_eval_memory(mem, retrieve_query_for_search(ctx.query))


def should_prefetch_memory(ctx: AgentHookContext) -> bool:
    """闲聊/旁观不预灌。点名或任务跟进才允许目录卡。"""
    tg = ctx.turn_graph
    if tg is None or not tg.is_group:
        return True
    if not tg.call_to_self and not tg.needs_task_tools:
        return False
    if ctx.cheap_gate == "light":
        return False
    if (ctx.intent or "") == "闲聊" and not tg.needs_task_tools:
        return False
    return True


def should_retrieve(query: str, intent: str, user_id: str) -> bool:
    """C4 寒暄门控（纯规则，无 LLM）：本轮值不值得开贵检索窗。

    只有"短 + 闲聊 + 无实体 + 无情绪 + 无回指 + 非任务引用"同时满足才跳过；
    主人 / 回指 / 情绪 / 实体一律强制检索，避免漏掉重要背景。

    门控在**套件内部**而不是内核：内核写 ``if enable_memory`` 会变成「整条链路跳过」，
    而闸门只该降级检索强度。关槽 = 不注册 = 自然跳过。
    """
    from gsuid_core.ai_core.utils import _is_master_user

    q = query.strip()
    if _is_master_user(str(user_id)):
        return True
    if _FORCE_RETRIEVE_RE.search(q) or _EMOTION_RETRIEVE_RE.search(q):
        return True
    if intent == "闲聊" and len(q) < _CHITCHAT_SHORT_LEN and not _ENTITY_HINT_RE.search(q):
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
        enable_system2=enable_system2,
        enable_user_global=memory_config.enable_user_global_memory,
        memory_eval=ctx.memory_eval,
    )


def _in_observe_scope(session_id: str, memory_session: str) -> bool:
    """被动感知范围：全部群聊 = 全记；按人格配置 = 只记人格覆盖的 session。"""
    if memory_session == "全部群聊":
        return True
    if memory_session != "按人格配置":
        return False
    from gsuid_core.ai_core.persona.config import persona_config_manager

    # 返回非 None 说明该 session 已匹配人格范围
    return persona_config_manager.get_persona_for_session(session_id) is not None


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
        if not _in_observe_scope(ev.session_id, memory_config.memory_session):
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
        )

    async def retrieve(self, ctx: AgentHookContext) -> None:
        """H05：旁观不预灌；Chat 给目录卡；仅 memory_eval 灌证据会话。"""
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.configs.ai_config import ai_config
        from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

        if not ai_config.get_config("enable_memory").data or not memory_config.enable_retrieval:
            return
        if not should_prefetch_memory(ctx):
            return
        search_q = retrieve_query_for_search(ctx.query)
        if not should_retrieve(search_q, ctx.intent or "", ctx.user_id):
            logger.debug(t("log.ai.memory_skip_hit_small_talk_gate"))
            return

        pref_contexts: List[str] = []
        if ctx.intent != "闲聊":
            domains: Set[str] = set(relevant_preference_contexts(search_q))
            domains.update(ctx.assembled_domains)
            pref_contexts = list(domains)
        scope = cog_scope_from_ctx(ctx)
        top_k = memory_config.retrieval_top_k
        if ctx.memory_eval:
            from gsuid_core.ai_core.kits.memory.eval_protocol import EVAL_RETRIEVAL_TOP_K

            top_k = max(int(top_k), EVAL_RETRIEVAL_TOP_K)
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
        )
        if ctx.memory_eval:
            from gsuid_core.ai_core.kits.memory.eval_protocol import boost_retrieved_memory

            mem.seed_ids = [e["id"] for e in mem.episodes[:12]]
            await boost_retrieved_memory(mem, search_q, ctx.user_id, ctx.group_id)
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
        anaphora = bool(_FORCE_RETRIEVE_RE.search(ctx.query))
        if intent not in ("问答", "工具") and not anaphora:
            logger.debug(t("log.ai.cognition_prefetch_skip", reason=f"intent={intent or '-'} 且无回指"))
            return

        from gsuid_core.ai_core.cognition import ALL_KINDS, search_cognition
        from gsuid_core.ai_core.cognition.facade import render_cognition_block

        hits = await search_cognition(
            ctx.query,
            kinds=ALL_KINDS,
            scope=cog_scope_from_ctx(ctx),
            limit=8,
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
            if ctx.memory_eval:
                from gsuid_core.ai_core.kits.memory.eval_protocol import inject_eval_memory_parts

                parts.extend(inject_eval_memory_parts(text, ctx.memory_guide or ""))
            else:
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
        if not ctx.memory_eval:
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
