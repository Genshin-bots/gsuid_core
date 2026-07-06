"""v2 · Agent Mesh Kanban · 任务树并发调度执行器。

设计稿见 docs/AGENT_MESH_COLLABORATION_PROPOSAL_20260521.md §6。

调度循环：``execute_ready_tasks(root_task_id)`` 扫描整棵任务树，把全部
"依赖已满足 / 时间已到 / 未越权" 的子任务并发派给 ``run_capability_agent``，
执行结果落 artifact、写状态、刷新根任务汇总状态。

并发安全：
- ``mark_subtask_running`` 用条件 SQL（WHERE status='pending'）防止两个调度回合
  同时把同一子任务派出去；
- 每个子任务还套一个 ``asyncio.Lock``（``get_task_node_lock``），防止主人格连续
  触发两次 ``execute_ready_tasks`` 时同一子任务被并发拉起；
- 根任务 status 由 ``refresh_root_status`` 在每次调度回合结束后汇总刷新。

失败处理：默认 ``notify_persona`` 策略——子任务失败时不级联整树失败，而是用人格
口吻把 ``failure_reason`` 转告主人格，由主人格调 ``respawn_subtask`` /
``fail_task_tree`` 二选一；超过重派上限会自动挂为 ``waiting_approval``，由主人通过
webconsole 或对话回复审批（``respond_subtask_approval``）。
"""

import re
import time
import asyncio
from typing import List, Tuple, Optional

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.proactive import emit_proactive_message

from . import kanban
from .models import AIAgentTask, AIAgentTaskLog, AIAgentArtifact
from .runtime import PlanRunContext, bind_plan_context, reset_plan_context
from .workspace import put_artifact, ensure_workspace

_VALID_USER_TYPES = ("group", "direct", "channel", "sub_channel")

# 围栏代码块匹配（含语言标注的 ```python ... ```）。用于在"转译兜底"时剥离能力代理
# 原始产出里的大段代码 / 原始数据——它们绝不该直接回灌给用户（群聊刷屏与污染）。
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _sanitize_for_user(text: str) -> str:
    """剥离面向用户文本里的围栏代码块并限长，用于转译为空 / 转译异常时的兜底返回。

    设计原则：宁可丢失原始细节，也绝不把能力代理（plugin_developer_agent /
    code_agent 等）产出的代码 / 原始数据当作播报正文直接发给用户。正常情况下人格
    转译已遵循"只点结论、不复述细节"，本函数只兜底那条 `or raw_result` 退路。
    """
    if not text:
        return text
    sanitized = _CODE_FENCE_RE.sub("〔代码已省略〕", text)
    # 半个未配对的围栏：从首个 ``` 处截断，杜绝半截代码块漏出
    if "```" in sanitized:
        sanitized = sanitized.split("```", 1)[0].rstrip() + " 〔代码已省略〕"
    return sanitized.strip()[:600]


def _build_event(task: AIAgentTask) -> Event:
    user_type = task.user_type if task.user_type in _VALID_USER_TYPES else "direct"
    return Event(
        bot_id=task.bot_id,
        user_id=task.owner_user_id,
        bot_self_id=task.bot_self_id,
        user_type=user_type,
        group_id=task.group_id,
        real_bot_id=task.bot_id,
        msg_id="",
        # 还原派活时的权限等级，否则退回 Event 默认 6（非管理员），pm 门控工具
        # （check_pm，如 plugin_dev 全家）会拒绝主人本人派出的子代理。
        user_pm=task.user_pm,
    )


def _get_bot(task: AIAgentTask, ev: Event) -> Optional[Bot]:
    from gsuid_core.gss import gss

    if task.WS_BOT_ID and task.WS_BOT_ID in gss.active_bot:
        return Bot(gss.active_bot[task.WS_BOT_ID], ev)
    for bot_id in gss.active_bot:
        return Bot(gss.active_bot[bot_id], ev)
    return None


