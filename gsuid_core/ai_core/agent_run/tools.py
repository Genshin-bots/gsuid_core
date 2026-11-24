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


class ToolsPhase(RunOnceHost):
    def _frozen_core_eligible(self) -> bool:
        """永冻核心的唯一判定：主会话首轮拍快照后 tools[] 不再变化，长尾工具一律经 call_tool。

        dynamic_tools=False 与未声明 dynamic 能力包的静态人格不走永冻，沿用调用方 / 静态挂载。
        """
        if self.create_by not in _MAIN_PERSONA_CREATE_BY or self.dynamic_tools is False:
            return False
        if self.dynamic_tools is None and self.persona_name:
            from gsuid_core.ai_core.agent_node import get_node, has_dynamic_pack

            node = get_node(self.persona_name)
            if node is not None and not has_dynamic_pack(node.tool_packs):
                return False
        return True

    def _frozen_core_active(self, tool_names: Iterable[str]) -> bool:
        """本轮是否真的挂上永冻核心（eligible 且 call_tool 在表）；装配后的分支只认 st.frozen_core。"""
        return "call_tool" in set(tool_names) and self._frozen_core_eligible()

    def _stabilize_session_toolset(self, core: ToolList, ctx_tags: list[str]) -> ToolList:
        """会话工具表：永冻主会话首轮拍快照后原样复用；其它会话按核心名单 append-only。"""
        self._session_toolset_tags = frozenset(ctx_tags)
        if self._frozen_core_eligible() and self._session_toolset_frozen is not None:
            names = list(self._session_toolset_frozen)
        else:
            names = stabilize_session_tool_names(
                self._session_toolset_frozen,
                list(dict.fromkeys(t.name for t in core)),
                exclusive=_capability_exclusive_tool_names(),
                ceiling=_SESSION_TOOL_CEILING,
            )
            self._session_toolset_frozen = names
        return tools_by_names(names, core)

    async def _build_core_tools(self, caller_tools: ToolList) -> tuple[ToolList, set[str]]:
        return await build_core_tools(
            caller_tools,
            persona_name=self.persona_name,
            frozen_core=self._frozen_core_eligible(),
            interactive=self.create_by in _INTERACTIVE_CREATE_BY,
        )

    async def _restore_frozen_session_tools(self, caller_tools: ToolList | None = None) -> ToolList:
        """还原或首轮预冻主会话核心工具表，避免首轮旁白/寻址门导致线缆层 tools[] 漂移。"""
        if self.dynamic_tools is False:
            return list(caller_tools) if caller_tools else []
        if not self._session_toolset_frozen:
            if self.create_by not in _MAIN_PERSONA_CREATE_BY:
                return []
            # 静态人格（未声明 dynamic 能力包）不越俎代庖预冻 13 项动态核心表
            if not self._frozen_core_eligible():
                return list(caller_tools) if caller_tools else []
            core_tools, _ = await self._build_core_tools(list(caller_tools) if caller_tools else [])
            out = self._stabilize_session_toolset(core_tools, [])
            if self.create_by in _INTERACTIVE_CREATE_BY:
                out = _without_progress_tool(out)
                exclusive = _capability_exclusive_tool_names()
                if exclusive:
                    out = [t for t in out if t.name not in exclusive]
                    if self._session_toolset_frozen is not None:
                        self._session_toolset_frozen = [n for n in self._session_toolset_frozen if n not in exclusive]
            return out
        out = []
        seen: set[str] = set()
        for name in self._session_toolset_frozen:
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

    async def _run_once_assemble_tools(self, st: RunOnceState) -> None:
        """工具五层装配 + 去重 + 渐进式暴露。"""
        from gsuid_core.ai_core.command_aside.policy import aside_blocks_tools

        if aside_blocks_tools():
            st.tools = await self._restore_frozen_session_tools(st.tools)
            st.tool_names = [t.name for t in st.tools]
            st.exposed_tool_names = []
            st.expose_dynamic = bool(st.tools)
            st.frozen_core = self._frozen_core_active(st.tool_names)
            self._last_assembled_domains = set()
            blocked_ctx = _require_context(st)
            from gsuid_core.ai_core.control.outbound_detectors import EXPOSED_TOOLS_EXTRA_KEY

            blocked_ctx.extra[EXPOSED_TOOLS_EXTRA_KEY] = []
            blocked_ctx.extra[TOOLS_EXECUTION_GATED_KEY] = True
            self._session_logger.log_tools_list(st.tool_names)
            return
        # 渐进式工具暴露是否在本轮生效（仅自动装配 + 非闲聊轮）。
        st.expose_dynamic = False
        # 媒体句柄（event 字段或正文 img_/图片ID 标注）——通道信号，非话题词
        _probe_for_media = ""
        if isinstance(st.user_message, str):
            _probe_for_media = st.user_message
        elif st.ev is not None and st.ev.raw_text:
            _probe_for_media = st.ev.raw_text
        st.has_media = interaction_scaffold.message_has_media_handles(
            _probe_for_media,
            image_id_list=st.ev.image_id_list if st.ev is not None else None,
            image_list=st.ev.image_list if st.ev is not None else None,
            audio_id=st.ev.audio_id if st.ev is not None else None,
        )
        st.group_slim = bool(st.tg is not None and bool(st.tg.is_group) and self.create_by in _INTERACTIVE_CREATE_BY)

        # dynamic 能力族门：显式 True/False 优先；None 沿用旧门（agentic 且未传 st.tools）。
        if self.dynamic_tools is not None:
            _assemble = self.dynamic_tools
        else:
            _assemble = self.create_by in _AGENTIC_CREATE_BY and not st.tools

        # persona 的 enabled_tools 作用域：本轮向量检索 / find_tools / 动态暴露的收放口径。
        # 只管检索池，不动常驻直装工具与 tool_packs 静态挂载。
        from gsuid_core.ai_core.agent_node.tool_scope import get_tool_scope as _get_tool_scope

        _scope = _get_tool_scope(self.persona_name)
        if not _scope.is_open and self.persona_name:
            logger.info(
                i18n_t(
                    "log.agent.tool_scope_restricted_recall",
                    p0=self.persona_name,
                    p1="all" if _scope.allow_all else ",".join(sorted(_scope.allow)) or "none",
                )
            )
        if st.context is not None:
            # find_tools 在 step 内现查，须能读到本轮作用域
            st.context.tool_scope = _scope
        # 跨方法复用同一份工具作用域快照
        st.tool_scope = _scope

        # persona 会话与其 AgentNode 声明同步：packs 去掉 dynamic 即关闭五层自动装配
        # 改为静态解析 packs + st.tool_names（与 task-mode 的 runner 同语义）。
        if _assemble and self.dynamic_tools is None and self.persona_name:
            from gsuid_core.ai_core.agent_node import (
                get_node as _get_agent_node,
                has_dynamic_pack,
                resolve_pack_tool_names,
            )

            _pnode = _get_agent_node(self.persona_name)
            if _pnode is not None and not has_dynamic_pack(_pnode.tool_packs):
                _assemble = False
                _static_names = list(dict.fromkeys(resolve_pack_tool_names(_pnode.tool_packs) + _pnode.tool_names))
                _seen_names = {t.name for t in st.tools}
                for _tn in _static_names:
                    if _tn in _seen_names:
                        continue
                    _tb = find_tool_base(_tn)
                    if _tb is not None:
                        _seen_names.add(_tn)
                        st.tools.append(_tb.tool)
                logger.debug(
                    i18n_t(
                        "log.agent.persona_declare_dynamic_capability",
                        p0=self.persona_name,
                        p1=len(st.tools),
                    )
                )

        if st.addr_gated:
            # C-3 寻址门：保留已冻结会话工具 schema 保住前缀缓存，执行期由 tool_execute 拦截
            st.tools = await self._restore_frozen_session_tools(st.tools)
            st.expose_dynamic = bool(st.tools)
            _require_context(st).extra[TOOLS_EXECUTION_GATED_KEY] = True
            _caller_by_name: dict[str, Tool[ToolContext]] = {}
        elif _assemble or self.create_by in _AGENTIC_CREATE_BY:
            _caller_tools: ToolList = list(st.tools) if st.tools else []
            _caller_by_name = {t.name: t for t in _caller_tools}
            if _assemble:
                qy = ""
                # 框架回灌：交付包不作向量检索 query（避免噪声 + 误装工具）
                if not st.fw_msg:
                    if isinstance(st.user_message, str):
                        qy = st.user_message
                    elif st.ev is not None:
                        qy = st.ev.raw_text

                _use_frozen_core = self._frozen_core_eligible()
                core_tools, core_names = await self._build_core_tools(_caller_tools)

                _is_group = bool(st.tg is not None and st.tg.is_group)
                _call_self = bool(st.tg is not None and st.tg.call_to_self)

                # L3 家族黏性 TTL 递减（不改 frozen core）。
                _interactive = self.create_by in _INTERACTIVE_CREATE_BY
                if self._recent_tool_families:
                    self._recent_tool_families = {
                        _d: _t - 1 for _d, _t in self._recent_tool_families.items() if _t - 1 > 0
                    }

                # 本轮检索种子仅在 user 侧给提示，不写进 frozen。
                turn_seeds: ToolList = []

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

                # 第三层：向量检索。闲聊且无明确待办时不主动预推噪声，真需要由模型自调 find_tools。
                _recall_limit = int(ai_config.get_config("tool_search_recall").data)
                _router_chat_only = (
                    self.route_decision is not None
                    and self.route_decision.intent == "闲聊"
                    and not self.route_decision.needs_committed_work
                    and not st.followup_detected
                    and not st.has_active_task
                    and not st.has_media
                )
                _skip_search = _router_chat_only or should_skip_tool_search(
                    in_flight_short=st.in_flight_short,
                    is_group=_is_group,
                    followup_detected=st.followup_detected,
                    has_active_task=st.has_active_task,
                    has_media=st.has_media,
                    call_to_self=_call_self,
                    intent=st.intent or "",
                )
                if (
                    st.intent == "闲聊"
                    and not st.followup_detected
                    and not st.has_active_task
                    and not self._recent_tool_families
                ):
                    _recall_limit = max(2, _recall_limit // 2)
                if qy and not _skip_search:
                    search_query = interaction_scaffold.build_tool_search_query(
                        qy,
                        self._recent_user_texts,
                        ctx_tags,
                        include_recent=bool(st.followup_detected),
                    )
                    logger.debug(i18n_t("log.agent.attempting_search_tools_query", search_query=search_query))

                    _ignore: tuple[str, ...] = ()
                    if self.persona_name:
                        from gsuid_core.ai_core.memory.group_profile import collect_persona_surfaces

                        _ignore = collect_persona_surfaces(self.persona_name)
                    from gsuid_core.ai_core.entity_index import (
                        ALIAS_ROUTE_TEXT_KEY,
                        ALIAS_PLUGIN_EXTRA_KEY,
                        ALIAS_PLUGIN_EXPLICIT_EXTRA_KEY,
                        plugins_in_text,
                    )

                    _utter = ""
                    if st.ev is not None and st.ev.raw_text:
                        _utter = st.ev.raw_text
                    elif st.ev is not None and st.ev.text:
                        _utter = st.ev.text
                    else:
                        from gsuid_core.ai_core.control.outbound_detectors import spoken_user_body

                        _utter = spoken_user_body(qy)
                    _bg = ""
                    if st.ev is not None:
                        from gsuid_core.message_history import get_history_manager
                        from gsuid_core.ai_core.thread_view import render_window

                        _bg = render_window(
                            get_history_manager().get_history(st.ev),
                            session_id=st.ev.session_id,
                            bot_id=str(st.ev.bot_id or ""),
                        )
                    _recall = await recall_turn_seeds(
                        search_query=search_query,
                        route_text=qy,
                        utterance=_utter,
                        background=_bg,
                        limit=_recall_limit,
                        exclude_names=core_names,
                        scope=_scope,
                        scope_key=ctx_scope_key,
                        ignore_surfaces=_ignore,
                    )
                    _found = _recall.found
                    turn_seeds = _recall.seeds
                    _scan = _recall.scan_text
                    if _recall.lock.plugin:
                        st.run_extra[ALIAS_PLUGIN_EXTRA_KEY] = _recall.lock.plugin
                        st.run_extra[ALIAS_PLUGIN_EXPLICIT_EXTRA_KEY] = _recall.lock.explicit
                        if _scan:
                            st.run_extra[ALIAS_ROUTE_TEXT_KEY] = _scan
                    if _call_self and st.intent != "闲聊":
                        _routed = plugins_in_text(_scan)
                        # 向量检索命中不算。要实体路由真正装上该插件的工具。
                        if len(_utter) >= 12 and _routed:
                            _wanted = {name for name in _routed if name}

                            def _mounted(tool_name: str) -> bool:
                                base = find_tool_base(tool_name)
                                return base is not None and bool(base.plugin) and base.plugin in _wanted

                            _hit = any(_mounted(tool.name) for tool in _found)
                            if not _hit:
                                _hit = any(_mounted(name) for name in core_names)
                            if _hit:
                                st.entity_routed = True

                st.tools = self._stabilize_session_toolset(core_tools, ctx_tags)
                if _interactive:
                    st.tools = _without_progress_tool(st.tools)

                _did_strip_exclusive = False
                if self.create_by in _INTERACTIVE_CREATE_BY:
                    _exclusive = _capability_exclusive_tool_names()
                    if _exclusive:
                        _before = {t.name for t in st.tools}
                        _stripped = _before & _exclusive
                        if _stripped:
                            st.tools = [t for t in st.tools if t.name not in _exclusive]
                            if self._session_toolset_frozen is not None:
                                self._session_toolset_frozen = [
                                    n for n in self._session_toolset_frozen if n not in _exclusive
                                ]
                            _did_strip_exclusive = True
                            logger.info(
                                i18n_t(
                                    "log.agent.main_persona_stripped_capability",
                                    n=len(_stripped),
                                    names=sorted(_stripped)[:12],
                                )
                            )

                # 强状态驱动种子（审批 / 在途任务 / 已有提醒管理）独立槽位，不与 RAG 召回争 cap=4 名额
                priority_seeds: ToolList = []
                _origin_sid = self.session_id or (st.ev.session_id if st.ev is not None else "")
                if _origin_sid:
                    from gsuid_core.ai_core.approval.session_ctx import session_has_pending

                    if session_has_pending(_origin_sid):
                        _ap_tb = find_tool_base("check_approval")
                        if _ap_tb is not None:
                            if not _use_frozen_core and all(t.name != "check_approval" for t in st.tools):
                                st.tools.append(_ap_tb.tool)
                            elif all(s.name != "check_approval" for s in priority_seeds):
                                priority_seeds.append(_ap_tb.tool)

                if st.has_active_task:
                    for _st_name in (
                        "list_my_kanban_tasks",
                        "check_delegation",
                        "pause_my_kanban_tree",
                        "resume_my_kanban_tree",
                        "fail_task_tree",
                        "respawn_subtask",
                        "artifact_get_recent",
                    ):
                        if not _scope.is_open and not _scope.tool_enabled(_st_name):
                            continue
                        _st_tb = find_tool_base(_st_name)
                        if _st_tb is not None and all(s.name != _st_name for s in priority_seeds):
                            priority_seeds.append(_st_tb.tool)
                _sched_manage_turn = bool(st.followup_detected or (st.tg is not None and st.tg.task_management))
                if (
                    _interactive
                    and (not st.in_flight_short or _sched_manage_turn)
                    and st.ev is not None
                    and str(st.ev.user_id).strip()
                    and (_sched_manage_turn or _call_self or (st.tg is not None and st.tg.quoted_tome))
                ):
                    from gsuid_core.ai_core.tool_state_signals import user_has_active_schedules

                    if await user_has_active_schedules(str(st.ev.user_id)) or _sched_manage_turn:
                        for _sc_name in (
                            "list_scheduled_tasks",
                            "modify_scheduled_task",
                            "cancel_scheduled_task",
                            "pause_scheduled_task",
                        ):
                            if not _scope.is_open and not _scope.tool_enabled(_sc_name):
                                continue
                            _sc_tb = find_tool_base(_sc_name)
                            if _sc_tb is not None and all(s.name != _sc_name for s in priority_seeds):
                                priority_seeds.append(_sc_tb.tool)

                # 渐进式工具暴露：find_tools + call_tool 恒在表，长尾工具不改 tools[]。
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

                if st.expose_dynamic:
                    # 种子不进 schema：工具数组每轮变动会让前缀缓存从工具段起失效。
                    # 状态种子（check_approval 等）与 RAG 召回分槽挑选，确保强状态提示永不被截断。
                    _prio_picked, _rag_picked = await select_turn_hint_names(
                        st.tools,
                        priority_seeds,
                        turn_seeds,
                        query=qy,
                        bot_id=str(st.ev.bot_id) if st.ev is not None else "",
                        user_id=str(st.ev.user_id) if st.ev is not None else "",
                    )
                    _hint_picked = [*_prio_picked, *_rag_picked]
                    if _hint_picked:
                        from gsuid_core.ai_core.buildin_tools.call_tool import format_compact_tool_schema
                        from gsuid_core.ai_core.buildin_tools.find_tools_rank import tool_brief

                        _prio_set = set(_prio_picked)
                        _direct_items: list[str] = []
                        _soft_items: list[str] = []
                        _hint_names: list[str] = []
                        for _sn in _hint_picked:
                            _stb = find_tool_base(_sn)
                            if _stb is None:
                                continue
                            _brief = " ".join(
                                tool_brief(covers=list(_stb.covers), description=_stb.description).split()
                            )[:44]
                            if _sn in _prio_set:
                                _spec = format_compact_tool_schema(_stb)
                                _direct_items.append(f"`{_sn}`：{_brief} | 参数: {_spec}")
                            else:
                                _soft_items.append(f"`{_sn}`：{_brief}")
                            _hint_names.append(_sn)
                        if _direct_items:
                            st.final_user_message = _append_user_text(
                                st.final_user_message,
                                turn_seed_hint_line("；".join(_direct_items), direct_call=True),
                            )
                        if _soft_items:
                            st.final_user_message = _append_user_text(
                                st.final_user_message,
                                turn_seed_hint_line("；".join(_soft_items), direct_call=False),
                            )
                        if _hint_names:
                            st.hinted_seed_names = _hint_names
                            logger.debug(
                                i18n_t(
                                    "log.agent.turn_seed_hinted",
                                    n=len(_hint_names),
                                    names=_hint_names,
                                )
                            )

                _need_subagent = _did_strip_exclusive
                deleg_pid = ""
                if qy:
                    deleg_pid = _matched_delegation_only_profile(qy)
                    if deleg_pid:
                        _need_subagent = True
                    elif not _need_subagent:
                        _pool_names = {t.name for t in st.tools} | set(st.hinted_seed_names)
                        _domain_pid = _pool_overlaps_capability_agent(_pool_names)
                        if _domain_pid:
                            _need_subagent = True
                            deleg_pid = _domain_pid
                if _need_subagent and not any(t.name == "create_subagent" for t in st.tools):
                    cs = find_tool_base("create_subagent")
                    if cs is not None:
                        st.tools.append(cs.tool)
                        logger.debug(
                            i18n_t(
                                "log.agent.delegation_safeguard_create_subagent",
                                deleg_pid=deleg_pid or "exclusive_strip",
                            )
                        )

                logger.debug(
                    i18n_t(
                        "log.agent.tool_count_baseline_extra",
                        p0=len(st.tools),
                        p1=len(core_tools),
                        p2=len(st.hinted_seed_names),
                    )
                )

                # L5：记录本轮用户原话，供下一轮上下文增强检索（保留窗口内的"上文"）
                if qy:
                    _text_window: int = ai_config.get_config("tool_context_window").data
                    keep = max(_text_window - 1, 0)
                    self._recent_user_texts.append(qy)
                    self._recent_user_texts = self._recent_user_texts[-keep:] if keep else []
            else:
                logger.debug(i18n_t("log.agent.passed_tools_list_arguments", p0=len(st.tools)))
        else:
            _caller_by_name = {t.name: t for t in st.tools} if st.tools else {}
            logger.debug(i18n_t("log.agent.skip_tool_search_searching_tools"))

        st.frozen_core = self._frozen_core_active(t.name for t in st.tools)

        # 工具装配套件与第三方钉工具点后各剥离一次 exclusive，防误装 render_* 工具。
        # addr_gated 为真时不执行钩子。
        if not st.addr_gated:
            await self._fire_tool_hooks(st)

        logger.debug(i18n_t("log.agent.tool_list", p0=[tool.name for tool in st.tools]))

        # 最终去重（兼容外部直接传入 st.tools 的情况），且同名工具始终保留调用方默认实现
        from gsuid_core.ai_core.command_aside.policy import aside_extra_tools

        _aside_extras = aside_extra_tools()
        _aside_extra_names = {et.name for et in _aside_extras}
        if _aside_extras:
            if st.frozen_core:
                # 永冻主会话不改 wire tools[]：挂入当轮临时工具表并内联契约，支持直接 call_tool
                from gsuid_core.ai_core.buildin_tools.call_tool import (
                    EPHEMERAL_TOOLS_EXTRA_KEY,
                    format_compact_tool_schema_from_tool,
                )

                _eph_ctx = _require_context(st)
                _eph_map: dict[str, Tool[ToolContext]] = {}
                _aside_hints: list[str] = []
                for extra_tool in _aside_extras:
                    _eph_map[extra_tool.name] = extra_tool
                    if extra_tool.name not in st.hinted_seed_names:
                        st.hinted_seed_names.append(extra_tool.name)
                        _desc = " ".join((extra_tool.description or "").split())[:44]
                        _spec = format_compact_tool_schema_from_tool(extra_tool)
                        _aside_hints.append(f"`{extra_tool.name}`：{_desc} | 参数: {_spec}")
                _eph_ctx.extra[EPHEMERAL_TOOLS_EXTRA_KEY] = _eph_map
                if _aside_hints:
                    st.final_user_message = _append_user_text(
                        st.final_user_message,
                        turn_seed_hint_line("；".join(_aside_hints), direct_call=True),
                    )
            else:
                for extra_tool in _aside_extras:
                    st.tools.append(extra_tool)
        if _caller_by_name:
            st.tools = [_caller_by_name[t.name] if t.name in _caller_by_name else t for t in st.tools]
        if self.create_by == "Autonomy":
            from gsuid_core.ai_core.autonomy.tools import AUTONOMY_CALL_TOOL_GUARD_KEY, autonomy_tool_allowed

            # 钩子与 call_tool 均须过自主行动允许表，禁放发送或高危执行。
            st.tools = [tool for tool in st.tools if autonomy_tool_allowed(tool.name)]
            _require_context(st).extra[AUTONOMY_CALL_TOOL_GUARD_KEY] = True
        st.tools = list({obj.name: obj for obj in st.tools}.values())
        st.tool_names = [t.name for t in st.tools]
        st.exposed_tool_names = list(st.tool_names)
        _ctx = _require_context(st)
        from gsuid_core.ai_core.control.outbound_detectors import EXPOSED_TOOLS_EXTRA_KEY

        _ctx.extra[EXPOSED_TOOLS_EXTRA_KEY] = list(st.tool_names)

        # 非永冻模式按会话只增不减沉淀；永冻主会话严禁回写 _session_toolset_frozen 防缓存击穿。
        if self.create_by in _MAIN_PERSONA_CREATE_BY and not st.frozen_core:
            _exclusive = _capability_exclusive_tool_names()
            for _tname in st.tool_names:
                if _tname not in _exclusive and _tname not in _aside_extra_names:
                    if self._session_toolset_frozen is not None and _tname not in self._session_toolset_frozen:
                        self._session_toolset_frozen.append(_tname)

        # 回填本轮装配工具的能力域，供 handle_ai 偏好注入精确过滤（"装配后回传"）： 把工具名映射回 capability_domain
        # handle_ai 据此只注入本轮可用工具相关的软偏好。
        assembled_domains: set[str] = set()
        for _tn in st.tool_names:
            _tb = find_tool_base(_tn)
            if _tb is not None and _tb.capability_domain:
                assembled_domains.add(_tb.capability_domain)
        self._last_assembled_domains = assembled_domains

        # 能力代理花名册已固化进 session system_prompt（可缓存），不再每轮塞 user 侧。

        # 记录本次传给 AI 的工具列表
        self._session_logger.log_tools_list(st.tool_names)
        # tools_change：与上一轮装配名单对比，只记有增删或有提示种子的轮次。
        # 首轮无基线只记提示；aside 早退路径不动基线。
        from gsuid_core.ai_core.prefix_probe import tools_diff

        _prev_tool_names = self._last_logged_tool_names
        _tc_added: list[str] = []
        _tc_removed: list[str] = []
        if _prev_tool_names is not None:
            _tc_diff = tools_diff(_prev_tool_names, st.tool_names)
            _tc_added = _tc_diff["added"]
            _tc_removed = _tc_diff["removed"]
        if _tc_added or _tc_removed or st.hinted_seed_names:
            self._session_logger.log_tools_change(
                source="assembly",
                added=_tc_added,
                removed=_tc_removed,
                hinted=list(st.hinted_seed_names),
                total=len(st.tool_names),
                from_find_tools=[n for n in _tc_added if n in self._last_run_find_tools_names],
            )
        self._last_logged_tool_names = list(st.tool_names)

    async def _fire_tool_hooks(self, st: RunOnceState) -> None:
        """开火 H14（装配套件）与 H15（第三方钉/砍工具），每点之后收口一次。

        收口 = exclusive 再剥离 + 去重。护栏：只认已注册的工具名、拒绝特权分类
        （``self`` / ``buildin`` / ``meta`` 是核心专用），且不许 drop ``create_subagent``。
        """
        from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, fire_hooks, should_fire

        for point in (AgentHookPoint.ASSEMBLE_TOOLS, AgentHookPoint.AFTER_ASSEMBLE_TOOLS):
            if not should_fire(point):
                continue
            ctx = AgentHookContext(
                point=point,
                ev=st.ev,
                bot=st.bot,
                session_id=self.session_id,
                persona_name=self.persona_name,
                create_by=self.create_by,
                is_subagent=self.is_subagent,
                addr_gated=st.addr_gated,
            )
            await fire_hooks(point, ctx)
            if ctx.ensured_tools or ctx.dropped_tools:
                self._apply_tool_mutations(st, ctx.ensured_tools, ctx.dropped_tools)
                self._reseal_tools(st)

    def _apply_tool_mutations(self, st: RunOnceState, ensured: List[str], dropped: List[str]) -> None:
        """按 hook 请求增删工具（永冻主会话不改 tools[]，改转当轮 user 侧种子提示）。"""
        from gsuid_core.ai_core.register import find_tool_base, is_core_only_category
        from gsuid_core.ai_core.buildin_tools.call_tool import format_compact_tool_schema
        from gsuid_core.ai_core.buildin_tools.find_tools_rank import tool_brief

        present = {t.name for t in st.tools}
        frozen_locked = st.frozen_core
        exclusive = _capability_exclusive_tool_names() if frozen_locked else set()
        hook_hints: list[str] = []
        for name in ensured:
            if name in present:
                continue
            tb = find_tool_base(name)
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
            # 永冻主会话不改线缆 tools[]；hook 请求 drop 的工具改为写入执行期拦截名单。
            _blk = _require_context(st).blocked_tool_names
            for name in dropped:
                if name == "create_subagent":
                    logger.warning(i18n_t("log.agent.hook_drop_delegation_denied", name=name))
                    continue
                _blk.add(name)
            return
        for name in dropped:
            if name == "create_subagent":
                logger.warning(i18n_t("log.agent.hook_drop_delegation_denied", name=name))
                continue
            st.tools = [t for t in st.tools if t.name != name]

    def _reseal_tools(self, st: RunOnceState) -> None:
        """内核收口：主人格交互轮再剥一遍 exclusive，然后去重。

        换套件也逃不掉这一步——不能借 hook 让主人格拿回 ``render_html_to_image``。
        """
        if self.create_by in _INTERACTIVE_CREATE_BY:
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

    def _run_once_build_agent_meta(self, st: RunOnceState) -> object:
        """构建 pydantic-ai Agent 与流式统计元数据；返回 Agent 实例。"""
        # 指定 return_model 时通过 st.output_type 强制 pydantic_ai 结构化输出。
        # 永冻主会话不按说话人在线缆层增删 run_skill_script，执行期统一拦截
        _skills_ts = skills_toolset if st.frozen_core else skills_toolset.filtered(skill_tool_visible)
        _keep_skills = not st.addr_gated or bool(self._session_toolset_frozen)
        _toolsets = [_skills_ts] if self.create_by in _SKILLS_CREATE_BY and _keep_skills else []
        if self.model:
            # 必须拷贝：self.model.settings 是模型对象的共享状态，就地改会污染后续所有 run
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
            # 归属透传（默认关闭）：把本次 run 的调用方标识按配置带给上游网关
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

        # 单工具抛错不炸整轮：SkillNotFound 等 → ⚠️ 回执，模型改道
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

        # 截断历史记录，避免无限制增长
        self.extract_history()
        # compact 保头；若首条 user 丢过 marker，补回第一条 user 末尾。
        self._inject_deepseek_rp_marker(st)

        # TTFT/TPS 流式统计：按"每次模型请求"打点，在对应 CallToolsNode 中结算入库。
        # st.req_start 在 ModelRequestNode 发起前记录；_first/st.last_event_at 由
        st.req_start = 0.0
        st.first_event_at = None
        st.last_event_at = None
        st.model_name = self.model.model_name if self.model else "unknown"
        st.provider = self.model.system if self.model else "unknown"
        # 流式响应需按模型 profile 规范化思考标签（默认与 DEFAULT_THINKING_TAGS 对齐）。
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
