"""阶段 D：收尾（history/闸门/假完成）+ UsageLimit 兜底 + finally 清理"""

from __future__ import annotations

import time
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai.messages import (
    ModelRequest,
)
from pydantic_ai.settings import ModelSettings

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core import (
    wall_clock,
    output_gate,
    output_firewall,
)
from gsuid_core.ai_core.const import (
    _STICKY_FAMILY_TURNS,
    _PROGRESSIVE_TOOLS_SKIP_INTENTS,
)
from gsuid_core.ai_core.utils import (
    NO_RESULT_TEXT,
    SILENCE_MARKERS,
    send_chat_result,
    _relean_user_turn,
    _extract_run_context,
    _compact_report_blocks_in_history,
    _truncate_tool_returns_in_history,
)
from gsuid_core.ai_core.register import find_tool_base
from gsuid_core.ai_core.agent_run.host import RunOnceHost
from gsuid_core.ai_core.agent_run.state import (
    RunOnceState,
    _require_limits,
    _require_context,
)
from gsuid_core.ai_core.agent_run.support import (
    _FAKE_DONE_NUDGE,
    _WALL_CLOCK_NUDGE,
    _RENDER_TOOL_NAMES,
    _REPORT_SPEECH_NUDGE,
    _INTERACTIVE_CREATE_BY,
    _RENDER_DELEGATE_NUDGE,
    _STATUS_ZERO_TOOL_NUDGE,
    _STRUCTURAL_ZERO_TOOL_NUDGE,
    _claims_fake_done,
    _looks_like_report_speech,
)
from gsuid_core.ai_core.agent_run.budget_ctx import _current_budget_scope
from gsuid_core.ai_core.agent_run.speech_policy import (
    looks_like_empty_handoff,
    strip_open_solicitations,
    claims_premature_delivery,
    has_orchestration_narration,
)