def _format_subtask_prompt(
    root: AIAgentTask,
    child: AIAgentTask,
    upstream_artifacts: List[AIAgentArtifact],
    resume_hint: str = "",
) -> str:
    """拼装喂给能力代理的任务文本（含上游 artifact + 工作区约束）。

    ``root == child`` 时表示叶子根（``create_subagent`` 创建的单步自执行任务），
    省略冗余的"任务树根目标"行——根目标就是任务本身。
    """
    import json as _json

    is_leaf_root = root.id == child.id
    parts: List[str]
    if is_leaf_root:
        parts = [
            "【Kanban 单步任务】你是被任务树调度器派来的专职执行体，请独立完成本任务。",
            f"任务描述：{child.goal[:1500]}",
            f"分配画像：{child.agent_profile or '（未指定）'}",
        ]
    else:
        parts = [
            "【Kanban 子任务】你是被任务树调度器派来的专职执行体，请独立完成本节点。",
            f"任务树根目标：{root.goal[:500]}",
            f"本子任务描述：{child.goal[:1000]}",
            f"分配画像：{child.agent_profile or '（未指定）'}",
        ]
    # 断点续作提示：审批挂起→批准→重新调度后，能力代理 history 为空、会从头重做；
    # 这段提示放在任务描述紧后面（高显著位），让它直接接着上一轮的断点往下做。
    if resume_hint:
        parts.append(resume_hint)
    if child.params_override:
        # JSON 而非 Python repr——避免 dict 渲染成 {'k': 'v'} 让 LLM 误以为是
        # Python 字面量；JSON 格式更接近代理实际要往 record_put / state_set 里塞
        # 的字符串内容，可直接抄写。
        try:
            params_json = _json.dumps(child.params_override, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            params_json = str(child.params_override)
        parts.append(f"【参数补充（JSON）】\n```json\n{params_json}\n```")
    if upstream_artifacts:
        parts.append("【上游产出（请阅读后再开始执行）】")
        for art in upstream_artifacts[:8]:
            preview = art.payload_inline or art.summary or ""
            preview = preview[:600]
            parts.append(f"- [{art.id}] kind={art.artifact_kind} from={art.from_profile or '-'} | {art.summary[:120]}")
            if preview:
                parts.append(f"  preview: {preview}")
    parts.append(
        "【交付要求】"
        "\n- 真实文件落地产物（PNG / PDF / CSV / 二进制等）必须用"
        ' `artifact_put(file_path="workspace 内文件名", summary=...)` 登记；'
        '**不要**用 `artifact_put(payload=\'{"file": "..."}\')` 这种 JSON 元数据冒充文件——'
        "主人格之后 `send_message_by_ai(image_id=res_xxx)` 时只有真实文件 artifact "
        "才能被发出去。"
        '\n- 纯文本结论 / 报告正文：`artifact_put(payload="...", summary="...")`。'
        "\n- 持久化业务数据（账户、持仓、流水、签到名单等）：用 `record_put` / "
        "`record_append` / `record_update` 写入框架统一的 `record:<集合名>` 集合，"
        "**不要**只塞进 state_set 大 JSON 块或自己写文件——其它子任务读不到。"
        "\n- 最后简短返回结论（一段话即可），剩下的让主人格自己转译。"
        "\n- 若本轮确实没有值得向主人播报的新进展（如决策全为观望/无变化），请在返回"
        f"结论开头单独一行写 {KANBAN_NO_BROADCAST_MARK}——任务照常完成归档，但不推群打扰。"
    )
    parts.append(
        "【工作区】你的唯一可写目录是框架绑定的 Artifact Workspace，禁止写入项目根目录、系统临时目录或其它任务目录。"
    )
    return "\n".join(parts)


async def _build_resume_hint(child: AIAgentTask) -> str:
    """重新调度时给能力代理的「断点续作」提示（目前仅插件开发的多步安装流程需要）。

    审批挂起 → 批准 → 重新调度后，能力代理的对话 history 为空、会从头重做（重读指南 /
    重新 scaffold）。这里按画像读对应的进度账本，给一段明确的续作指引，让它直接接着
    copy/load/test，而不是把整套流程重跑一遍。非该类画像或无进行中流程时返回空串。
    """
    if child.agent_profile != "plugin_developer_agent":
        return ""
    try:
        from gsuid_core.ai_core.buildin_tools.plugin_developer import (
            install_resume_hint_for_task,
        )

        return await install_resume_hint_for_task(child.id)
    except Exception as e:
        logger.debug(f"📋 [Kanban] 构造断点续作提示失败: {e}")
        return ""


async def _collect_upstream_artifacts(child: AIAgentTask) -> List[AIAgentArtifact]:
    """汇总上游子任务的 output_artifact + 全量 workspace_file 列表。"""
    deps = child.dependency_task_ids if isinstance(child.dependency_task_ids, list) else []
    if not deps:
        return []
    bag: List[AIAgentArtifact] = []
    seen: set = set()
    for dep_id in deps:
        rows = await AIAgentArtifact.list_for_task(dep_id)
        for r in rows:
            if r.id not in seen:
                bag.append(r)
                seen.add(r.id)
    return bag


async def _persona_relay(
    task: AIAgentTask, raw_result: str, is_approval_request: bool = False
) -> Tuple[str, List[str]]:
    """人格转译：能力代理结果再过一遍主人格口吻。

    ``is_approval_request=True`` 时按"请求主人审批"口吻转译（请主人回复同意/拒绝），
    而非"任务已完成"的进展播报。

    把本子任务登记的 ``workspace_file`` / ``output`` artifact 显式列在转译 prompt
    里——否则主人格转译时看不到 ``res_xxx`` 句柄，主人事后追问"刚才那张图呢"
    会无法发出去。同时给转译 Agent 注入 ``send_message_by_ai`` 工具——它现在
    支持 ``image_id="res_xxx"`` 自动从 Kanban artifact 读 payload、自动转 RM 发送，
    所以单个工具就能覆盖文本 / 图片两种产物（详见
    ``buildin_tools/message_sender.py``）。

    **优先发送二进制文件 artifact**：本函数会把 artifact 按"图片落盘文件 → 其它落盘
    文件 → 纯文本 inline"三档排序，并把"推荐发送"句柄单独高亮，避免转译 agent 在
    多份 artifact 里随机挑一个 inline 文本 artifact 发出去（实测 ``love_heart`` 会话
    曾出现 code_agent 自己又叠了一份 HTML 模板预览图、转译 agent 发错那张的问题）。

    转译 Agent 启用 SubAgent 日志（``is_subagent=True``）。早期为了避免 60+KB 噪声
    曾经禁用过这个日志，但归一到 ``emit_proactive_message`` 后转译日志会作为
    ``generator_log_files`` 挂到主 session 的 ``linked_agents`` 上——事后审计
    "为什么转译时是这种口吻"必须有日志才能复盘。

    返回 ``(转译后文本, 转译 SubAgent 日志路径列表)``。
    """
    if not task.persona_name:
        return raw_result, []

    from gsuid_core.ai_core.persona import build_persona_prompt
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent, create_agent
    from gsuid_core.ai_core.register import get_all_tools
    from gsuid_core.ai_core.session_logger import AISessionLogger

    relay_log_files: List[str] = []
    agent: Optional[GsCoreAIAgent] = None
    relay_logger: Optional[AISessionLogger] = None

    try:
        arts = await AIAgentArtifact.list_for_task(task.id)
        artifact_block: str = ""
        if arts:
            # 排序：图片落盘 > 其它落盘 > 纯 inline 文本；同档按时间倒序（最新先）
            def _priority(a: AIAgentArtifact) -> int:
                if a.payload_path and a.mime.startswith("image/"):
                    return 0
                if a.payload_path:
                    return 1
                return 2

            sorted_arts = sorted(arts, key=lambda a: (_priority(a), -a.created_at.timestamp()))
            recommended: Optional[AIAgentArtifact] = next(
                (a for a in sorted_arts if a.payload_path and a.mime.startswith("image/")),
                None,
            ) or next((a for a in sorted_arts if a.payload_path), None)

            lines: List[str] = []
            for a in sorted_arts[:8]:
                payload_hint = f" path={a.payload_path}" if a.payload_path else " (inline 文本)"
                star = " ⭐" if recommended is not None and a.id == recommended.id else ""
                lines.append(
                    f"- {a.id} | kind={a.artifact_kind} | mime={a.mime}{payload_hint} | {a.summary[:80]}{star}"
                )
            hint: str = ""
            if recommended is not None:
                hint = (
                    f"\n⭐ 推荐发送：`{recommended.id}`（{recommended.mime}，"
                    f"主人最可能想要的真实产物文件）。"
                    "**只发推荐句柄、不要把 inline 文本 artifact 当图片发**——"
                    "inline 文本 artifact 没有 payload_path，发送会失败。"
                )
            artifact_block = (
                "\n\n【本子任务登记的 artifact 句柄（如有图片 / 文件，"
                '请直接用 send_message_by_ai(image_id="res_xxx") 把产物发给主人——'
                "框架会自动从 Kanban artifact 读 payload 并转 RM 发送）】\n" + "\n".join(lines) + hint
            )

        base: str = await build_persona_prompt(task.persona_name)
        # 启用 SubAgent 日志：转译过程要进 generator_log_files，由 emitter
        # 挂到主 session 的 linked_agents 上做事后审计。
        relay_session_id: str = f"kanban_relay_{task.id[:8]}_{int(time.time())}"
        agent = create_agent(
            system_prompt=base,
            create_by="Kanban_Relay",
            persona_name=task.persona_name,
            task_level="low",
            session_id=relay_session_id,
            is_subagent=True,
        )
        relay_logger = agent._session_logger

        # 给转译 agent 准备最小工具池：只装 send_message_by_ai（已统一支持
        # img_xxx / res_xxx / http / base64 多种来源，无需额外的 send_original_pic）
        relay_tools = []
        all_tools = get_all_tools()
        if "send_message_by_ai" in all_tools:
            relay_tools.append(all_tools["send_message_by_ai"].tool)

        ev = _build_event(task)
        if is_approval_request:
            instruction = (
                f"【Kanban 审批请求转译】你的专职助手开发好了「{task.display_name}」，但装进框架需要主人点头。"
                "请用你自己的口吻简短转告主人这件事，并**明确请主人回复同意或拒绝**——不要复述代码/细节，也不要替主人做决定。"
            )
        else:
            instruction = (
                f"【Kanban 子任务播报转译】你的专职助手刚完成了「{task.display_name}」，执行结果如下。"
                "请用你自己的口吻、简短地把这条进展转告主人——只点关键结论与下一步动作，"
                "**不要复述细节、不要把自己当作做出该决定的人**。"
                "**严禁原样输出任何代码块、文件内容或大段原始数据**（会造成群聊刷屏）；"
                "如助手产出了代码 / 文件，只需一句话说明做了什么、放在哪，不要把代码贴出来。"
            )
        spoken: str = await agent.run(
            user_message=f"{instruction}\n---\n{raw_result[:1500]}" + artifact_block,
            ev=ev,
            bot=_get_bot(task, ev),
            tools=relay_tools,
            return_mode="return",
        )
        # 人格转译正常产出直接用；仅"转译为空"的兜底退路对 raw_result 做去代码处理，
        # 避免把能力代理的原始代码 / 数据当播报正文发给用户。
        return spoken.strip() or _sanitize_for_user(raw_result), relay_log_files
    except Exception as e:
        logger.debug(f"📋 [Kanban] 人格转译失败，去代码兜底播报: {e}")
        return _sanitize_for_user(raw_result), relay_log_files
    finally:
        # 无论成功 / 异常，关闭转译 SubAgent logger；relay_log_files 在
        # return 表达式求值后才被 append（list 是引用，append 对返回值同样可见）。
        if relay_logger is not None:
            relay_log_files.append(str(relay_logger._file_path))
            relay_logger.close()


async def _notify(
    task: AIAgentTask,
    message: str,
    trigger_reason: str,
    generator_log_files: Optional[List[str]] = None,
) -> None:
    """通过统一主动消息出口把转译 / 失败播报送给主人。

    替代旧 ``bot.send`` 直发的写法——经过 ``emit_proactive_message`` 后会自动：
    1. 在主用户 session 的 pydantic_ai history 里追加一条 assistant-only turn；
    2. 在主用户 session_logger 中写一条 ``proactive_emission``；
    3. 走 C8 网关（``source="kanban"`` 不被抑制，避免误杀关键播报）；
    4. message_history 单次落库且 metadata 含 ``proactive_source=kanban``。
    """
    ev = _build_event(task)
    sent = await emit_proactive_message(
        event=ev,
        message=message,
        source="kanban",
        trigger_reason=trigger_reason,
        generator_log_files=generator_log_files or [],
        suppress_when_heartbeat_recent=False,
    )
    if not sent:
        logger.warning(f"📋 [Kanban] 任务 root=#{task.ordinal} 主动消息发送失败 / 被抑制")


# ============================================================
# 子任务播报静默信号
# ============================================================
# 能力代理在最终输出里以本标记单独成段/作行首前缀，声明"本轮没有值得播报的
# 进展"——框架据此完成+归档但不推群（如模拟盘全 hold 不吭声，真买卖才冒泡）。
KANBAN_NO_BROADCAST_MARK = "<<NO_BROADCAST>>"
# 只认行首位置（大小写不敏感）：正文中途提及该字面串不触发静默、也不被剥离
_NO_BROADCAST_PATTERN = re.compile(
    rf"^[ \t]*{re.escape(KANBAN_NO_BROADCAST_MARK)}[ \t]*",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_no_broadcast(raw: str) -> Tuple[str, bool]:
    """剥离 ``<<NO_BROADCAST>>`` 静默标记，返回 ``(去标记后的文本, 是否静默)``。

    大小写不敏感，但只认**行首**位置的标记（单独成段或作某行前缀）——避免正文
    中途引用该字面串的合法产出（如解释本静默机制的任务）被误判静默、误洗归档文本。
    命中后子任务照常完成，去标记后的文本仍作 artifact 归档（剥离后为空时由调用方
    写占位说明留档），只是不走 relay/notify 推群。
    """
    if not raw or KANBAN_NO_BROADCAST_MARK.lower() not in raw.lower():
        return raw, False
    stripped, n = _NO_BROADCAST_PATTERN.subn("", raw)
    if n == 0:
        return raw, False
    return stripped.strip(), True


async def _run_one_task_node(root: AIAgentTask, child: AIAgentTask) -> None:
    """派活单个子任务节点。"""
    lock = kanban.get_task_node_lock(child.id)
    if lock.locked():
        return
    async with lock:
        # 再读一次最新状态防 race
        fresh = await AIAgentTask.get_by_id(child.id)
        if fresh is None or fresh.status != "pending":
            return

        # 1) 条件 SQL 抢锁
        won = await kanban.mark_subtask_running(fresh)
        if not won:
            return
        fresh.status = "running"

        # 2) 绑定工作区 + 上下文
        workspace = ensure_workspace(root.id, fresh.id, agent_profile=fresh.agent_profile)
        plan_ctx = PlanRunContext(
            task_id=fresh.id,
            step_id=None,
            root_task_id=root.id,
            artifact_workspace=workspace,
            allowed_write_roots=[workspace],
            agent_profile=fresh.agent_profile,
        )
        token = bind_plan_context(plan_ctx)

        ev = _build_event(fresh)
        bot = _get_bot(fresh, ev)
        upstream = await _collect_upstream_artifacts(fresh)
        resume_hint = await _build_resume_hint(fresh)
        prompt = _format_subtask_prompt(root, fresh, upstream, resume_hint)

        # 3) 让能力代理执行
        raw_result: str = ""
        try:
            from gsuid_core.ai_core.capability_agents.runner import run_capability_agent

            profile_id = fresh.agent_profile or "research_agent"
            raw_result = await run_capability_agent(
                profile_id=profile_id,
                task=prompt,
                ev=ev,
                bot=bot,
                session_id_suffix=f"kanban_{root.id[:6]}_{fresh.id[:6]}",
            )
        except Exception as e:
            logger.exception(f"📋 [Kanban] 子任务执行抛出异常: {e}")
            await kanban.mark_subtask_failed(fresh, f"{type(e).__name__}: {e}")
            await _notify_failure(root, fresh, str(e))
            return
        finally:
            reset_plan_context(token)

        # 3.5) 剥离静默标记：能力代理声明"本轮无值得播报"时，完成+归档但不推群
        raw_result, no_broadcast = _strip_no_broadcast(raw_result)
        if no_broadcast and not raw_result:
            # 纯标记输出也要留档，下游依赖本节点 artifact 时不至于拿到空上游
            raw_result = "（本轮无值得播报的进展）"

        # 4) 没产出 artifact 时用 raw_result 兜底写一份 text
        latest = await AIAgentTask.get_by_id(fresh.id)
        output_id = latest.output_artifact_id if latest and latest.output_artifact_id else ""
        if not output_id and raw_result:
            art = await put_artifact(
                payload=raw_result[:12000],
                summary=f"子任务自动留档：{fresh.display_name}"[:512],
                mime="text/plain",
                artifact_kind="output",
                plan_ctx=plan_ctx,
            )
            if art is not None:
                output_id = art.id

        # 5) 落终态
        from gsuid_core.ai_core.capability_agents.runner import (
            CAPABILITY_AGENT_ERROR_PREFIX,
        )

        # 5a) 安装审批：copy_to_plugin_dir 已把任务挂为 waiting_approval，转译审批请求且不落终态
        if latest is not None and latest.status == "waiting_approval":
            if bot:
                spoken, relay_log_files = await _persona_relay(
                    fresh, latest.failure_reason or raw_result, is_approval_request=True
                )
                if spoken:
                    await _notify(
                        fresh,
                        spoken,
                        trigger_reason=f"approval_request:{fresh.display_name}",
                        generator_log_files=relay_log_files,
                    )
        elif (raw_result or "").startswith(CAPABILITY_AGENT_ERROR_PREFIX):
            await kanban.mark_subtask_failed(fresh, raw_result[:1000])
            await _notify_failure(root, fresh, raw_result[:1000])
        else:
            await kanban.mark_subtask_completed(fresh, output_artifact_id=output_id)
            if bot and raw_result and not no_broadcast:
                spoken, relay_log_files = await _persona_relay(fresh, raw_result)
                if spoken:
                    await _notify(
                        fresh,
                        spoken,
                        trigger_reason=f"subtask={fresh.display_name}",
                        generator_log_files=relay_log_files,
                    )
            elif bot and no_broadcast:
                logger.debug(
                    f"📋 [Kanban] 子任务 {fresh.display_name} 声明静默（{KANBAN_NO_BROADCAST_MARK}），跳过推群"
                )


async def _notify_failure(root: AIAgentTask, child: AIAgentTask, reason: str) -> None:
    """子任务失败时按 failure_policy 通知主人格。默认 notify_persona。

    §8.1 改造：失败播报同样走 ``emit_proactive_message``——否则主 session 不知道
    任务失败发生过，用户追问"刚那条警告是啥意思"时主人格会失忆。
    """
    policy = root.failure_policy or "notify_persona"
    if policy == "auto_abort":
        await kanban.fail_task_tree(root.id, f"子任务 {child.display_name} 失败：{reason[:200]}")
        await _notify(
            child,
            f"⚠️ 任务「{root.display_name}」整树终止：{reason[:200]}",
            trigger_reason=f"failure_abort:{child.display_name}",
        )
        return
    # notify_persona：把失败原因转告人格，让主人格走 respawn / fail 决策
    spoken = (
        f"⚠️ 子任务「{child.display_name}」失败：{reason[:300]}\n"
        f"请用 respawn_subtask 修参数重派（达上限会自动转 waiting_approval）；"
        f"或 fail_task_tree 终结整树。"
    )
    await _notify(
        child,
        spoken,
        trigger_reason=f"failure:{child.display_name}",
    )


async def execute_ready_tasks(root_task_id: str) -> None:
    """Kanban 调度核心：扫描任务树，把所有可跑的子任务并发派活。

    三种调度形态：
    - **多步任务树**：根任务聚合 + N 子任务，按依赖 / not_before 并发派活；
    - **叶子根（单步自执行）**：根任务自身带 ``agent_profile`` 且无子任务，
      此时直接把根任务作为单一可执行节点派出，跳过子任务循环。这是
      ``create_subagent(agent_profile=...)`` 的承载形态，避免冗余的"根 + 1 子任务"
      双节点结构（实测会话 e05e495b 主人投诉点）。
    - **周期模板根**（``recurring_status='armed'`` 且 ``recurring_trigger`` 非空）：
      永远不在此处直接执行——它们只是被克隆的样板，由 ``recurring._fire_template``
      到点克隆出实例树后再走本函数推进实例。
    """
    root, children = await kanban.get_task_tree(root_task_id)
    if root is None:
        return
    if root.status in ("completed", "failed", "cancelled"):
        return
    if root.recurring_trigger and root.recurring_status == "armed":
        logger.debug(f"📋 [Kanban] 跳过模板根的直接调度 root={root_task_id}（应由 _fire_template 克隆实例后再调度）")
        return

    # 叶子根：直接把 root 当作单一执行节点派出
    if kanban.is_leaf_root(root, len(children)):
        if root.status == "pending":
            logger.info(f"📋 [Kanban] 调度叶子根 root={root_task_id} profile={root.agent_profile}")
            await _run_one_task_node(root, root)
        # 叶子根状态由 _run_one_task_node 自己写完，不需要 refresh_root_status
        return

    # 先把"依赖已满足、可以 arm"的周期子任务模板挂到 APScheduler——
    # 这一步对一棵新树第一次 kick 时把 init 子任务派出去后立刻生效，
    # arm 完成后周期子任务**不**进 ready 队列（由 get_ready_child_tasks 排除），
    # 后续 fire 由 APScheduler 触发 → clone 一个新的执行实例子任务进入 ready。
    await _maybe_arm_recurring_subtasks(root, children)

    ready = kanban.get_ready_child_tasks(children, root_status=root.status)
    if not ready:
        await kanban.refresh_root_status(root_task_id)
        return

    logger.info(f"📋 [Kanban] 调度回合 root={root_task_id} 可跑子任务 {len(ready)} 个")
    runners = [_run_one_task_node(root, c) for c in ready]
    await asyncio.gather(*runners, return_exceptions=True)
    await kanban.refresh_root_status(root_task_id)

    # 若仍有 pending 且依赖刚刚解锁，递归再跑一轮——但限制最多 4 层避免死循环
    await _schedule_continuation(root_task_id, depth=0)


async def _maybe_arm_recurring_subtasks(
    root: AIAgentTask,
    children: List[AIAgentTask],
) -> None:
    """对一棵任务树里所有"依赖已满足、待 arm"的周期子任务模板做一次 arm。

    arm 等于"把模板挂到 APScheduler + 数据库写 recurring_status='armed'"。arm 失败
    时模板自动转 disarmed，避免阻塞下游。本函数在 ``execute_ready_tasks`` 入口
    调用，效果是：
    - 新树第一次 kick：init 子任务派出 + 周期子任务 arm 等到点 fire；
    - 上游 init 完成后再 kick：刚解锁依赖的周期子任务跟着 arm；
    - 重复 kick：已 armed 的模板被 ``get_pending_recurring_templates_ready_to_arm``
      过滤（要求 recurring_status 为空），所以幂等。
    """
    ready_templates = kanban.get_pending_recurring_templates_ready_to_arm(children, root_status=root.status)
    for tpl in ready_templates:
        try:
            ok, msg = await kanban.arm_recurring_subtask(tpl, tpl.recurring_trigger or "")
            if not ok:
                logger.warning(f"📋 [Kanban] 周期子任务 arm 失败 subtask={tpl.id} root={root.id}: {msg}")
        except Exception as e:
            logger.exception(f"📋 [Kanban] 周期子任务 arm 抛出异常 subtask={tpl.id} root={root.id}: {e}")


async def _schedule_continuation(root_task_id: str, depth: int) -> None:
    if depth >= 4:
        return
    root, children = await kanban.get_task_tree(root_task_id)
    if root is None or root.status in ("completed", "failed", "cancelled"):
        return
    # 上游子任务刚完成可能解锁周期子任务的依赖 → 再 arm 一遍
    await _maybe_arm_recurring_subtasks(root, children)
    new_ready = kanban.get_ready_child_tasks(children, root_status=root.status)
    if not new_ready:
        return
    runners = [_run_one_task_node(root, c) for c in new_ready]
    await asyncio.gather(*runners, return_exceptions=True)
    await kanban.refresh_root_status(root_task_id)
    await _schedule_continuation(root_task_id, depth=depth + 1)


async def kick_root(root_task_id: str) -> None:
    """立即触发一次调度（创建 / 恢复 / 重派后调用）。"""
    try:
        await execute_ready_tasks(root_task_id)
    except Exception as e:
        logger.exception(f"📋 [Kanban] kick_root 异常: {e}")
        await AIAgentTaskLog.add_log(root_task_id, "decision", f"调度异常：{type(e).__name__}: {e}")
