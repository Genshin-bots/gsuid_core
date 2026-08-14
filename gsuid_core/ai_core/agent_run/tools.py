"""阶段 B：工具五层装配 + 构建 pydantic-ai Agent"""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings, merge_model_settings

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
    _pool_overlaps_capability_agent,
    _capability_exclusive_tool_names,
    _matched_delegation_only_profile,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.configs.attribution import resolve_attribution_settings


class ToolsPhase(RunOnceHost):
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
            # C-3：@别人且未点自己 → 零工具
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

                # 第 1.5 层：状态驱动工具池（L2）
                _state_pool_names: set[str] = set()
                try:
                    from gsuid_core.ai_core.tool_state_signals import get_state_driven_family_tools

                    state_tools = await get_state_driven_family_tools(
                        st.ev, core_names, has_active_task=st.has_active_task, intent=st.intent
                    )
                    if state_tools:
                        core_tools = core_tools + state_tools
                        core_names.update(t.name for t in state_tools)
                        _state_pool_names = {t.name for t in state_tools}
                except Exception as e:
                    logger.debug(i18n_t("log.agent.load_state_driven_pool", e=e))

                # C-1：跟进补调度族 + 产物族（追问产物需 artifact_get_recent）
                if st.followup_detected:
                    for _dom in ("定时任务", "长期任务编排", "产物"):
                        for _tb in get_tools_by_capability_domain(_dom):
                            if _tb.name not in core_names:
                                core_names.add(_tb.name)
                                core_tools.append(_tb.tool)
                    logger.debug(i18n_t("log.agent.scaffold_supplemented_scheduled_task"))

                # 第 1.6 层：会话驻留工具池（L3）
                if self._recent_tool_families:
                    for _dom, _ttl in list(self._recent_tool_families.items()):
                        if _ttl <= 0:
                            continue
                        for _tb in get_tools_by_capability_domain(_dom):
                            if _tb.name not in core_names:
                                core_names.add(_tb.name)
                                core_tools.append(_tb.tool)
                    self._recent_tool_families = {
                        _d: _t - 1 for _d, _t in self._recent_tool_families.items() if _t - 1 > 0
                    }

                # 附加工具池 = 语境工具池 + 查询工具池
                extra_tools: ToolList = []
                _ctx_pool_names: set[str] = set()

                # 第二层：语境工具池（群聊瘦模式也保留标签池，上限更紧）
                ctx_tags: list[str] = []
                if st.ev is not None and st.ev.group_id:
                    try:
                        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

                        scope_key = make_scope_key(ScopeType.GROUP, str(st.ev.group_id))
                        ctx_tags = await get_scope_context_tags(scope_key)
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
                _soft_cont = bool(st.tg.soft_continue) if st.tg is not None else False
                _ellip = bool(st.tg.ellipsis_followup) if st.tg is not None else False
                _skip_search = st.is_light or (
                    st.group_slim
                    and st.intent == "闲聊"
                    and not st.followup_detected
                    and not st.has_active_task
                    and not st.has_media
                    and not _ellip
                    and not _soft_cont
                )
                if (
                    st.intent == "闲聊"
                    and not st.followup_detected
                    and not st.has_active_task
                    and not self._recent_tool_families
                ):
                    _recall_limit = max(2, _recall_limit // 2)
                    max_extra_tools = max(3, max_extra_tools // 2)
                if st.group_slim or st.is_light:
                    max_extra_tools = min(max_extra_tools, 6)
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
                    )
                    # 补搜索族（瘦保底已含 web_search_tool；再补 fetch/knowledge）
                    if (st.group_slim or st.is_light) and st.intent in ("工具", "问答"):
                        for _tn in ("web_fetch_tool", "search_knowledge"):
                            if _tn in core_names:
                                continue
                            _tb = find_tool_base(_tn)
                            if _tb is not None:
                                core_names.add(_tn)
                                core_tools.append(_tb.tool)

                # 附加池：先按能力族整族展开（L4），再去重/限量。 召回族内任一工具即带出整族（剔除与保底重名/族内重复）
                deduped_extra = expand_tools_to_families(
                    extra_tools,
                    exclude_names=core_names,
                    max_tools=max_extra_tools,
                )

                # 召回族也写进 L3 驻留：下一轮并入稳定保底段，工具集随对话收敛，
                # provider 前缀缓存命中↑、跨轮追问免重检索（§cache 54%→更高）。
                for _et in deduped_extra:
                    _etb = find_tool_base(_et.name)
                    _edom = _etb.capability_domain if _etb is not None else None
                    if _edom:
                        self._recent_tool_families[_edom] = _STICKY_FAMILY_TURNS

                # §25(3) 工具序稳定化：两段各自按名排序，
                core_tools.sort(key=lambda _t: _t.name)
                deduped_extra.sort(key=lambda _t: _t.name)
                st.tools = core_tools + deduped_extra

                # 委派：剥离能力代理专属工具，逼主人格走 create_subagent
                # 状态/语境池工具不参与 exclusive 剥离，避免只读能力被误卸
                _did_strip_exclusive = False
                if self.create_by in _INTERACTIVE_CREATE_BY:
                    _exclusive = _capability_exclusive_tool_names()
                    _shielded = _ctx_pool_names | _state_pool_names
                    _exclusive = _exclusive - _shielded
                    if _exclusive:
                        _before = {t.name for t in st.tools}
                        _stripped = _before & _exclusive
                        if _stripped:
                            st.tools = [t for t in st.tools if t.name not in _exclusive]
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

                # 渐进式工具暴露：常挂 find_tools + RetrievableToolset（含误判闲聊轮）。
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

        logger.debug(i18n_t("log.agent.tool_list", p0=[tool.name for tool in st.tools]))

        # 最终去重（兼容外部直接传入 st.tools 的情况）
        st.tools = list({obj.name: obj for obj in st.tools}.values())
        st.tool_names = [t.name for t in st.tools]

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

    def _run_once_build_agent_meta(self, st: RunOnceState) -> Any:
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
        _agent = Agent(
            model=self.model,
            deps_type=ToolContext,
            system_prompt=self.system_prompt or "你是一个智能助手, 简短的一句话回答问题即可。",
            model_settings=_model_settings,
            tools=st.tools,
            toolsets=_toolsets,
            capabilities=[build_tool_safety_capability()],
            retries=3,
            output_type=st.output_type or str,
        )

        # 截断历史记录，避免无限制增长
        self.extract_history()

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