class SettlePhase(RunOnceHost):
    async def _run_once_settle_result(
        self,
        st: RunOnceState,
        agent_run: Any,
        statistics_manager: Any,
    ) -> Any:
        """iter 成功路径：history / 闸门 / token / 假完成 / OOC / return。"""
        # 遍历完成后，直接从 agent_run 中获取最终结果
        result = agent_run.result
        if result:
            logger.info(i18n_t("log.agent.iter_ok"))

            # 存 history 前把本轮 user turn 的 content 换成精简版（剥离 st.rag_context）
            # 防止 [历史对话]/记忆/群语境快照逐轮累积膨胀 input 并冲淡缓存（§优化 O-1）。
            _new_msgs = result.new_messages()
            _relean_user_turn(
                _new_msgs,
                st.lean_user_message,
                strip_hint_texts=(_WALL_CLOCK_NUDGE, *output_gate.GATE_NUDGE_MARKERS),
            )
            # 框架注入 drop 后可能留下空 ModelRequest，禁止进 B 轨
            _new_msgs = [m for m in _new_msgs if not (isinstance(m, ModelRequest) and len(m.parts) == 0)]
            # 超长工具返回截断为头+尾摘要（§25(5)）：本轮已消费完整返回，历史无需原文
            _truncate_tool_returns_in_history(_new_msgs)
            self.history.extend(_new_msgs)

            # 输出闸门收尾：尖括号熔断/补写/scrub；熔断后仍做独立 OOC 重说
            st.ab_abort = await self._resolve_output_gate_after_run(
                _require_context(st),
                st.bot,
                st.ev,
                return_mode=st.return_mode,
                ooc_blocked=st.ooc_blocked,
                ab_abort=st.ab_abort or output_gate.is_fused(_require_context(st).extra),
            )

            # L3：记录本轮实际调用过的工具所属能力族，使其在随后数轮继续常驻，
            if st.tool_call_list:
                for _tname in set(st.tool_call_list):
                    _tb = find_tool_base(_tname)
                    _dom = _tb.capability_domain if _tb else None
                    if _dom:
                        self._recent_tool_families[_dom] = _STICKY_FAMILY_TURNS

            # 更新连续无工具调用计数（仅对交互式主 Agent 生效）。闲聊类意图不计数（§15）
            # 豁免口径与注入门同源：_PROGRESSIVE_TOOLS_SKIP_INTENTS（评审修复 E12）。
            if self.create_by in ["Chat", "Agent"] and st.intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS:
                if st.tool_call_list:
                    self._consecutive_no_tool_rounds = 0
                else:
                    self._consecutive_no_tool_rounds += 1
                    # 意图-行为不一致检测（结构化）：thinking 里提到了本轮
                    # 已装配的工具名却没真正调用——顶到阈值，下轮强制提醒。
                    thinking_blob = "\n".join(st.thinking_segments)
                    if thinking_blob and st.tool_names and any(tn in thinking_blob for tn in st.tool_names):
                        self._consecutive_no_tool_rounds = max(self._consecutive_no_tool_rounds, 2)
                        logger.debug(i18n_t("log.agent.intent_action_mismatch_force"))

            # 记录 Token 使用量和延迟统计
            # 记录响应延迟
            latency = time.time() - st.start_time
            statistics_manager.record_latency(latency=latency)

            try:
                # v2: result.usage / result.timestamp 由方法改为属性
                usage_obj: RunUsage = result.usage
                input_tokens: int = usage_obj.input_tokens
                output_tokens: int = usage_obj.output_tokens
                cache_read_tokens: int = usage_obj.cache_read_tokens
                cache_write_tokens: int = usage_obj.cache_write_tokens

                logger.info(
                    i18n_t(
                        "log.agent.token_usage_input_tokens",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        cache_write_tokens=cache_write_tokens,
                    )
                )

                # 小时级性能统计（TTFT/TPS）已在每轮 CallToolsNode 中按请求结算,
                # 此处只记录 run 级的 Token 汇总
                if input_tokens > 0 or output_tokens > 0:
                    statistics_manager.record_token_usage(
                        model_name=st.model_name,
                        chat_type=self.create_by,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        cache_write_tokens=cache_write_tokens,
                    )
                    # 预算记账：可归属 scope 的 run 计入对应 Session 额度，无 scope 只进全局
                    # 统计。独立 try 且先于 session 日志，避免日志抛错把整笔记账一起跳过。
                    if st.budget_scope is not None:
                        try:
                            from gsuid_core.ai_core.budget import budget_manager

                            await budget_manager.record_usage_scope(
                                st.budget_scope[0],
                                st.budget_scope[1],
                                st.budget_scope[2],
                                self.session_id,
                                input_tokens,
                                output_tokens,
                                cache_read_tokens,
                                cache_write_tokens,
                            )
                        except Exception as _be:
                            logger.warning(i18n_t("log.agent.budget_fail", _be=_be))
                    try:
                        self._session_logger.log_token_usage(
                            input_tokens,
                            output_tokens,
                            st.model_name,
                            cache_read_tokens,
                            cache_write_tokens,
                        )
                    except Exception as _le:
                        logger.debug(i18n_t("log.agent.write_token_usage_log", _le=_le))
            except AttributeError as e:
                # result 没有 usage 属性（如 pydantic_graph End 节点返回的结果）
                logger.info(i18n_t("log.agent.access_result_usage", e=e))
                pass
            except TypeError as e:
                # v1 旧写法 result.usage() 在 v2 抛 'RunUsage' is not callable
                logger.info(i18n_t("log.agent.result_usage_call_style", e=e))
                pass
            except Exception as e:
                logger.warning(i18n_t("log.agent.record_statistics", e=e))

            # 当 return_model 指定时，直接返回 Pydantic 模型实例
            if st.output_type is not None:
                self._session_logger.log_run_end()
                self._session_logger.log_result(result.output, st.tool_call_list)
                return result.output

            # 始终返回字符串类型
            result_msg = str(result.output).strip()
            # 工具调用列表只进调试日志，不追加到用户可见消息
            if st.tool_call_list:
                logger.debug(i18n_t("log.agent.current_tool_call_event", p0=", ".join(st.tool_call_list)))

            self._session_logger.log_run_end()
            self._session_logger.log_result(result_msg, st.tool_call_list)

            # 假完成结算（结构判据收口）。
            async def _resend_fab_blocked() -> None:
                for _bt in st.fab_blocked:
                    if _bt in self._run_sent_texts:
                        continue
                    try:
                        if st.bot is None:
                            logger.warning(i18n_t("log.agent.fakedone_bot_object_unavailable"))
                            continue
                        await send_chat_result(st.bot, _bt, ev=st.ev)
                        self._run_sent_texts.add(_bt)
                    except Exception as _se:
                        logger.debug(i18n_t("log.agent.fakedone_se", _se=_se))

            if st.fab_blocked and st.tool_call_list and st.bot and st.return_mode in ["always", "by_bot"]:
                logger.info(i18n_t("log.agent.fakedone_claim"))
                await _resend_fab_blocked()
            elif (
                result_msg
                and not st.tool_call_list
                and st.tool_names
                and not st.fake_done_retry
                # 结构证据：预检暂扣 or 文本宣称完成；不靠 st.intent 标签（误标会误伤闲聊）
                and (st.fab_blocked or _claims_fake_done(result_msg))
            ):
                logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
                try:
                    corrected = await self._execute_run_once(
                        user_message=_FAKE_DONE_NUDGE,
                        bot=st.bot,
                        ev=st.ev,
                        tools=st.tools,
                        return_mode=st.return_mode,
                        intent=st.intent,
                        has_active_task=st.has_active_task,
                        suppress_intermediate_text=st.suppress_intermediate_text,
                        fake_done_retry=True,
                    )
                except Exception as _fe:
                    # 纠正 pass 是增强路径，失败不影响原结果返回；暂扣文本补发防"整轮沉默"
                    logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_fe))
                    corrected = None
                    if st.fab_blocked and st.bot and st.return_mode in ["always", "by_bot"]:
                        await _resend_fab_blocked()
                if isinstance(corrected, str) and corrected.strip():
                    # 纠正成功：从持久历史剥掉 nudge user turn 与暂扣未发的编造声明 （用户从没见过它们，
                    _fabricated = {t.strip() for t in st.fab_blocked}
                    if _claims_fake_done(result_msg):
                        _fabricated.add(result_msg.strip())
                    result_msg = corrected.strip()
                    self._scrub_fake_done_history(_fabricated)

            # 结构假完成：被呼叫/省略续聊 + 池非空 + 零调用 + 非沉默 + 非极短寒暄（无用户话题词）
            # 闲聊 st.intent 不二次重跑，避免占满群聊应答配额
            elif (
                result_msg
                and not st.tool_call_list
                and st.tool_names
                and not st.fake_done_retry
                and self.create_by in _INTERACTIVE_CREATE_BY
                and self.create_by != "CapabilityAgent"
                and (st.intent or "") not in _PROGRESSIVE_TOOLS_SKIP_INTENTS
                and st.ev is not None
                and (
                    bool(st.ev.is_tome)
                    or bool(
                        st.tg is not None and (st.tg.call_to_self or st.tg.soft_continue or st.tg.ellipsis_followup)
                    )
                )
                and result_msg.strip() not in SILENCE_MARKERS
                and len(result_msg.strip()) > 12
            ):
                logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
                try:
                    corrected = await self._execute_run_once(
                        user_message=_STRUCTURAL_ZERO_TOOL_NUDGE,
                        bot=st.bot,
                        ev=st.ev,
                        tools=st.tools,
                        return_mode=st.return_mode,
                        intent=st.intent,
                        has_active_task=st.has_active_task,
                        suppress_intermediate_text=st.suppress_intermediate_text,
                        fake_done_retry=True,
                    )
                except Exception as _fe:
                    logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_fe))
                    corrected = None
                if isinstance(corrected, str) and corrected.strip():
                    _prior = result_msg.strip()
                    result_msg = corrected.strip()
                    if _prior:
                        self._scrub_fake_done_history({_prior})

            # 结构/事实包已返回却未委派 render，或本轮把报告体念成台词 → 纠正出图
            # 异步在途不再 nudge（等回灌或用户追问）
            elif (
                (
                    st.report_speech_blocked
                    or (
                        st.saw_structured_return
                        and st.tool_call_list
                        and result_msg
                        and (
                            len(result_msg.strip()) > 40
                            or _looks_like_report_speech(result_msg)
                            or st.report_speech_blocked
                        )
                    )
                )
                and not st.delegated_render
                and not st.pending_async_delivery
                and not (_RENDER_TOOL_NAMES & set(st.tool_call_list))
                and not st.fake_done_retry
                and self.create_by in _INTERACTIVE_CREATE_BY
                and self.create_by != "CapabilityAgent"
            ):
                logger.warning(i18n_t("log.agent.render_data_nudge_once"))
                _nudge_msg = (
                    _REPORT_SPEECH_NUDGE
                    if st.report_speech_blocked or _looks_like_report_speech(result_msg or "")
                    else _RENDER_DELEGATE_NUDGE
                )
                try:
                    _rc = await self._execute_run_once(
                        user_message=_nudge_msg,
                        bot=st.bot,
                        ev=st.ev,
                        tools=st.tools,
                        return_mode=st.return_mode,
                        intent=st.intent,
                        has_active_task=st.has_active_task,
                        suppress_intermediate_text=True,
                        fake_done_retry=True,
                    )
                except Exception as _re:
                    logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_re))
                    _rc = None
                if isinstance(_rc, str) and _rc.strip():
                    _rc_s = _rc.strip()
                    if _rc_s in SILENCE_MARKERS:
                        result_msg = _rc_s
                    elif (
                        has_orchestration_narration(_rc_s)
                        or claims_premature_delivery(_rc_s)
                        or _looks_like_report_speech(_rc_s)
                    ):
                        result_msg = "<SILENCE>"
                    else:
                        result_msg = strip_open_solicitations(_rc_s) or "<SILENCE>"
                elif st.report_speech_blocked:
                    # 纠正未产出可见文本：吞掉原报告体出口
                    result_msg = "<SILENCE>"

            # 进度追问却零工具：纠正重跑去查 kanban/artifact
            elif (
                st.status_inquiry
                and st.has_active_task
                and not st.tool_call_list
                and not st.fake_done_retry
                and result_msg
                and result_msg.strip() not in SILENCE_MARKERS
                and self.create_by in ("Chat", "Agent")
            ):
                logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
                try:
                    _sc = await self._execute_run_once(
                        user_message=_STATUS_ZERO_TOOL_NUDGE,
                        bot=st.bot,
                        ev=st.ev,
                        tools=st.tools,
                        return_mode=st.return_mode,
                        intent=st.intent,
                        has_active_task=st.has_active_task,
                        suppress_intermediate_text=st.suppress_intermediate_text,
                        fake_done_retry=True,
                    )
                except Exception as _se:
                    logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_se))
                    _sc = None
                if isinstance(_sc, str) and _sc.strip():
                    result_msg = _sc.strip()

            # 出口消毒：异步在途 / 编排泄漏 / 报告体 / 引导追问 → 对外 SILENCE 或短句
            if self.create_by in ("Chat", "Agent") and result_msg:
                _rs = result_msg.strip()
                if st.pending_async_delivery and _rs not in SILENCE_MARKERS:
                    result_msg = "<SILENCE>"
                elif has_orchestration_narration(_rs):
                    result_msg = "<SILENCE>"
                elif claims_premature_delivery(_rs) and not st.image_sent_this_run:
                    result_msg = "<SILENCE>"
                elif looks_like_empty_handoff(_rs) and not st.image_sent_this_run:
                    result_msg = "<SILENCE>"
                elif _looks_like_report_speech(_rs) and not st.image_sent_this_run:
                    result_msg = "<SILENCE>"
                else:
                    _stripped = strip_open_solicitations(_rs)
                    if _stripped != _rs:
                        result_msg = _stripped if _stripped else "<SILENCE>"

            # <report> 制品正文换占位符（§1 漂移固化）。
            _compact_report_blocks_in_history(_new_msgs, sent_texts=self._run_sent_texts)

            if st.return_mode in ["by_bot"] and st.bot and st.ev:
                return ""
            # 对用户可见出口才做 roleplay OOC；子代理/能力代理 return 必须保留 res_ 句柄
            if result_msg and output_firewall.is_enabled():
                _skip_roleplay_scrub = self.is_subagent or self.create_by in (
                    "CapabilityAgent",
                    "AutoPlanner",
                )
                if _skip_roleplay_scrub:
                    if output_firewall.is_tech_dump(result_msg):
                        result_msg = "⚠️ 子任务返回技术错误堆栈，已屏蔽。请主人格换路或重试（勿向用户念本句）。"
                else:
                    result_msg, _ooc_scrubbed = output_firewall.scrub_or_fallback(
                        result_msg,
                        user_text=st.ev.raw_text if st.ev is not None and st.ev.raw_text else "",
                    )
                    if _ooc_scrubbed:
                        logger.warning(i18n_t("log.agent.firewall_run_return_value_hit"))
            return result_msg

        # result 为空时的默认返回值（常量：handle_ai 好感度门等消费端按它识别准失败轮）
        return NO_RESULT_TEXT

    async def _run_once_usage_limit_fallback(
        self,
        st: RunOnceState,
        statistics_manager: Any,
    ) -> Any:
        """UsageLimitExceeded 专属兜底总结。"""
        # 达到限制后的处理逻辑
        logger.warning(i18n_t("log.agent.pydanticai_reached_maximum_thinking", p0=_require_limits(st).request_limit))
        statistics_manager.record_error(error_type="usage_limit")
        self._session_logger.log_error("usage_limit", f"达到最高思考轮数限制 {_require_limits(st).request_limit}")

        # 子代理（return 模式，如 Kanban 能力代理 / plugin_developer_agent）： **绝不**直接对用户的 st.bot 说话
        # 也**绝不**把超轮数的中间产物强制总结后回灌
        if st.return_mode == "return":
            return (
                "⚠️ 已达最大思考轮数，未能在限定步数内完成本任务。"
                "中间产物（如已写入的文件 / artifact）已留在工作区，未回传以避免刷屏。"
            )

        # 安抚用户
        if st.bot:
            await st.bot.send(await st.bot.t("log.ai_agent.chain_too_long_summary"))

        # ✨ 【关键点2】发起"强制总结"请求
        try:
            user_question = st.last_user_question or "用户之前提出的问题"

            # 从历史中提取已获取的事实和模型推理片段
            run_context = _extract_run_context(self.history)

            if run_context:
                final_message = (
                    f"【用户的问题】\n{user_question}\n\n"
                    f"【已获取的信息和推理过程】\n{run_context}\n\n"
                    "请根据以上已知信息，根据人设风格直接回答用户的问题。"
                    "禁止调用任何工具，只输出自然语言文本。"
                )
            else:
                final_message = (
                    f"【用户的问题】\n{user_question}\n\n"
                    "请直接回答这个问题（根据你的已有知识和角色性格），不要调用任何工具。"
                )

            # 创建无工具精简 Agent（tools=[] = 无 schema，从根源消除工具调用）
            from pydantic_ai.settings import ModelSettings

            _fb_settings: ModelSettings | None = None
            if self.max_tokens is not None:
                _fb_settings = ModelSettings(max_tokens=int(self.max_tokens))
            _fallback_agent = Agent(
                model=self.model,
                system_prompt=self.system_prompt or "你是一个智能助手。",
                model_settings=_fb_settings,
                tools=[],
                toolsets=[],
                retries=0,
                output_type=str,
            )

            # message_history 为空：所有上下文已聚焦到 final_message 中
            fallback_result = await _fallback_agent.run(
                final_message,
                message_history=[],
                usage_limits=UsageLimits(request_limit=1),
            )

            # 强制总结同样是一次真实 LLM 往返，把它的最终产出记进当前 session
            # logger（与本 run 同一文件）——否则"超轮数兜底"答复在日志里不可见。
            fallback_text = str(fallback_result.output)
            self._session_logger.log_text_output(fallback_text)
            self._session_logger.log_result(fallback_text, st.tool_call_list)

            if st.bot:
                await send_chat_result(st.bot, fallback_result.output, ev=st.ev)
            return ""

        except Exception as e:
            logger.error(i18n_t("log.agent.pydanticai_forced_summary", e=e))
            self._session_logger.log_error("fallback_failed", str(e))
            fallback_error = "⚠️ 问题较复杂，现有信息不足以给出准确答案。可以尝试提高思维链长度，或换个方式描述问题。"
            if st.bot:
                await st.bot.send(fallback_error)
                return ""
            return fallback_error

            # 瞬时故障（超时/网络/5xx/529 等）一律不在此捕获，向上抛给 _execute_run
            # 统一重试；download image 自愈与错误文案/统计也收敛到 _execute_run。

    def _run_once_cleanup(self, st: RunOnceState) -> None:
        """finally：还原 budget scope / 墙钟 / 单轮节流。"""
        # 还原预算 scope contextvar，避免本次绑定泄漏到上层调用栈。
        if st.budget_scope_token is not None:
            _current_budget_scope.reset(st.budget_scope_token)
        # 同理还原墙钟时钟：嵌套 run 结束后父 run 必须拿回自己的累加器。
        if st.wall_clock_token is not None:
            wall_clock.uninstall_clock(st.wall_clock_token)
        # 清理本轮的单轮节流计数（scheduler.py add_once_task 等共享）， 防止内存中 key 无限累积。
        try:
            from gsuid_core.ai_core.buildin_tools.scheduler import (
                clear_turn_throttle,
            )
            from gsuid_core.ai_core.buildin_tools.message_sender import (
                clear_turn_send_throttle,
            )

            sess = st.ev.session_id if st.ev is not None else None
            if sess:
                clear_turn_throttle(str(sess), st.turn_id)
                clear_turn_send_throttle(str(sess), st.turn_id)
        except Exception as _e:
            logger.debug(i18n_t("log.agent.clear_counter", _e=_e))
