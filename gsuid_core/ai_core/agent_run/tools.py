"""阶段 B：工具五层装配 + 构建 pydantic-ai Agent"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Iterable, Protocol, Sequence
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.tools import Tool
from pydantic_ai.settings import ModelSettings, merge_model_settings
from pydantic_ai.capabilities import AbstractCapability

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core import (
    interaction_scaffold,
)
from gsuid_core.ai_core.const import (
    _SKILLS_CREATE_BY,
    _AGENTIC_CREATE_BY,
)
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.skills import skills_toolset
from gsuid_core.ai_core.register import find_tool_base, get_tools_by_capability_domain
from gsuid_core.ai_core.thinking import _normalize_thinking_tags
from gsuid_core.ai_core.rag.tools import (
    ToolList,
    get_main_agent_tools,
    get_scope_context_tags,
    pin_trigger_keyword_hits,
    align_seeds_to_context_plugin,
    search_tools_with_entity_routing,
)
from gsuid_core.ai_core.tool_risk import TOOLS_EXECUTION_GATED_KEY, skill_tool_visible
from gsuid_core.ai_core.tool_safety import build_tool_safety_capability
from gsuid_core.ai_core.control.lines import turn_seed_hint_line
from gsuid_core.ai_core.agent_run.host import RunOnceHost
from gsuid_core.ai_core.agent_run.state import (
    RunOnceState,
    _require_context,
)
from gsuid_core.ai_core.agent_run.support import (
    _INTERACTIVE_CREATE_BY,
    _MAIN_PERSONA_CREATE_BY,
    _append_user_text,
    _pool_overlaps_capability_agent,
    _capability_exclusive_tool_names,
    _matched_delegation_only_profile,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.tool_state_signals import STATE_DRIVEN_FAMILY_DOMAINS
from gsuid_core.ai_core.configs.attribution import resolve_attribution_settings
from gsuid_core.ai_core.agent_run.remote_web_search import attach_remote_web_search

if TYPE_CHECKING:
    from gsuid_core.ai_core.entity_index import PluginLock
    from gsuid_core.ai_core.agent_node.tool_scope import ToolScope

_PROGRESS_TOOL = "check_delegation"
_PINNED_SESSION_TOOLS: tuple[str, ...] = ("find_tools", "call_tool", "create_subagent")
_SCHED_CREATE_NAMES: frozenset[str] = frozenset({"schedule_task"})
_SCHED_MUTATE_NAMES: frozenset[str] = frozenset(
    {
        "list_scheduled_tasks",
        "query_scheduled_task",
        "modify_scheduled_task",
        "cancel_scheduled_task",
        "pause_scheduled_task",
        "resume_scheduled_task",
    }
)


_SESSION_TOOL_CEILING: int = 24
# 人格节点 tool_names 里的旧核工具：永冻主会话不再把它们钉进 tools[]，按需经 call_tool 调用。
_FROZEN_CORE_RETIRED: frozenset[str] = frozenset({"read_video", "capability_map"})


def should_skip_tool_search(
    *,
    in_flight_short: bool,
    is_group: bool,
    followup_detected: bool,
    has_active_task: bool,
    has_media: bool,
    call_to_self: bool,
    intent: str = "",
) -> bool:
    """向量预检索是否跳过。点名必搜；旁观/在途短轮才跳。"""
    if in_flight_short:
        return True
    if followup_detected or has_active_task or has_media or call_to_self:
        return False
    if is_group:
        return True
    return intent == "闲聊"


def is_group_send_extra(name: str) -> bool:
    """对用户发送 extras（不在通道核）。静态快照不收，本轮种子可以收。"""
    return name.startswith("send_") and name not in interaction_scaffold.MAIN_AGENT_CORE_TOOLS


_TURN_SEED_CAP = 4


class _NamedTool(Protocol):
    name: str


def pick_turn_seed_names(
    tools: Sequence[_NamedTool],
    seeds: Sequence[_NamedTool],
    *,
    exclusive: set[str],
    cap: int = _TURN_SEED_CAP,
) -> list[str]:
    """挑本轮提示种子，只算不装：跳过已装/空名/exclusive，至多 cap 个。"""
    seen = {tool.name for tool in tools}
    picked: list[str] = []
    for tool in seeds:
        if len(picked) >= cap:
            break
        name = tool.name
        if not name or name in seen or name in exclusive:
            continue
        seen.add(name)
        picked.append(name)
    return picked


# 状态种子（审批 / 在途任务 / 提醒管理）独立槽位，不与检索种子争 _TURN_SEED_CAP。
_PRIORITY_SEED_CAP = 12
# 未开 rerank 时向量分偏噪，召回阈值至少抬到这里，防弱相关工具挤进本轮提示。
_NO_RERANK_RECALL_FLOOR = 0.52


def effective_tool_recall_threshold() -> float:
    """本轮工具召回阈值：取配置值；未开 rerank 时不低于 _NO_RERANK_RECALL_FLOOR。"""
    from gsuid_core.ai_core.rag.base import is_enable_rerank

    threshold = float(ai_config.get_config("tool_recall_threshold").data)
    return threshold if is_enable_rerank() else max(threshold, _NO_RERANK_RECALL_FLOOR)


@dataclass
class TurnSeedRecall:
    """本轮工具检索结果：found 为原始召回，seeds 为触发词置顶 + 插件锁对齐后的候选。"""

    found: ToolList
    seeds: ToolList
    lock: "PluginLock"
    scan_text: str


async def recall_turn_seeds(
    *,
    search_query: str,
    route_text: str,
    utterance: str,
    background: str,
    limit: int,
    exclude_names: set[str],
    scope: "ToolScope",
    scope_key: str = "",
    ignore_surfaces: tuple[str, ...] = (),
) -> TurnSeedRecall:
    """生产装配与 assemble_preview 共用：实体路由召回 → 触发词置顶 → 插件锁对齐。"""
    from gsuid_core.ai_core.entity_index import lock_plugin, strip_surfaces

    found = await search_tools_with_entity_routing(
        query=search_query,
        route_text=route_text,
        limit=limit,
        threshold=effective_tool_recall_threshold(),
        scope_key=scope_key,
        ignore_surfaces=ignore_surfaces,
        exclude_names=exclude_names,
        scope=scope,
    )
    seeds = pin_trigger_keyword_hits(route_text, found, limit=_TURN_SEED_CAP, scope=scope)
    scan = strip_surfaces(utterance, ignore_surfaces) if ignore_surfaces else utterance
    lock = lock_plugin(scan, background)
    if lock.plugin:
        seeds = await align_seeds_to_context_plugin(seeds, lock.plugin, scan or route_text, scope, hard=lock.explicit)
    return TurnSeedRecall(found=found, seeds=seeds, lock=lock, scan_text=scan)


async def select_turn_hint_names(
    present: Sequence[_NamedTool],
    priority_seeds: Sequence[_NamedTool],
    turn_seeds: Sequence[_NamedTool],
    *,
    query: str,
    bot_id: str,
    user_id: str,
) -> tuple[list[str], list[str]]:
    """挑本轮只提示、不进 tools[] 的种子，返回 (状态种子, 检索种子)；私有 MCP 过预推门控。"""
    from gsuid_core.ai_core.mcp.node_profile import mcp_hint_allowed_for_turn

    exclusive = _capability_exclusive_tool_names()
    prio = pick_turn_seed_names(present, priority_seeds, exclusive=exclusive, cap=_PRIORITY_SEED_CAP)
    rag = pick_turn_seed_names(
        present,
        [s for s in turn_seeds if s.name not in prio],
        exclusive=exclusive,
        cap=_TURN_SEED_CAP,
    )

    async def _hintable(names: list[str]) -> list[str]:
        kept: list[str] = []
        for name in names:
            tb = find_tool_base(name)
            if tb is None:
                continue
            if tb.category == "mcp" and not await mcp_hint_allowed_for_turn(
                name, query, bot_id=bot_id, user_id=user_id
            ):
                continue
            kept.append(name)
        return kept

    return await _hintable(prio), await _hintable(rag)


# 回想核内只有 search_cognition；attach_article 走 find_tools，避免每轮写工具进 schema。
_KERNEL_NO_FAMILY_CLOSE_DOMAINS = frozenset({"回想"})


def complete_kernel_family_names(core_names: Sequence[str], *, exclusive: set[str]) -> list[str]:
    """核内已出现的非状态驱动域，把该域非 exclusive、非发送 extras 一并进快照。"""
    seen: set[str] = set()
    out: list[str] = []

    def _add(name: str) -> None:
        if not name or name in seen or name in exclusive:
            return
        if is_group_send_extra(name) and name not in core_names:
            return
        seen.add(name)
        out.append(name)

    for name in core_names:
        _add(name)
    domains: list[str] = []
    for name in list(out):
        tb = find_tool_base(name)
        domain = tb.capability_domain if tb is not None else None
        if domain and domain not in domains:
            domains.append(domain)
    for domain in domains:
        if domain in STATE_DRIVEN_FAMILY_DOMAINS or domain in _KERNEL_NO_FAMILY_CLOSE_DOMAINS:
            continue
        for tb in get_tools_by_capability_domain(domain):
            _add(tb.name)
    return out


def stabilize_session_tool_names(
    frozen: list[str] | None,
    incoming: Sequence[str],
    *,
    exclusive: set[str],
    ceiling: int,
    pin: tuple[str, ...] = _PINNED_SESSION_TOOLS,
) -> list[str]:
    """Append-only 会话工具名。frozen 空则拍快照；否则只在末尾 append，超顶丢新名。"""
    seen: set[str] = set()
    out: list[str] = []

    def _try_add(name: str) -> None:
        if not name or name in seen or name in exclusive:
            return
        if len(out) >= ceiling:
            return
        seen.add(name)
        out.append(name)

    if frozen is None:
        for name in pin:
            _try_add(name)
        for name in incoming:
            _try_add(name)
        return out

    for name in frozen:
        if not name or name in exclusive or name in seen:
            continue
        seen.add(name)
        out.append(name)
    for name in incoming:
        _try_add(name)
    return out


def _without_progress_tool(tools: ToolList) -> ToolList:
    return [t for t in tools if t.name != _PROGRESS_TOOL]


def tools_by_names(names: Sequence[str], preferred: ToolList) -> ToolList:
    """按名单解析工具；同名优先用 preferred 里的实现（调用方默认工具覆盖全局注册）。"""
    by_name: dict[str, Tool[ToolContext]] = {}
    for t in preferred:
        if t.name not in by_name:
            by_name[t.name] = t
    out: ToolList = []
    for name in names:
        if name in by_name:
            out.append(by_name[name])
            continue
        tb = find_tool_base(name)
        if tb is not None:
            out.append(tb.tool)
    return out


async def build_core_tools(
    caller_tools: ToolList,
    *,
    persona_name: str | None,
    frozen_core: bool,
    interactive: bool,
) -> tuple[ToolList, set[str]]:
    """装配会话保底核工具（调用方置顶 + 通道核 + 人格节点白名单）。永冻主会话不补核内族。"""
    core_tools: ToolList = list(caller_tools)
    core_names: set[str] = {t.name for t in core_tools}
    for mt in await get_main_agent_tools():
        if mt.name not in core_names:
            core_names.add(mt.name)
            core_tools.append(mt)
    if not frozen_core:
        for tn in complete_kernel_family_names(list(core_names), exclusive=_capability_exclusive_tool_names()):
            if tn in core_names:
                continue
            tb = find_tool_base(tn)
            if tb is not None:
                core_names.add(tn)
                core_tools.append(tb.tool)

    # 节点显式白名单：persona 投影节点在 config.json 声明的 tool_names 并入保底。
    # 永冻主会话剥离已退役的旧核工具，避免线缆层 tools[] 抖动击穿缓存。
    if persona_name:
        from gsuid_core.ai_core.agent_node import get_node

        node = get_node(persona_name)
        if node is not None and node.tool_names:
            for tn in node.tool_names:
                if (frozen_core and tn in _FROZEN_CORE_RETIRED) or tn in core_names:
                    continue
                tb = find_tool_base(tn)
                if tb is not None:
                    core_names.add(tn)
                    core_tools.append(tb.tool)

    if interactive:
        core_tools = _without_progress_tool(core_tools)
        core_names.discard(_PROGRESS_TOOL)
    return core_tools, core_names


@dataclass
class TurnAssemblyPreview:
    """单轮主会话装配预览：core 为永冻 tools[]，hinted 为本轮 user 侧提示（经 call_tool 调用）。"""

    core: ToolList
    seeds: ToolList
    hinted: list[str]
    recall_limit: int
    hint_cap: int = _TURN_SEED_CAP


async def preview_main_turn_assembly(query: str, persona_name: str | None = None) -> TurnAssemblyPreview:
    """无会话状态的首轮 Chat 装配：与生产共用核心构建、检索、提示挑选，供控制台与评测。

    不含随会话状态变化的部分（闲聊跳检索、在途任务 / 审批状态种子、群语境标签、私有 MCP 凭据）。
    """
    from gsuid_core.ai_core.memory.group_profile import collect_persona_surfaces
    from gsuid_core.ai_core.agent_node.tool_scope import get_tool_scope

    core_tools, core_names = await build_core_tools(
        [],
        persona_name=persona_name,
        frozen_core=True,
        interactive=True,
    )
    exclusive = _capability_exclusive_tool_names()
    names = stabilize_session_tool_names(
        None,
        list(dict.fromkeys(t.name for t in core_tools)),
        exclusive=exclusive,
        ceiling=_SESSION_TOOL_CEILING,
    )
    core = tools_by_names(names, core_tools)
    recall_limit = int(ai_config.get_config("tool_search_recall").data)
    recall = await recall_turn_seeds(
        search_query=interaction_scaffold.build_tool_search_query(query),
        route_text=query,
        utterance=query,
        background="",
        limit=recall_limit,
        exclude_names=core_names,
        scope=get_tool_scope(persona_name),
        ignore_surfaces=collect_persona_surfaces(persona_name) if persona_name else (),
    )
    _, hinted = await select_turn_hint_names(core, [], recall.seeds, query=query, bot_id="", user_id="")
    return TurnAssemblyPreview(core=core, seeds=recall.seeds, hinted=hinted, recall_limit=recall_limit)


def _frozen_core_eligible_for(host: RunOnceHost) -> bool:
    """永冻核心的唯一判定：主会话首轮拍快照后 tools[] 不再变化，长尾工具一律经 call_tool。"""
    if host.create_by not in _MAIN_PERSONA_CREATE_BY or host.dynamic_tools is False:
        return False
    if host.dynamic_tools is None and host.persona_name:
        from gsuid_core.ai_core.agent_node import get_node, has_dynamic_pack

        node = get_node(host.persona_name)
        if node is not None and not has_dynamic_pack(node.tool_packs):
            return False
    return True


def _frozen_core_active_for(host: RunOnceHost, tool_names: Iterable[str]) -> bool:
    """本轮是否真的挂上永冻核心（eligible 且 call_tool 在表）；装配后的分支只认 st.frozen_core。"""
    return "call_tool" in set(tool_names) and _frozen_core_eligible_for(host)


def _stabilize_session_toolset_for(host: RunOnceHost, core: ToolList, ctx_tags: list[str]) -> ToolList:
    """会话工具表：永冻主会话首轮拍快照后原样复用；其它会话按核心名单 append-only。"""
    host._session_toolset_tags = frozenset(ctx_tags)
    if _frozen_core_eligible_for(host) and host._session_toolset_frozen is not None:
        names = list(host._session_toolset_frozen)
    else:
        names = stabilize_session_tool_names(
            host._session_toolset_frozen,
            list(dict.fromkeys(t.name for t in core)),
            exclusive=_capability_exclusive_tool_names(),
            ceiling=_SESSION_TOOL_CEILING,
        )
        host._session_toolset_frozen = names
    return tools_by_names(names, core)


async def _build_core_tools_for(host: RunOnceHost, caller_tools: ToolList) -> tuple[ToolList, set[str]]:
    return await build_core_tools(
        caller_tools,
        persona_name=host.persona_name,
        frozen_core=_frozen_core_eligible_for(host),
        interactive=host.create_by in _INTERACTIVE_CREATE_BY,
    )


async def _restore_frozen_session_tools_for(host: RunOnceHost, caller_tools: ToolList | None = None) -> ToolList:
    """还原或首轮预冻主会话核心工具表，避免首轮旁白/寻址门导致线缆层 tools[] 漂移。"""
    if host.dynamic_tools is False:
        return list(caller_tools) if caller_tools else []
    if not host._session_toolset_frozen:
        if host.create_by not in _MAIN_PERSONA_CREATE_BY:
            return []
        if not _frozen_core_eligible_for(host):
            return list(caller_tools) if caller_tools else []
        core_tools, _ = await _build_core_tools_for(host, list(caller_tools) if caller_tools else [])
        out = _stabilize_session_toolset_for(host, core_tools, [])
        if host.create_by in _INTERACTIVE_CREATE_BY:
            out = _without_progress_tool(out)
            exclusive = _capability_exclusive_tool_names()
            if exclusive:
                out = [t for t in out if t.name not in exclusive]
                if host._session_toolset_frozen is not None:
                    host._session_toolset_frozen = [n for n in host._session_toolset_frozen if n not in exclusive]
        return out
    out = []
    seen: set[str] = set()
    for name in host._session_toolset_frozen:
        if not name or name in seen:
            continue
        tb = find_tool_base(name)
        if tb is not None:
            seen.add(name)
            out.append(tb.tool)
    for meta_name in ("find_tools", "call_tool"):
        if meta_name not in seen:
            mtb = find_tool_base(meta_name)
            if mtb is not None:
                seen.add(meta_name)
                out.append(mtb.tool)
    return out


async def _handle_aside_blocked_tools(host: RunOnceHost, st: RunOnceState) -> None:
    """命令旁白屏蔽工具分支：还原冻结工具名但锁定执行期门控。"""
    st.tools = await _restore_frozen_session_tools_for(host, st.tools)
    st.tool_names = [t.name for t in st.tools]
    st.exposed_tool_names = []
    st.expose_dynamic = bool(st.tools)
    st.frozen_core = _frozen_core_active_for(host, st.tool_names)
    host._last_assembled_domains = set()
    blocked_ctx = _require_context(st)
    from gsuid_core.ai_core.control.outbound_detectors import EXPOSED_TOOLS_EXTRA_KEY

    blocked_ctx.extra[EXPOSED_TOOLS_EXTRA_KEY] = []
    blocked_ctx.extra[TOOLS_EXECUTION_GATED_KEY] = True
    host._session_logger.log_tools_list(st.tool_names)


def _resolve_assembly_scope(host: RunOnceHost, st: RunOnceState) -> tuple[bool, "ToolScope"]:
    """判定本轮是否动态装配并解析 persona 的 ToolScope（静态人格顺便展开 packs）。"""
    st.expose_dynamic = False
    probe_for_media = ""
    if isinstance(st.user_message, str):
        probe_for_media = st.user_message
    elif st.ev is not None and st.ev.raw_text:
        probe_for_media = st.ev.raw_text
    st.has_media = interaction_scaffold.message_has_media_handles(
        probe_for_media,
        image_id_list=st.ev.image_id_list if st.ev is not None else None,
        image_list=st.ev.image_list if st.ev is not None else None,
        audio_id=st.ev.audio_id if st.ev is not None else None,
    )
    st.group_slim = bool(st.tg is not None and bool(st.tg.is_group) and host.create_by in _INTERACTIVE_CREATE_BY)

    if host.dynamic_tools is not None:
        assemble = host.dynamic_tools
    else:
        assemble = host.create_by in _AGENTIC_CREATE_BY and not st.tools

    from gsuid_core.ai_core.agent_node.tool_scope import get_tool_scope as _get_tool_scope

    scope = _get_tool_scope(host.persona_name)
    if not scope.is_open and host.persona_name:
        logger.info(
            i18n_t(
                "log.agent.tool_scope_restricted_recall",
                p0=host.persona_name,
                p1="all" if scope.allow_all else ",".join(sorted(scope.allow)) or "none",
            )
        )
    if st.context is not None:
        st.context.tool_scope = scope
    st.tool_scope = scope

    if assemble and host.dynamic_tools is None and host.persona_name:
        from gsuid_core.ai_core.agent_node import (
            get_node as _get_agent_node,
            has_dynamic_pack,
            resolve_pack_tool_names,
        )

        pnode = _get_agent_node(host.persona_name)
        if pnode is not None and not has_dynamic_pack(pnode.tool_packs):
            assemble = False
            static_names = list(dict.fromkeys(resolve_pack_tool_names(pnode.tool_packs) + pnode.tool_names))
            seen_names = {t.name for t in st.tools}
            for tn in static_names:
                if tn in seen_names:
                    continue
                tb = find_tool_base(tn)
                if tb is not None:
                    seen_names.add(tn)
                    st.tools.append(tb.tool)
            logger.debug(
                i18n_t(
                    "log.agent.persona_declare_dynamic_capability",
                    p0=host.persona_name,
                    p1=len(st.tools),
                )
            )
    return assemble, scope


async def _recall_turn_seeds_stage(
    host: RunOnceHost,
    st: RunOnceState,
    *,
    query: str,
    scope: "ToolScope",
    core_names: set[str],
    is_group: bool,
    call_self: bool,
) -> tuple[ToolList, list[str]]:
    """第三层语义与实体路由召回：仅产成本轮种子候选，不直接改动 tools[]。"""
    ctx_tags: list[str] = []
    ctx_scope_key = ""
    if st.ev is not None and st.ev.group_id:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

        ctx_scope_key = make_scope_key(ScopeType.GROUP, str(st.ev.group_id))
    if ctx_scope_key and not st.in_flight_short:
        try:
            ctx_tags = await get_scope_context_tags(ctx_scope_key)
        except Exception as e:
            logger.debug(i18n_t("log.agent.load_contextual_pool", e=e))

    recall_limit = int(ai_config.get_config("tool_search_recall").data)
    router_chat_only = (
        host.route_decision is not None
        and host.route_decision.intent == "闲聊"
        and not host.route_decision.needs_committed_work
        and not st.followup_detected
        and not st.has_active_task
        and not st.has_media
    )
    skip_search = router_chat_only or should_skip_tool_search(
        in_flight_short=st.in_flight_short,
        is_group=is_group,
        followup_detected=st.followup_detected,
        has_active_task=st.has_active_task,
        has_media=st.has_media,
        call_to_self=call_self,
        intent=st.intent or "",
    )
    if st.intent == "闲聊" and not st.followup_detected and not st.has_active_task and not host._recent_tool_families:
        recall_limit = max(2, recall_limit // 2)

    turn_seeds: ToolList = []
    if not (query and not skip_search):
        return turn_seeds, ctx_tags

    search_query = interaction_scaffold.build_tool_search_query(
        query,
        host._recent_user_texts,
        ctx_tags,
        include_recent=bool(st.followup_detected),
    )
    logger.debug(i18n_t("log.agent.attempting_search_tools_query", search_query=search_query))

    ignore: tuple[str, ...] = ()
    if host.persona_name:
        from gsuid_core.ai_core.memory.group_profile import collect_persona_surfaces

        ignore = collect_persona_surfaces(host.persona_name)
    from gsuid_core.ai_core.entity_index import (
        ALIAS_ROUTE_TEXT_KEY,
        ALIAS_PLUGIN_EXTRA_KEY,
        ALIAS_PLUGIN_EXPLICIT_EXTRA_KEY,
        plugins_in_text,
    )

    if st.ev is not None and st.ev.raw_text:
        utter = st.ev.raw_text
    elif st.ev is not None and st.ev.text:
        utter = st.ev.text
    else:
        from gsuid_core.ai_core.control.outbound_detectors import spoken_user_body

        utter = spoken_user_body(query)
    bg = ""
    if st.ev is not None:
        from gsuid_core.message_history import get_history_manager
        from gsuid_core.ai_core.thread_view import render_window

        bg = render_window(
            get_history_manager().get_history(st.ev),
            session_id=st.ev.session_id,
            bot_id=str(st.ev.bot_id or ""),
        )
    recall = await recall_turn_seeds(
        search_query=search_query,
        route_text=query,
        utterance=utter,
        background=bg,
        limit=recall_limit,
        exclude_names=core_names,
        scope=scope,
        scope_key=ctx_scope_key,
        ignore_surfaces=ignore,
    )
    found = recall.found
    turn_seeds = recall.seeds
    scan = recall.scan_text
    if recall.lock.plugin:
        st.run_extra[ALIAS_PLUGIN_EXTRA_KEY] = recall.lock.plugin
        st.run_extra[ALIAS_PLUGIN_EXPLICIT_EXTRA_KEY] = recall.lock.explicit
        if scan:
            st.run_extra[ALIAS_ROUTE_TEXT_KEY] = scan
    if call_self and st.intent != "闲聊":
        routed = plugins_in_text(scan)
        # 向量检索命中不算。要实体路由真正装上该插件的工具。
        if len(utter) >= 12 and routed:
            wanted = {name for name in routed if name}

            def _mounted(tool_name: str) -> bool:
                base = find_tool_base(tool_name)
                return base is not None and bool(base.plugin) and base.plugin in wanted

            hit = any(_mounted(tool.name) for tool in found) or any(_mounted(name) for name in core_names)
            if hit:
                st.entity_routed = True
    return turn_seeds, ctx_tags


async def _collect_priority_seeds(
    host: RunOnceHost,
    st: RunOnceState,
    scope: "ToolScope",
    *,
    use_frozen_core: bool,
    interactive: bool,
    call_self: bool,
) -> ToolList:
    """收集强状态驱动种子（审批 / 在途看板 / 活跃提醒管理）。"""
    priority_seeds: ToolList = []
    origin_sid = host.session_id or (st.ev.session_id if st.ev is not None else "")
    if origin_sid:
        from gsuid_core.ai_core.approval.session_ctx import session_has_pending

        if session_has_pending(origin_sid):
            ap_tb = find_tool_base("check_approval")
            if ap_tb is not None:
                if not use_frozen_core and all(t.name != "check_approval" for t in st.tools):
                    st.tools.append(ap_tb.tool)
                elif all(s.name != "check_approval" for s in priority_seeds):
                    priority_seeds.append(ap_tb.tool)

    if st.has_active_task:
        for st_name in (
            "list_my_kanban_tasks",
            "check_delegation",
            "pause_my_kanban_tree",
            "resume_my_kanban_tree",
            "fail_task_tree",
            "respawn_subtask",
            "artifact_get_recent",
        ):
            if not scope.is_open and not scope.tool_enabled(st_name):
                continue
            st_tb = find_tool_base(st_name)
            if st_tb is not None and all(s.name != st_name for s in priority_seeds):
                priority_seeds.append(st_tb.tool)

    sched_manage_turn = bool(st.followup_detected or (st.tg is not None and st.tg.task_management))
    if (
        interactive
        and (not st.in_flight_short or sched_manage_turn)
        and st.ev is not None
        and str(st.ev.user_id).strip()
        and (sched_manage_turn or call_self or (st.tg is not None and st.tg.quoted_tome))
    ):
        from gsuid_core.ai_core.tool_state_signals import user_has_active_schedules

        if await user_has_active_schedules(str(st.ev.user_id)) or sched_manage_turn:
            for sc_name in (
                "list_scheduled_tasks",
                "modify_scheduled_task",
                "cancel_scheduled_task",
                "pause_scheduled_task",
            ):
                if not scope.is_open and not scope.tool_enabled(sc_name):
                    continue
                sc_tb = find_tool_base(sc_name)
                if sc_tb is not None and all(s.name != sc_name for s in priority_seeds):
                    priority_seeds.append(sc_tb.tool)
    return priority_seeds


async def _inject_progressive_exposure(
    st: RunOnceState,
    *,
    priority_seeds: ToolList,
    turn_seeds: ToolList,
    query: str,
    did_strip_exclusive: bool,
) -> None:
    """确保 find_tools/call_tool 在表、将本轮提示种子注入 user 侧，并保底 create_subagent。"""
    if any(t.name == "find_tools" for t in st.tools):
        st.expose_dynamic = True
    else:
        ft = find_tool_base("find_tools")
        if ft is not None:
            st.tools.append(ft.tool)
            st.expose_dynamic = True
    if st.expose_dynamic and not any(t.name == "call_tool" for t in st.tools):
        ct = find_tool_base("call_tool")
        if ct is not None:
            st.tools.append(ct.tool)
    if st.expose_dynamic:
        logger.debug(i18n_t("log.agent.find_tools_progressive_exposure"))
        prio_picked, rag_picked = await select_turn_hint_names(
            st.tools,
            priority_seeds,
            turn_seeds,
            query=query,
            bot_id=str(st.ev.bot_id) if st.ev is not None else "",
            user_id=str(st.ev.user_id) if st.ev is not None else "",
        )
        hint_picked = [*prio_picked, *rag_picked]
        if hint_picked:
            from gsuid_core.ai_core.buildin_tools.call_tool import format_compact_tool_schema
            from gsuid_core.ai_core.buildin_tools.find_tools_rank import tool_brief

            prio_set = set(prio_picked)
            direct_items: list[str] = []
            soft_items: list[str] = []
            hint_names: list[str] = []
            for sn in hint_picked:
                stb = find_tool_base(sn)
                if stb is None:
                    continue
                brief = " ".join(tool_brief(covers=list(stb.covers), description=stb.description).split())[:44]
                if sn in prio_set:
                    spec = format_compact_tool_schema(stb)
                    direct_items.append(f"`{sn}`：{brief} | 参数: {spec}")
                else:
                    soft_items.append(f"`{sn}`：{brief}")
                hint_names.append(sn)
            if direct_items:
                st.final_user_message = _append_user_text(
                    st.final_user_message,
                    turn_seed_hint_line("；".join(direct_items), direct_call=True),
                )
            if soft_items:
                st.final_user_message = _append_user_text(
                    st.final_user_message,
                    turn_seed_hint_line("；".join(soft_items), direct_call=False),
                )
            if hint_names:
                st.hinted_seed_names = hint_names
                logger.debug(i18n_t("log.agent.turn_seed_hinted", n=len(hint_names), names=hint_names))

    need_subagent = did_strip_exclusive
    deleg_pid = ""
    if query:
        deleg_pid = _matched_delegation_only_profile(query)
        if deleg_pid:
            need_subagent = True
        elif not need_subagent:
            pool_names = {t.name for t in st.tools} | set(st.hinted_seed_names)
            domain_pid = _pool_overlaps_capability_agent(pool_names)
            if domain_pid:
                need_subagent = True
                deleg_pid = domain_pid
    if need_subagent and not any(t.name == "create_subagent" for t in st.tools):
        cs = find_tool_base("create_subagent")
        if cs is not None:
            st.tools.append(cs.tool)
            logger.debug(
                i18n_t(
                    "log.agent.delegation_safeguard_create_subagent",
                    deleg_pid=deleg_pid or "exclusive_strip",
                )
            )


def _apply_tool_mutations_for(host: RunOnceHost, st: RunOnceState, ensured: List[str], dropped: List[str]) -> None:
    """按 hook 请求增删工具（永冻主会话不改 tools[]，改转当轮 user 侧种子提示）。"""
    del host
    from gsuid_core.ai_core.register import find_tool_base as _find_tb, is_core_only_category
    from gsuid_core.ai_core.buildin_tools.call_tool import format_compact_tool_schema
    from gsuid_core.ai_core.buildin_tools.find_tools_rank import tool_brief

    present = {t.name for t in st.tools}
    frozen_locked = st.frozen_core
    exclusive = _capability_exclusive_tool_names() if frozen_locked else set()
    hook_hints: list[str] = []
    for name in ensured:
        if name in present:
            continue
        tb = _find_tb(name)
        if tb is None:
            logger.warning(i18n_t("log.agent.hook_ensure_unknown_tool", name=name))
            continue
        if is_core_only_category(name):
            logger.warning(i18n_t("log.agent.hook_ensure_privileged_denied", name=name))
            continue
        if frozen_locked:
            if name in exclusive or name in st.hinted_seed_names:
                continue
            st.hinted_seed_names.append(name)
            brief = " ".join(tool_brief(covers=list(tb.covers), description=tb.description).split())[:44]
            spec = format_compact_tool_schema(tb)
            hook_hints.append(f"`{name}`：{brief} | 参数: {spec}")
            continue
        present.add(name)
        st.tools.append(tb.tool)
    if hook_hints:
        st.final_user_message = _append_user_text(
            st.final_user_message,
            turn_seed_hint_line("；".join(hook_hints), direct_call=True),
        )
    if frozen_locked:
        blk = _require_context(st).blocked_tool_names
        for name in dropped:
            if name == "create_subagent":
                logger.warning(i18n_t("log.agent.hook_drop_delegation_denied", name=name))
                continue
            blk.add(name)
        return
    for name in dropped:
        if name == "create_subagent":
            logger.warning(i18n_t("log.agent.hook_drop_delegation_denied", name=name))
            continue
        st.tools = [t for t in st.tools if t.name != name]


def _reseal_tools_for(host: RunOnceHost, st: RunOnceState) -> None:
    """内核收口：主人格交互轮再剥一遍 exclusive，然后去重。"""
    if host.create_by in _INTERACTIVE_CREATE_BY:
        exclusive = _capability_exclusive_tool_names()
        if exclusive:
            before = {t.name for t in st.tools}
            st.tools = [t for t in st.tools if t.name not in exclusive]
            stripped = before - {t.name for t in st.tools}
            if stripped:
                logger.info(
                    i18n_t(
                        "log.agent.main_persona_stripped_capability",
                        n=len(stripped),
                        names=sorted(stripped)[:12],
                    )
                )
    st.tools = list({obj.name: obj for obj in st.tools}.values())


async def _fire_tool_hooks_for(host: RunOnceHost, st: RunOnceState) -> None:
    """开火 H14（装配套件）与 H15（第三方钉/砍工具），每点之后收口一次。"""
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, fire_hooks, should_fire

    for point in (AgentHookPoint.ASSEMBLE_TOOLS, AgentHookPoint.AFTER_ASSEMBLE_TOOLS):
        if not should_fire(point):
            continue
        ctx = AgentHookContext(
            point=point,
            ev=st.ev,
            bot=st.bot,
            session_id=host.session_id,
            persona_name=host.persona_name,
            create_by=host.create_by,
            is_subagent=host.is_subagent,
            addr_gated=st.addr_gated,
        )
        await fire_hooks(point, ctx)
        if ctx.ensured_tools or ctx.dropped_tools:
            _apply_tool_mutations_for(host, st, ctx.ensured_tools, ctx.dropped_tools)
            _reseal_tools_for(host, st)


async def _finalize_and_log_assembled_tools(
    host: RunOnceHost,
    st: RunOnceState,
    caller_by_name: dict[str, Tool[ToolContext]],
) -> None:
    """收口 hooks、旁白增补工具、Autonomy 白名单、冻结快照写回与日志审计。"""
    st.frozen_core = _frozen_core_active_for(host, (t.name for t in st.tools))

    if not st.addr_gated:
        await _fire_tool_hooks_for(host, st)

    logger.debug(i18n_t("log.agent.tool_list", p0=[tool.name for tool in st.tools]))

    from gsuid_core.ai_core.command_aside.policy import aside_extra_tools

    aside_extras = aside_extra_tools()
    aside_extra_names = {et.name for et in aside_extras}
    if aside_extras:
        if st.frozen_core:
            from gsuid_core.ai_core.buildin_tools.call_tool import (
                EPHEMERAL_TOOLS_EXTRA_KEY,
                format_compact_tool_schema_from_tool,
            )

            eph_ctx = _require_context(st)
            eph_map: dict[str, Tool[ToolContext]] = {}
            aside_hints: list[str] = []
            for extra_tool in aside_extras:
                eph_map[extra_tool.name] = extra_tool
                if extra_tool.name not in st.hinted_seed_names:
                    st.hinted_seed_names.append(extra_tool.name)
                    desc = " ".join((extra_tool.description or "").split())[:44]
                    spec = format_compact_tool_schema_from_tool(extra_tool)
                    aside_hints.append(f"`{extra_tool.name}`：{desc} | 参数: {spec}")
            eph_ctx.extra[EPHEMERAL_TOOLS_EXTRA_KEY] = eph_map
            if aside_hints:
                st.final_user_message = _append_user_text(
                    st.final_user_message,
                    turn_seed_hint_line("；".join(aside_hints), direct_call=True),
                )
        else:
            for extra_tool in aside_extras:
                st.tools.append(extra_tool)
    if caller_by_name:
        st.tools = [caller_by_name[t.name] if t.name in caller_by_name else t for t in st.tools]
    if host.create_by == "Autonomy":
        from gsuid_core.ai_core.autonomy.tools import AUTONOMY_CALL_TOOL_GUARD_KEY, autonomy_tool_allowed

        st.tools = [tool for tool in st.tools if autonomy_tool_allowed(tool.name)]
        _require_context(st).extra[AUTONOMY_CALL_TOOL_GUARD_KEY] = True
    st.tools = list({obj.name: obj for obj in st.tools}.values())
    st.tool_names = [t.name for t in st.tools]
    st.exposed_tool_names = list(st.tool_names)
    ctx = _require_context(st)
    from gsuid_core.ai_core.control.outbound_detectors import EXPOSED_TOOLS_EXTRA_KEY

    ctx.extra[EXPOSED_TOOLS_EXTRA_KEY] = list(st.tool_names)

    # 非永冻模式按会话只增不减沉淀；永冻主会话严禁回写 _session_toolset_frozen 防缓存击穿。
    if host.create_by in _MAIN_PERSONA_CREATE_BY and not st.frozen_core:
        exclusive = _capability_exclusive_tool_names()
        for tname in st.tool_names:
            if tname not in exclusive and tname not in aside_extra_names:
                if host._session_toolset_frozen is not None and tname not in host._session_toolset_frozen:
                    host._session_toolset_frozen.append(tname)

    assembled_domains: set[str] = set()
    for tn in st.tool_names:
        tb = find_tool_base(tn)
        if tb is not None and tb.capability_domain:
            assembled_domains.add(tb.capability_domain)
    host._last_assembled_domains = assembled_domains

    host._session_logger.log_tools_list(st.tool_names)
    from gsuid_core.ai_core.prefix_probe import tools_diff

    prev_tool_names = host._last_logged_tool_names
    tc_added: list[str] = []
    tc_removed: list[str] = []
    if prev_tool_names is not None:
        tc_diff = tools_diff(prev_tool_names, st.tool_names)
        tc_added = tc_diff["added"]
        tc_removed = tc_diff["removed"]
    if tc_added or tc_removed or st.hinted_seed_names:
        host._session_logger.log_tools_change(
            source="assembly",
            added=tc_added,
            removed=tc_removed,
            hinted=list(st.hinted_seed_names),
            total=len(st.tool_names),
            from_find_tools=[n for n in tc_added if n in host._last_run_find_tools_names],
        )
    host._last_logged_tool_names = list(st.tool_names)


class ToolsPhase:
    def _stabilize_session_toolset(self: RunOnceHost, core: ToolList, ctx_tags: list[str]) -> ToolList:
        return _stabilize_session_toolset_for(self, core, ctx_tags)

    async def _restore_frozen_session_tools(self: RunOnceHost, caller_tools: ToolList | None = None) -> ToolList:
        return await _restore_frozen_session_tools_for(self, caller_tools)

    async def _run_once_assemble_tools(self: RunOnceHost, st: RunOnceState) -> None:
        """工具五层装配 + 去重 + 渐进式暴露。"""
        from gsuid_core.ai_core.command_aside.policy import aside_blocks_tools

        if aside_blocks_tools():
            await _handle_aside_blocked_tools(self, st)
            return

        assemble, scope = _resolve_assembly_scope(self, st)

        if st.addr_gated:
            # C-3 寻址门：保留已冻结会话工具 schema 保住前缀缓存，执行期由 tool_execute 拦截
            st.tools = await _restore_frozen_session_tools_for(self, st.tools)
            st.expose_dynamic = bool(st.tools)
            _require_context(st).extra[TOOLS_EXECUTION_GATED_KEY] = True
            caller_by_name: dict[str, Tool[ToolContext]] = {}
        elif assemble or self.create_by in _AGENTIC_CREATE_BY:
            caller_tools: ToolList = list(st.tools) if st.tools else []
            caller_by_name = {t.name: t for t in caller_tools}
            if assemble:
                qy = ""
                if not st.fw_msg:
                    if isinstance(st.user_message, str):
                        qy = st.user_message
                    elif st.ev is not None:
                        qy = st.ev.raw_text

                use_frozen_core = _frozen_core_eligible_for(self)
                core_tools, core_names = await _build_core_tools_for(self, caller_tools)

                is_group = bool(st.tg is not None and st.tg.is_group)
                call_self = bool(st.tg is not None and st.tg.call_to_self)
                interactive = self.create_by in _INTERACTIVE_CREATE_BY
                if self._recent_tool_families:
                    self._recent_tool_families = {d: t - 1 for d, t in self._recent_tool_families.items() if t - 1 > 0}

                turn_seeds, ctx_tags = await _recall_turn_seeds_stage(
                    self,
                    st,
                    query=qy,
                    scope=scope,
                    core_names=core_names,
                    is_group=is_group,
                    call_self=call_self,
                )

                st.tools = _stabilize_session_toolset_for(self, core_tools, ctx_tags)
                if interactive:
                    st.tools = _without_progress_tool(st.tools)

                did_strip_exclusive = False
                if self.create_by in _INTERACTIVE_CREATE_BY:
                    exclusive = _capability_exclusive_tool_names()
                    if exclusive:
                        before = {t.name for t in st.tools}
                        stripped = before & exclusive
                        if stripped:
                            st.tools = [t for t in st.tools if t.name not in exclusive]
                            if self._session_toolset_frozen is not None:
                                self._session_toolset_frozen = [
                                    n for n in self._session_toolset_frozen if n not in exclusive
                                ]
                            did_strip_exclusive = True
                            logger.info(
                                i18n_t(
                                    "log.agent.main_persona_stripped_capability",
                                    n=len(stripped),
                                    names=sorted(stripped)[:12],
                                )
                            )

                priority_seeds = await _collect_priority_seeds(
                    self,
                    st,
                    scope,
                    use_frozen_core=use_frozen_core,
                    interactive=interactive,
                    call_self=call_self,
                )
                await _inject_progressive_exposure(
                    st,
                    priority_seeds=priority_seeds,
                    turn_seeds=turn_seeds,
                    query=qy,
                    did_strip_exclusive=did_strip_exclusive,
                )

                logger.debug(
                    i18n_t(
                        "log.agent.tool_count_baseline_extra",
                        p0=len(st.tools),
                        p1=len(core_tools),
                        p2=len(st.hinted_seed_names),
                    )
                )

                if qy:
                    text_window: int = ai_config.get_config("tool_context_window").data
                    keep = max(text_window - 1, 0)
                    self._recent_user_texts.append(qy)
                    self._recent_user_texts = self._recent_user_texts[-keep:] if keep else []
            else:
                logger.debug(i18n_t("log.agent.passed_tools_list_arguments", p0=len(st.tools)))
        else:
            caller_by_name = {t.name: t for t in st.tools} if st.tools else {}
            logger.debug(i18n_t("log.agent.skip_tool_search_searching_tools"))

        await _finalize_and_log_assembled_tools(self, st, caller_by_name)

    def _run_once_build_agent_meta(self: RunOnceHost, st: RunOnceState) -> object:
        """构建 pydantic-ai Agent 与流式统计元数据；返回 Agent 实例。"""
        _skills_ts = skills_toolset if st.frozen_core else skills_toolset.filtered(skill_tool_visible)
        _keep_skills = not st.addr_gated or bool(self._session_toolset_frozen)
        _toolsets = [_skills_ts] if self.create_by in _SKILLS_CREATE_BY and _keep_skills else []
        if self.model:
            _base_settings: ModelSettings = self.model.settings.copy() if self.model.settings else ModelSettings()
            if self._wire_max_tokens is not None:
                _base_settings["max_tokens"] = self._wire_max_tokens
            thinking_override = self.thinking
            if thinking_override is not None:
                _base_settings["thinking"] = thinking_override
            elif self.create_by in (
                "MemEntityExtraction",
                "MemPreferenceDistill",
                "MemSummary",
                "MemProfileExtraction",
                "MemClusterNaming",
                "ImageDescSummary",
            ):
                _base_settings["thinking"] = False
            _model_settings: ModelSettings | None = merge_model_settings(
                _base_settings,
                resolve_attribution_settings(
                    config_full_name=self._active_config_name or "",
                    task_level=self.task_level,
                    scope=st.budget_scope,
                    session_id=self.session_id,
                    create_by=self.create_by,
                ),
            )
        else:
            _model_settings = None

        _caps: list[AbstractCapability[Any]] = [build_tool_safety_capability()]
        _remote_web = attach_remote_web_search(st, self._active_config_name)
        if _remote_web is not None:
            _caps.append(_remote_web)
        _agent = Agent(
            model=self.model,
            deps_type=ToolContext,
            system_prompt=self.system_prompt or "你是一个智能助手, 简短的一句话回答问题即可。",
            model_settings=_model_settings,
            tools=st.tools,
            toolsets=_toolsets,
            capabilities=_caps,
            retries=3,
            output_type=st.output_type or str,
        )

        self.extract_history()
        self._inject_deepseek_rp_marker(st)

        st.req_start = 0.0
        st.first_event_at = None
        st.last_event_at = None
        st.model_name = self.model.model_name if self.model else "unknown"
        st.provider = self.model.system if self.model else "unknown"
        st.thinking_tags = ("<think>", "</think>")
        if self.model is not None:
            _profile_obj = self.model.profile
            if isinstance(_profile_obj, dict):
                if "thinking_tags" in _profile_obj:
                    _raw_tags = _profile_obj["thinking_tags"]
                    if (
                        isinstance(_raw_tags, (tuple, list))
                        and len(_raw_tags) == 2
                        and all(isinstance(x, str) for x in _raw_tags)
                    ):
                        st.thinking_tags = _normalize_thinking_tags((_raw_tags[0], _raw_tags[1]))
            else:
                logger.error(
                    i18n_t(
                        "log.agent.abnormal_profile_type_forensics",
                        p0=type(_profile_obj).__name__,
                        p1=st.model_name,
                        p2=repr(_profile_obj)[:300],
                    )
                )

        return _agent
