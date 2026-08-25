"""阶段 B：工具五层装配 + 构建 pydantic-ai Agent"""

from __future__ import annotations

from typing import Any, List, Sequence

from pydantic_ai import Agent
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
    _STICKY_FAMILY_TURNS,
    ENABLE_PROGRESSIVE_TOOLS,
)
from gsuid_core.ai_core.utils import _normalize_thinking_tags
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.skills import skills_toolset
from gsuid_core.ai_core.register import find_tool_base, get_tools_by_capability_domain
from gsuid_core.ai_core.rag.tools import (
    ToolList,
    get_main_agent_tools,
    get_scope_context_tags,
    expand_tools_to_families,
    get_tools_by_context_tags,
    search_tools_with_entity_routing,
)
from gsuid_core.ai_core.tool_safety import build_tool_safety_capability
from gsuid_core.ai_core.agent_run.host import RunOnceHost
from gsuid_core.ai_core.agent_run.state import (
    RunOnceState,
    _require_context,
)
from gsuid_core.ai_core.dynamic_toolset import RetrievableToolset
from gsuid_core.ai_core.agent_run.support import (
    _INTERACTIVE_CREATE_BY,
    _append_user_text,
    _pool_overlaps_capability_agent,
    _capability_exclusive_tool_names,
    _matched_delegation_only_profile,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.tool_state_signals import STATE_DRIVEN_FAMILY_DOMAINS
from gsuid_core.ai_core.configs.attribution import resolve_attribution_settings
from gsuid_core.ai_core.agent_run.remote_web_search import attach_remote_web_search

_PROGRESS_TOOL = "check_delegation"
_PINNED_SESSION_TOOLS: tuple[str, ...] = ("find_tools", "create_subagent", "capability_map")
_LIGHT_REQUEST_FLOOR = 4
_SCHED_CREATE_NAMES: frozenset[str] = frozenset({"add_once_task", "add_interval_task"})
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
_GROUP_RECALL_NAMES: frozenset[str] = frozenset({"search_cognition", "find_tools", "create_subagent", "capability_map"})


def _session_tool_ceiling(*, group_slim: bool = False) -> int:
    key = "group_session_tool_ceiling" if group_slim else "session_tool_ceiling"
    raw = ai_config.get_config(key).data
    fallback = 20 if group_slim else 24
    return int(raw) if isinstance(raw, int) else fallback


def l2_state_driven_wanted(
    *,
    addr_gated: bool,
    is_group: bool,
    call_to_self: bool,
    followup_detected: bool,
) -> bool:
    """L2 是否加载持久实体对应的非 exclusive 族。群聊须点名或省略跟进；私聊始终 1:1。"""
    if addr_gated:
        return False
    if not is_group:
        return True
    return call_to_self or followup_detected


def group_idle_request_limit(
    default_limit: int,
    *,
    is_group: bool,
    followup_detected: bool,
    has_active_task: bool,
    idle_cap: int,
    is_light: bool = False,
    call_to_self: bool = False,
) -> int:
    """旁观收紧 request_limit。点名履约不收；LIGHT 保底够 find_tools+一次动作。"""
    if default_limit < 1 or idle_cap < 1:
        return default_limit
    if is_light:
        return min(default_limit, max(idle_cap, _LIGHT_REQUEST_FLOOR))
    if call_to_self:
        return default_limit
    if is_group and not followup_detected and not has_active_task:
        return min(default_limit, idle_cap)
    return default_limit


def snapshot_tool_allowed(
    name: str,
    *,
    create_ok: bool,
    mutate_ok: bool,
    recall_ok: bool,
) -> bool:
    """是否把该名作为**本轮新 extras**。不用于从 frozen schema 摘名。"""
    if name in _SCHED_CREATE_NAMES:
        return create_ok
    if name in _SCHED_MUTATE_NAMES:
        return mutate_ok
    if name in _GROUP_RECALL_NAMES:
        return recall_ok
    return True


def should_skip_tool_search(
    *,
    in_flight_short: bool,
    group_slim: bool,
    followup_detected: bool,
    has_active_task: bool,
    has_media: bool,
    call_to_self: bool,
    is_light: bool,
) -> bool:
    """向量预检索是否跳过。点名+LIGHT 不预检索但仍挂 find_tools；FULL 才检索。"""
    if in_flight_short:
        return True
    if not group_slim:
        return False
    if followup_detected or has_active_task or has_media:
        return False
    return (not call_to_self) or is_light


def _snapshot_visibility_flags(st: RunOnceState) -> tuple[bool, bool, bool]:
    """从 run_extra 读创建/变更/回想三旗；缺旗偏可见。"""
    from gsuid_core.ai_core.buildin_tools.visibility import (
        GROUP_RECALL_OK_KEY,
        SCHED_CREATE_OK_KEY,
        SCHED_MUTATE_OK_KEY,
    )

    extra = st.run_extra
    create_ok = True if SCHED_CREATE_OK_KEY not in extra else bool(extra[SCHED_CREATE_OK_KEY])
    mutate_ok = True if SCHED_MUTATE_OK_KEY not in extra else bool(extra[SCHED_MUTATE_OK_KEY])
    recall_ok = True if GROUP_RECALL_OK_KEY not in extra else bool(extra[GROUP_RECALL_OK_KEY])
    return create_ok, mutate_ok, recall_ok


def is_group_send_extra(name: str) -> bool:
    """群聊 extras 里的对用户发送工具（不在瘦核）。只许本轮 find_tools 动态暴露。"""
    return name.startswith("send_") and name not in interaction_scaffold.SLIM_GROUP_CORE_TOOLS


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
        if domain in STATE_DRIVEN_FAMILY_DOMAINS:
            continue
        for tb in get_tools_by_capability_domain(domain):
            _add(tb.name)
    return out


STATE_PERSISTED_FAMILY_HINT = (
    "\n\n（系统提示：当前会话已有持久条目。变更已有条目请用对应能力族的查询/修改/取消工具，不要再创建一条来代替。）"
)


def _take_extra_seeds(tools: ToolList, exclude_names: set[str], max_tools: int) -> ToolList:
    """群聊 extras 只保留召回种子，不整族展开。"""
    out: ToolList = []
    seen = set(exclude_names)
    cap = max_tools if max_tools > 0 else 0
    for t in tools:
        if t.name in seen:
            continue
        seen.add(t.name)
        out.append(t)
        if cap and len(out) >= cap:
            break
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


def _kernel_owns_tool_assembly() -> bool:
    """``tool_assembly`` 槽是否仍由第一方套件占据（= 内核跑五层装配）。

    槽位 ``off`` 或被用户套件占据时返回 False：内核让位，只留调用方传入的工具
    与自己的 exclusive 收口。总闸关闭时视为内核自管（回落纯内核编排）。

    判据取**配置**而非运行期占用表：占用表是在 ``load_enabled_kits`` 里填的，而它排在
    ``_INIT_STEPS`` 后段。用占用表会把「套件还没加载完」误读成「用户把槽拆了」，于是
    在启动窗口内所有请求都退化成零工具（``find_tools`` 一并消失）——实测某个前置
    init 步骤卡住数分钟时，整轮基准 24 例全部 0 工具调用。「没装好」不等于「不要装」，
    这个门必须 fail-open。
    """
    from gsuid_core.ai_core.kits import resolve_slot_config
    from gsuid_core.ai_core.hooks import hooks_enabled

    if not hooks_enabled():
        return True
    configured = resolve_slot_config("tool_assembly")
    if not configured:
        return False
    return all(kit_id.startswith("gscore.") for kit_id in configured)


class ToolsPhase(RunOnceHost):
    def _stabilize_session_toolset(
        self,
        core: ToolList,
        extras: ToolList,
        ctx_tags: list[str],
        *,
        group_slim: bool = False,
    ) -> ToolList:
        """Append-only：首轮拍快照（瘦核 ∪ pin）；其后只 append，exclusive 永不进表。"""
        tags = frozenset(ctx_tags)
        rebuild = self._session_toolset_frozen is None
        exclusive = _capability_exclusive_tool_names()
        incoming: list[str] = []
        for t in list(core) + list(extras):
            if t.name not in incoming:
                incoming.append(t.name)
        for name in self._session_appended_tools:
            if name not in incoming:
                incoming.append(name)
        names = stabilize_session_tool_names(
            None if rebuild else self._session_toolset_frozen,
            incoming,
            exclusive=exclusive,
            ceiling=_session_tool_ceiling(group_slim=group_slim),
        )
        self._session_toolset_frozen = names
        self._session_toolset_tags = tags
        out: ToolList = []
        for name in names:
            tb = find_tool_base(name)
            if tb is not None:
                out.append(tb.tool)
        return out

    async def _run_once_assemble_tools(self, st: RunOnceState) -> None:
        """工具五层装配 + 去重 + 渐进式暴露。"""
        # 渐进式工具暴露是否在本轮生效（仅自动装配 + 非闲聊轮）。决定是否挂 RetrievableToolset。
        st.expose_dynamic = False
        st.is_light = st.cheap is interaction_scaffold.CheapGate.LIGHT if st.cheap is not None else False
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
        # light 与 full 群聊均走瘦保底；light 不再清工具，只是少检索 + 短回 hint
        st.group_slim = bool(st.tg is not None and bool(st.tg.is_group) and self.create_by in _INTERACTIVE_CREATE_BY)

        # dynamic 能力族门：显式 True/False 优先；None 沿用旧门（agentic 且未传 st.tools）。
        if self.dynamic_tools is not None:
            _assemble = self.dynamic_tools
        else:
            _assemble = self.create_by in _AGENTIC_CREATE_BY and not st.tools

        # tool_assembly 槽为 off（或被用户套件占据）时，内核**不跑**五层自动装配：
        # 只留调用方传入的工具 + 下方的 exclusive 剥离与委派补全。
        # 副作用：find_tools 是 meta 分类、由装配层注入，本槽 off 时渐进式工具发现
        # 一并消失——这是正确行为（用户套件无权声明特权分类）。
        if _assemble and not _kernel_owns_tool_assembly():
            logger.info(i18n_t("log.agent.tool_assembly_slot_not_kernel"))
            _assemble = False

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
            # C-3 寻址门（**内核密封**）：@别人且未点自己 → 零工具，且不打 ASSEMBLE_TOOLS
            st.tools = []
        elif _assemble or self.create_by in _AGENTIC_CREATE_BY:
            if _assemble:
                qy = ""
                # 框架回灌：交付包不作向量检索 query（避免噪声 + 误装工具）
                if not st.fw_msg:
                    if isinstance(st.user_message, str):
                        qy = st.user_message
                    elif st.ev is not None:
                        qy = st.ev.raw_text

                # 第一层：保底池。群聊（含 light）瘦保底；私聊/能力代理仍全量。
                if st.group_slim or st.is_light:
                    core_tools = []
                    core_names: set[str] = set()
                    for _tn in interaction_scaffold.SLIM_GROUP_CORE_TOOLS:
                        _tb = find_tool_base(_tn)
                        if _tb is not None and _tn not in core_names:
                            core_names.add(_tn)
                            core_tools.append(_tb.tool)
                else:
                    core_tools = await get_main_agent_tools()
                    core_names = {t.name for t in core_tools}

                if st.group_slim or st.is_light:
                    _fam_exclusive = _capability_exclusive_tool_names()
                    for _tn in complete_kernel_family_names(list(core_names), exclusive=_fam_exclusive):
                        if _tn in core_names:
                            continue
                        _tb = find_tool_base(_tn)
                        if _tb is not None:
                            core_names.add(_tn)
                            core_tools.append(_tb.tool)

                # 调用方显式传入的基础工具（dynamic 节点的 packs+白名单）并入保底
                for _bt in st.tools:
                    if _bt.name not in core_names:
                        core_names.add(_bt.name)
                        core_tools.append(_bt)

                # 节点显式白名单：persona 投影节点在 config.json 声明的 st.tool_names 并入保底
                if self.persona_name and not st.group_slim:
                    from gsuid_core.ai_core.agent_node import get_node as _get_agent_node

                    _node = _get_agent_node(self.persona_name)
                    if _node is not None and _node.tool_names:
                        for _tn in _node.tool_names:
                            if _tn in core_names:
                                continue
                            _tb = find_tool_base(_tn)
                            if _tb is not None:
                                core_names.add(_tn)
                                core_tools.append(_tb.tool)

                # L2：有持久实体则补该族非 exclusive 工具（点名/跟进/私聊；旁观不加）
                extra_tools: ToolList = []
                _is_group = bool(st.tg is not None and st.tg.is_group)
                _call_self = bool(st.tg is not None and st.tg.call_to_self)
                if l2_state_driven_wanted(
                    addr_gated=st.addr_gated,
                    is_group=_is_group,
                    call_to_self=_call_self,
                    followup_detected=st.followup_detected,
                ):
                    try:
                        from gsuid_core.ai_core.tool_state_signals import (
                            get_state_driven_families,
                            get_state_driven_family_tools,
                        )

                        _exclusive_now = _capability_exclusive_tool_names()
                        _l2_domains = await get_state_driven_families(st.ev, has_active_task=st.has_active_task)
                        if _l2_domains:
                            extra_tools += await get_state_driven_family_tools(
                                st.ev,
                                exclude_names=core_names | _exclusive_now,
                                has_active_task=st.has_active_task,
                            )
                            st.final_user_message = _append_user_text(
                                st.final_user_message, STATE_PERSISTED_FAMILY_HINT
                            )
                    except Exception as e:
                        logger.debug(i18n_t("log.agent.load_state_driven_pool", e=e))

                # L3 不写 core；跨轮靠 find_tools 成功后 append。TTL 仍递减。
                _interactive = self.create_by in _INTERACTIVE_CREATE_BY
                if self._recent_tool_families:
                    self._recent_tool_families = {
                        _d: _t - 1 for _d, _t in self._recent_tool_families.items() if _t - 1 > 0
                    }

                # 交互主人格：进度查询不常挂，追问时经 find_tools 进尾槽。
                if _interactive:
                    core_tools = _without_progress_tool(core_tools)
                    core_names.discard(_PROGRESS_TOOL)
                    extra_tools = _without_progress_tool(extra_tools)

                # 附加工具池 = L2/跟进尾槽 + 语境 + 查询
                _ctx_pool_names: set[str] = set()

                # 第二层：语境工具池（群聊瘦模式也保留标签池，上限更紧）
                ctx_tags: list[str] = []
                ctx_scope_key = ""
                if st.ev is not None and st.ev.group_id:
                    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

                    ctx_scope_key = make_scope_key(ScopeType.GROUP, str(st.ev.group_id))
                if ctx_scope_key and not st.in_flight_short:
                    try:
                        ctx_tags = await get_scope_context_tags(ctx_scope_key)
                        if ctx_tags:
                            _ctx_max = 4 if st.group_slim else 8
                            ctx_tools = get_tools_by_context_tags(ctx_tags, max_count=_ctx_max)
                            if ctx_tools:
                                extra_tools += ctx_tools
                                _ctx_pool_names = {t.name for t in ctx_tools}
                                logger.debug(
                                    i18n_t(
                                        "log.agent.contextual_pool_context_tags",
                                        p0=len(ctx_tools),
                                        ctx_tags=ctx_tags,
                                    )
                                )
                    except Exception as e:
                        logger.debug(i18n_t("log.agent.load_contextual_pool", e=e))

                # 第三层：向量检索。light 或群聊纯闲聊可跳过（保底已含搜/图/渲/调度入口）。
                # soft_continue / ellipsis 与呼叫跟进同权：不得因 st.intent=闲聊 跳过检索。
                _recall_limit = int(ai_config.get_config("tool_search_recall").data)
                max_extra_tools: int = int(ai_config.get_config("tool_extra_pool_max").data)
                _recall_threshold = float(ai_config.get_config("tool_recall_threshold").data)
                _skip_search = should_skip_tool_search(
                    in_flight_short=st.in_flight_short,
                    group_slim=st.group_slim,
                    followup_detected=st.followup_detected,
                    has_active_task=st.has_active_task,
                    has_media=st.has_media,
                    call_to_self=_call_self,
                    is_light=st.is_light,
                )
                if (
                    st.intent == "闲聊"
                    and not st.followup_detected
                    and not st.has_active_task
                    and not self._recent_tool_families
                ):
                    _recall_limit = max(2, _recall_limit // 2)
                    max_extra_tools = max(3, max_extra_tools // 2)
                if st.group_slim or st.is_light or st.in_flight_short:
                    max_extra_tools = min(max_extra_tools, 6)
                if st.in_flight_short:
                    max_extra_tools = min(max_extra_tools, 2)
                if qy and not _skip_search:
                    search_query = interaction_scaffold.build_tool_search_query(
                        qy,
                        self._recent_user_texts,
                        ctx_tags,
                    )
                    logger.debug(i18n_t("log.agent.attempting_search_tools_query", search_query=search_query))

                    extra_tools += await search_tools_with_entity_routing(
                        query=search_query,
                        route_text=qy,
                        limit=_recall_limit,
                        non_category=["self", "buildin"],
                        threshold=_recall_threshold,
                        scope_key=ctx_scope_key,
                    )
                    # 外部检索不进瘦核；问答/工具轮才 append（不钉核，避免闲聊付税）
                    if (st.group_slim or st.is_light) and st.intent in ("工具", "问答"):
                        for _tn in ("web_search_tool", "web_fetch_tool"):
                            if _tn in core_names:
                                continue
                            _tb = find_tool_base(_tn)
                            if _tb is not None:
                                extra_tools.append(_tb.tool)

                # 对用户发送 extras 不进静态附加池；只许本轮 find_tools 动态暴露
                if st.group_slim or st.is_light or _interactive:
                    extra_tools = [t for t in extra_tools if not is_group_send_extra(t.name)]

                # 群聊 extras 只留种子；私聊仍整族展开（能建就能改靠核内族闭合，不靠 L4）
                if _interactive:
                    extra_tools = _without_progress_tool(extra_tools)
                _create_ok_ex, _mutate_ok_ex, _ = _snapshot_visibility_flags(st)
                extra_tools = [
                    t
                    for t in extra_tools
                    if snapshot_tool_allowed(
                        t.name,
                        create_ok=_create_ok_ex,
                        mutate_ok=_mutate_ok_ex,
                        recall_ok=True,
                    )
                ]
                if st.group_slim:
                    deduped_extra = _take_extra_seeds(extra_tools, core_names, max_extra_tools)
                else:
                    deduped_extra = expand_tools_to_families(
                        extra_tools,
                        exclude_names=core_names,
                        max_tools=max_extra_tools,
                    )
                if _interactive:
                    deduped_extra = _without_progress_tool(deduped_extra)

                # L3 只记族 TTL，不把专属工具写进 core（exclusive 会闪烁前缀）
                for _et in deduped_extra:
                    _etb = find_tool_base(_et.name)
                    _edom = _etb.capability_domain if _etb is not None else None
                    if _edom:
                        self._recent_tool_families[_edom] = _STICKY_FAMILY_TURNS

                st.tools = self._stabilize_session_toolset(
                    core_tools, deduped_extra, ctx_tags, group_slim=st.group_slim
                )
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

                _need_subagent = _did_strip_exclusive
                deleg_pid = ""
                if qy:
                    deleg_pid = _matched_delegation_only_profile(qy)
                    if deleg_pid:
                        _need_subagent = True
                    elif not _need_subagent:
                        _pool_names = {t.name for t in st.tools}
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

                # 渐进式工具暴露：PIN 恒在 schema；未点名由 check_func 拒执行。
                if ENABLE_PROGRESSIVE_TOOLS:
                    if any(t.name == "find_tools" for t in st.tools):
                        st.expose_dynamic = True
                    else:
                        ft = find_tool_base("find_tools")
                        if ft is not None:
                            st.tools.append(ft.tool)
                            st.expose_dynamic = True
                    if st.expose_dynamic:
                        logger.debug(i18n_t("log.agent.find_tools_progressive_exposure"))

                logger.debug(
                    i18n_t(
                        "log.agent.tool_count_baseline_extra",
                        p0=len(st.tools),
                        p1=len(core_tools),
                        p2=len(deduped_extra),
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
            logger.debug(i18n_t("log.agent.skip_tool_search_searching_tools"))

        # H14 / H15：工具装配套件与第三方钉工具。两点之后**各剥离一次** exclusive——
        # H14 后防套件直接装上 render_*，H15 后防第三方 ensure 回来。
        # addr_gated 时两点都不打（C-3 零工具硬约束）。
        if not st.addr_gated:
            await self._fire_tool_hooks(st)

        logger.debug(i18n_t("log.agent.tool_list", p0=[tool.name for tool in st.tools]))

        # 最终去重（兼容外部直接传入 st.tools 的情况）
        st.tools = list({obj.name: obj for obj in st.tools}.values())
        st.tool_names = [t.name for t in st.tools]
        st.exposed_tool_names = list(st.tool_names)
        _ctx = _require_context(st)
        from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY

        _ctx.extra[EXPOSED_TOOLS_EXTRA_KEY] = list(st.tool_names)

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
        """按 hook 请求增删工具（护栏在此，不在 Context 里）。"""
        from gsuid_core.ai_core.register import find_tool_base, is_core_only_category

        present = {t.name for t in st.tools}
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
            present.add(name)
            st.tools.append(tb.tool)
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
        # 当 return_model 指定时，使用 st.output_type 让 pydantic_ai 强制结构化输出
        # st.output_type 默认为 str（返回文本），指定 Pydantic 模型时强制返回结构化 JSON
        _toolsets = [skills_toolset] if self.create_by in _SKILLS_CREATE_BY and not st.addr_gated else []
        # 启用渐进式暴露时挂 RetrievableToolset：每个 step 读 dynamic_tool_names 即时暴露命中工具。
        # exclude_names：静态池 + 能力代理专属（防 find_tools 把已剥离工具回灌主人格）。
        if st.expose_dynamic:
            _dyn_exclude = set(st.tool_names) | set(_require_context(st).blocked_tool_names)
            _toolsets = [*_toolsets, RetrievableToolset(exclude_names=_dyn_exclude)]
        # eval_mode 下固定 temperature=0：记忆评测的答案须可复现，
        from gsuid_core.ai_core.memory.config import memory_config

        if self.model:
            # 必须拷贝：self.model.settings 是模型对象的共享状态，就地改会污染后续所有 run
            _base_settings: ModelSettings = self.model.settings.copy() if self.model.settings else ModelSettings()
            if memory_config.eval_mode:
                _base_settings["temperature"] = 0.0
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
        # 流式响应下需手动按完整文本重新拆分内嵌 <think> 标签（见 _split_embedded_thinking）。
        # thinking_tags 取自模型 profile；缺省与 pydantic_ai DEFAULT_THINKING_TAGS 对齐。
        # 裸名 ('think','think') 会误伤英文思考里的单词 think，必须先规范化。
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
