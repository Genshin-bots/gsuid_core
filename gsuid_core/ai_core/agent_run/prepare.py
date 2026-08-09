"""阶段 A：预算闸门 → 初始化状态 → 装配 user 消息"""

from __future__ import annotations

import time
import uuid
from typing import Any, Sequence

from sqlalchemy.exc import SQLAlchemyError
from pydantic_ai.usage import UsageLimits

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core import (
    wall_clock,
    interaction_scaffold,
)
from gsuid_core.ai_core.const import (
    _PROGRESSIVE_TOOLS_SKIP_INTENTS,
)
from gsuid_core.ai_core.utils import (
    _truncate_message_for_log,
    notify_master_of_budget_block,
)
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.agent_run.host import RunOnceHost
from gsuid_core.ai_core.agent_run.state import (
    BUDGET_GATE_PASS,
    RunOnceState,
)
from gsuid_core.ai_core.persona.prompts import INNER_OS_MARKER
from gsuid_core.ai_core.agent_run.support import (
    _STATUS_INQUIRY_HINT,
    _INTERACTIVE_CREATE_BY,
    _append_user_text,
    _capability_exclusive_tool_names,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.agent_run.budget_ctx import _current_budget_scope
from gsuid_core.ai_core.agent_run.speech_policy import (
    resolve_speech_policy,
    looks_like_status_inquiry,
)
from gsuid_core.ai_core.agent_run.user_turn_ctx import get_user_turn_id, set_user_turn_id


class PreparePhase(RunOnceHost):
    async def _run_once_budget_gate(self, st: RunOnceState) -> Any:
        """预算闸门：超额返回早退值；放行返回 ``BUDGET_GATE_PASS``。"""
        # ============ 预算闸门 + scope 解析（统一入口）============
        # 仅 st.budget_gate=True 的自主入口在此早退；放行/未启用/豁免均零额外开销。
        st.budget_scope = self._resolve_budget_scope(st.ev)
        if st.budget_gate and st.budget_scope is not None:
            try:
                from gsuid_core.ai_core.budget import budget_manager

                _bd = await budget_manager.check_scope(
                    st.budget_scope[0], st.budget_scope[1], st.budget_scope[2], self.session_id
                )
            except SQLAlchemyError as _be:
                logger.warning(i18n_t("log.agent.budget_check_db_fail", _be=_be))
                _bd = None
            except Exception as _be:
                logger.exception(i18n_t("log.agent.budget_check_fail_allowing", _be=_be))
                _bd = None
            if _bd is not None and not _bd.allowed:
                logger.info(
                    i18n_t(
                        "log.agent.budget_exceeded_intercepted_create",
                        p0=self.create_by,
                        p1=_bd.block_scope_label,
                    )
                )
                # 仅交互式（有 st.bot + st.ev）时处理用户提示与主人告警；自主后台静默掐断。
                if st.bot is not None and st.ev is not None:
                    if _bd.notify and _bd.message:
                        try:
                            await st.bot.send(_bd.message)
                        except Exception as _se:
                            logger.warning(i18n_t("log.agent.budget_exceeded_notice_se", _se=_se))
                    # 主人告警独立于用户提示：运行层拦截也同步给主人，便于与会话层闸区分开排查
                    await notify_master_of_budget_block(
                        bot=st.bot,
                        ev=st.ev,
                        decision=_bd,
                    )
                return None if st.output_type is not None else ""

        return BUDGET_GATE_PASS

    def _run_once_init_state(self, st: RunOnceState) -> None:
        """scope 绑定、环内可变状态、limits、ToolContext。"""
        # 提前到 try 前设置归属 scope：使本次 run 期间未显式绑定 scope 的嵌套 LLM 调用（含
        # _prepare_user_message 的图片理解）都按此记账；finally 还原，泄漏至多止于本 task。
        st.budget_scope_token = _current_budget_scope.set(st.budget_scope) if st.budget_scope is not None else None

        st.tool_call_list = []  # 用于记录本次运行中被调用的工具列表，供后续统计使用
        # 同引用暴露给 _execute_run 的干净重试分支：判断失败前是否已有工具副作用（F14）
        self._last_attempt_tool_calls = st.tool_call_list
        st.wall_nudged = False  # C-4 墙钟软预算：每 run 至多注入一次收敛提示
        # 出戏防火墙拦下的文本段（§D.4）：iter 结束后走"提醒→重说→放行"闭环
        st.ooc_blocked = []
        # 输出闸门：待注入 REWRITE feedback（同 response 可多段合并）；熔断后本轮静默
        st.ab_pending_nudges = []
        st.ab_abort = False
        # 假完成预检暂扣的文本段：声明完成但至今零工具——iter 后按"动作是否真发生"补发或纠正
        st.fab_blocked = []
        # 本轮是否见过结构化工具返回（用于出图履约闸）
        st.saw_structured_return = False
        # 本轮是否已委派 render_agent（勿把 research 的 create_subagent 当成已出图）
        st.delegated_render = False
        # 同工具空转计数：连续同名工具调用次数；达阈值后注入 thrash fuse（每 run 一次）
        st.same_tool_streak = 0
        st.same_tool_name = ""
        st.thrash_fused = False
        st.thinking_segments = []  # 累积本轮模型 thinking 文本，供意图-行为一致性检测
        # A: 被同 Session 更新消息 supersede 时置位，不写 history、不收尾发
        st.generation_cancelled = False
        st.cancel_ev = self._cancel_generation
        st.speech_policy = "free"
        st.status_inquiry = False
        st.pending_async_delivery = False
        st.image_sent_this_run = False
        st.has_status_tool_call = False
        st.report_speech_blocked = False
        st.wait_comfort_sent = False

        # 使用自定义迭代次数限制（如果有），否则使用配置默认值
        if self.max_iterations is not None:
            st.limits = UsageLimits(request_limit=self.max_iterations)
        else:
            multi_agent_lenth: int = ai_config.get_config("multi_agent_lenth").data
            st.limits = UsageLimits(request_limit=multi_agent_lenth)

        # 记录开始时间用于延迟统计
        st.start_time = time.time()
        # C-4 墙钟时钟：ask_user 等"挂起等人"的时段记进 excluded，判定预算时扣除。
        # token 在 finally 还原，否则嵌套 run（图片理解/subagent）会顶掉本 run 的时钟。
        st.wall_acc, st.wall_clock_token = wall_clock.install_clock()

        logger.info(i18n_t("log.agent.run_start_started"))
        # st.turn_id：本轮 Agent Run 的唯一标识（= agent_run_id），写入 ToolContext.extra
        # 供子工具读取（如 scheduler 单轮节流）。run 结束 finally 清理。
        st.turn_id = uuid.uuid4().hex
        # 用户回合（User Turn）：仅主人格交互 root 新建；嵌套 run 继承 contextvar。
        # 与 turn_id/agent_run_id 分离——子代理有自己的 run id，但共享同一 user_turn_id。
        st.user_turn_token = None
        st.owns_user_turn = False
        if self.create_by in ("Chat", "Agent") and not self.is_subagent:
            st.user_turn_id = st.turn_id
            st.owns_user_turn = True
            st.user_turn_token = set_user_turn_id(st.user_turn_id)
        else:
            _inherited_ut = get_user_turn_id()
            st.user_turn_id = _inherited_ut or ""
        # 交互主人格：专属工具从静态池剥离后，同步写入 blocked，堵住 find_tools 回灌
        st.blocked_exclusive = _capability_exclusive_tool_names() if self.create_by in _INTERACTIVE_CREATE_BY else set()
        # 出站：主人格交互会话；Kanban_Relay 是人格播报专用（非能力代理）。
        # 能力代理 / 通用 subagent 一律 False——产物只回上游，由主人格或 Relay 发。
        st.allow_outbound = self.create_by == "Kanban_Relay" or (
            self.create_by in ("Chat", "Agent") and not self.is_subagent
        )
        st.run_extra = {
            "turn_id": st.turn_id,
            "agent_run_id": st.turn_id,
            "run_sent_texts": self._run_sent_texts,
        }
        if st.user_turn_id:
            st.run_extra["user_turn_id"] = st.user_turn_id
        # 框架回灌：强制 @ 任务 owner（st.ev.user_id 已由 Kanban 填为 owner）
        st.fw_msg = isinstance(st.user_message, str) and (
            st.is_framework_injection or st.user_message.lstrip().startswith("[框架·")
        )
        if st.fw_msg and st.ev is not None and st.ev.user_id:
            st.run_extra["at_user_id"] = str(st.ev.user_id)
        # 话术策略：框架轮 / 进度追问 / 普通（详见 speech_policy）
        _probe_for_policy = ""
        if isinstance(st.user_message, str):
            _probe_for_policy = st.user_message
        st.status_inquiry = (not st.fw_msg) and looks_like_status_inquiry(
            _probe_for_policy,
            has_active_task=st.has_active_task,
        )
        st.speech_policy = resolve_speech_policy(
            is_framework=st.fw_msg,
            fake_done_retry=st.fake_done_retry,
            is_status_inquiry=st.status_inquiry,
            has_active_task=st.has_active_task,
            user_text=_probe_for_policy,
        )
        st.context = ToolContext(
            bot=st.bot,
            ev=st.ev,
            # run_sent_texts 同引用透传：send_message_by_ai 等工具内发送路径与主循环
            # 共用同一去重集合，干净历史重试不再重复发送相同文本（评审修复 F14）
            extra=st.run_extra,
            parent_session_id=self.session_id,
            blocked_tool_names=st.blocked_exclusive,
            allow_user_outbound=st.allow_outbound,
        )

    async def _run_once_prepare_user_message(self, st: RunOnceState) -> None:
        """用户消息外壳 / RAG / DS / 无工具提醒 / 脚手架 hints / session log。"""
        # 记录原始用户问题，供后续强制总结使用
        st.last_user_question = ""
        if isinstance(st.user_message, str):
            st.last_user_question = st.user_message.strip()
        elif isinstance(st.user_message, Sequence):
            # 从 Sequence[UserContent] 中提取纯文本
            st.last_user_question = "\n".join(item for item in st.user_message if isinstance(item, str)).strip()

        # 处理用户消息：框架注入不加 [用户发言]；真人句才加外壳
        if isinstance(st.user_message, Sequence) and not isinstance(st.user_message, str):
            st.final_user_message = await self._prepare_user_message(list(st.user_message))
        elif st.fw_msg and isinstance(st.user_message, str):
            st.final_user_message = st.user_message
        else:
            st.final_user_message = f"[用户发言]\n{st.user_message}"

        # history：框架注入的 UserPrompt 整段剥掉（不进 B 轨，避免被当成群友）
        # 真人轮才 lean 成精简发言
        if st.fw_msg:
            st.lean_user_message = ""
        else:
            st.lean_user_message = (
                list(st.final_user_message) if isinstance(st.final_user_message, list) else st.final_user_message
            )

        if st.rag_context:
            st.final_user_message = _append_user_text(st.final_user_message, f"\n\n{st.rag_context}")
            logger.info(i18n_t("log.agent.added_rag_context"))

        # DS 专属角色扮演模式（inner_os）：仅在 Chat 模式首轮 st.user_message 末尾追加
        if (
            self.create_by == "Chat"
            and not self.history
            and ai_config.get_config("enable_deepseek_rp").data
            and isinstance(st.final_user_message, str)
        ):
            st.final_user_message = f"{st.final_user_message}{INNER_OS_MARKER}"
            logger.info(i18n_t("log.agent.ds_inject"))

        # 连续无工具调用检测：连续两轮只推脱不调工具时注入强制提醒。闲聊类意图豁免（§15）
        # 豁免口径唯一定义在 _PROGRESSIVE_TOOLS_SKIP_INTENTS（评审修复 E12）。
        if (
            self.create_by in ["Chat", "Agent"]
            and self._consecutive_no_tool_rounds >= 2
            and st.intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS
        ):
            no_tool_reminder = (
                "\n\n【⚠️ 系统检测】你已连续多轮未调用任何工具，"
                "当前用户问题可能尚未得到有效回答。"
                "若你上一轮的思考里明确提到要调用某个工具（如 register_kanban_task、"
                "evaluate_agent_mesh_capability、create_subagent）却没有真正调用——"
                "口头答应 ≠ 执行，请本轮立即调用对应工具。否则请立即检查工具列表，"
                "选择最合适的工具调用，或明确说明为何确实无工具可用——禁止以角色"
                "不懂为由跳过工具。"
            )
            st.final_user_message = _append_user_text(st.final_user_message, no_tool_reminder)
            logger.debug(i18n_t("log.agent.forced_nudge_consecutive_turns"))

        # ── 交互脚手架：优先消费入口 TurnGraph；缺省时现场构建 ──
        st.addr_gated = False
        st.followup_detected = False
        st.tg = st.turn_graph
        st.cheap = st.cheap_gate
        if self.create_by in _INTERACTIVE_CREATE_BY:
            _cur_text = st.last_user_question
            _probe = st.ev.raw_text if st.ev is not None and st.ev.raw_text else st.last_user_question
            _is_tome = bool(st.ev.is_tome) if st.ev is not None else False
            _recent = interaction_scaffold.recent_history_texts(self.history)
            if st.tg is None:
                _spk0 = str(st.ev.user_id) if st.ev is not None else ""
                _spk0 = interaction_scaffold.extract_speaker_id(_cur_text) or _spk0
                _ut = "direct"
                if st.ev is not None:
                    _ut = str(st.ev.user_type or ("group" if st.ev.group_id else "direct"))
                st.tg = interaction_scaffold.build_turn_graph(
                    _probe or _cur_text,
                    persona_name=self.persona_name or "",
                    is_tome=_is_tome,
                    user_type=_ut,
                    primary_speaker=_spk0,
                    recent=_recent,
                    recent_tool_call=interaction_scaffold.has_recent_tool_call(self.history),
                    followup_max_len=int(ai_config.get_config("scaffold_followup_max_len").data),
                    ambient_max_len=int(ai_config.get_config("scaffold_ambient_max_len").data),
                )
            if st.cheap is None:
                st.cheap = interaction_scaffold.decide_cheap_gate(
                    st.tg, has_active_task=st.has_active_task, intent=str(st.intent or "")
                )
            st.addr_gated = bool(st.tg.address_gated)
            st.followup_detected = bool(st.tg.needs_task_tools)
            _hints = interaction_scaffold.scaffold_hints_from_graph(st.tg, cheap=st.cheap)
            # C-2：≥2 且比上轮增加才保留漂移提醒（hints 里可能已有，按计数裁）
            _pushes = st.tg.style_push_count
            if interaction_scaffold.DRIFT_REMINDER in _hints:
                if not (_pushes >= 2 and _pushes > self._last_drift_push_count):
                    _hints = [h for h in _hints if h is not interaction_scaffold.DRIFT_REMINDER]
                else:
                    logger.debug(i18n_t("log.agent.scaffold_drift_budget_reminder_inject", _pushes=_pushes))
            self._last_drift_push_count = _pushes
            if st.addr_gated:
                logger.info(i18n_t("log.agent.scaffold_addressing_gate_directed_create"))
            elif st.tg.ellipsis_followup:
                logger.debug(i18n_t("log.agent.scaffold_ellipsis_style_follow_inject"))
            for _h in _hints:
                st.final_user_message = _append_user_text(st.final_user_message, _h)

        # 用户追问进行中任务：再判一次（用 last_user_question 剥壳后）并注入进度契约
        if not st.fw_msg and not st.status_inquiry:
            st.status_inquiry = looks_like_status_inquiry(
                st.last_user_question,
                has_active_task=st.has_active_task,
            )
            if st.status_inquiry and st.has_active_task:
                st.speech_policy = resolve_speech_policy(
                    is_framework=False,
                    fake_done_retry=st.fake_done_retry,
                    is_status_inquiry=True,
                    has_active_task=True,
                    user_text=st.last_user_question,
                )
        if st.status_inquiry and st.has_active_task and self.create_by in ("Chat", "Agent"):
            st.final_user_message = _append_user_text(st.final_user_message, _STATUS_INQUIRY_HINT)
            logger.debug(i18n_t("log.agent.scaffold_ellipsis_style_follow_inject"))

        # 4.7 supersede 交接：上一 run 被抢答时有在途子代理委派 → 注入一句交接语后清空。
        if self._pending_delegation_handoff and not st.fw_msg:
            st.final_user_message = _append_user_text(st.final_user_message, self._pending_delegation_handoff)
            self._pending_delegation_handoff = ""

        # 截断日志输出中的 base64 数据，避免日志过长
        truncated_msg = _truncate_message_for_log(st.final_user_message)
        logger.trace(i18n_t("log.agent.user_truncated_msg", truncated_msg=truncated_msg))

        # session logger：框架注入单独记账，不计入 user_input
        self._session_logger.log_run_start()
        if st.fw_msg and isinstance(st.final_user_message, str):
            self._session_logger.log_system_injection(st.final_user_message, source="framework")
        else:
            self._session_logger.log_user_input(st.final_user_message)

        if st.tools is None:
            st.tools = []
