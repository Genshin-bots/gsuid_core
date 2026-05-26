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

import asyncio
from typing import List, Optional

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

from . import kanban
from .models import AIAgentTask, AIAgentTaskLog, AIAgentArtifact
from .runtime import PlanRunContext, bind_plan_context, reset_plan_context
from .workspace import put_artifact, ensure_workspace

_VALID_USER_TYPES = ("group", "direct", "channel", "sub_channel")


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
    )
    parts.append(
        "【工作区】你的唯一可写目录是框架绑定的 Artifact Workspace，禁止写入项目根目录、系统临时目录或其它任务目录。"
    )
    return "\n".join(parts)


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


async def _persona_relay(task: AIAgentTask, raw_result: str) -> str:
    """人格转译：能力代理结果再过一遍主人格口吻。

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

    转译 Agent 不写 session log（``session_id=None``）——每次子任务完成都会触发
    一次转译，过去每次都生成 60+KB 的会话日志，纯属噪声。
    """
    if not task.persona_name:
        return raw_result
    try:
        from gsuid_core.ai_core.persona import build_persona_prompt
        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.register import get_all_tools

        arts = await AIAgentArtifact.list_for_task(task.id)
        artifact_block = ""
        if arts:
            # 排序：图片落盘 > 其它落盘 > 纯 inline 文本；同档按时间倒序（最新先）
            def _priority(a: AIAgentArtifact) -> int:
                if a.payload_path and a.mime.startswith("image/"):
                    return 0
                if a.payload_path:
                    return 1
                return 2

            sorted_arts = sorted(arts, key=lambda a: (_priority(a), -a.created_at.timestamp()))
            recommended = next(
                (a for a in sorted_arts if a.payload_path and a.mime.startswith("image/")),
                None,
            ) or next((a for a in sorted_arts if a.payload_path), None)

            lines = []
            for a in sorted_arts[:8]:
                payload_hint = f" path={a.payload_path}" if a.payload_path else " (inline 文本)"
                star = " ⭐" if recommended is not None and a.id == recommended.id else ""
                lines.append(
                    f"- {a.id} | kind={a.artifact_kind} | mime={a.mime}{payload_hint} | {a.summary[:80]}{star}"
                )
            hint = ""
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

        base = await build_persona_prompt(task.persona_name)
        # session_id=None：不写转译 session 日志，避免每次子任务完成都产生 ~67KB 噪声
        agent = create_agent(
            system_prompt=base,
            create_by="Kanban_Relay",
            task_level="low",
            session_id=None,
        )

        # 给转译 agent 准备最小工具池：只装 send_message_by_ai（已统一支持
        # img_xxx / res_xxx / http / base64 多种来源，无需额外的 send_original_pic）
        relay_tools = []
        all_tools = get_all_tools()
        if "send_message_by_ai" in all_tools:
            relay_tools.append(all_tools["send_message_by_ai"].tool)

        ev = _build_event(task)
        spoken = await agent.run(
            user_message=(
                f"【Kanban 子任务播报转译】你的专职助手刚完成了「{task.display_name}」，"
                f"执行结果如下。请用你自己的口吻、简短地把这条进展转告主人——"
                f"只点关键结论与下一步动作，**不要复述细节、不要把自己当作做出该决定的人**。"
                f"\n---\n{raw_result[:1500]}" + artifact_block
            ),
            ev=ev,
            bot=_get_bot(task, ev),
            tools=relay_tools,
            return_mode="return",
        )
        return str(spoken).strip() or raw_result
    except Exception as e:
        logger.debug(f"📋 [Kanban] 人格转译失败，原样播报: {e}")
        return raw_result


async def _notify(task: AIAgentTask, message: str) -> None:
    """通过任意可用 Bot 把消息送达 owner（与 v1 _notify 同源）。"""
    ev = _build_event(task)
    bot = _get_bot(task, ev)
    if not bot:
        logger.warning(f"📋 [Kanban] 任务 root=#{task.ordinal} 无可用 Bot，消息未送达")
        return
    await bot.send(message)


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
        prompt = _format_subtask_prompt(root, fresh, upstream)

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

        if (raw_result or "").startswith(CAPABILITY_AGENT_ERROR_PREFIX):
            await kanban.mark_subtask_failed(fresh, raw_result[:1000])
            await _notify_failure(root, fresh, raw_result[:1000])
        else:
            await kanban.mark_subtask_completed(fresh, output_artifact_id=output_id)
            if bot and raw_result:
                spoken = await _persona_relay(fresh, raw_result)
                if spoken:
                    await _notify(fresh, spoken)


async def _notify_failure(root: AIAgentTask, child: AIAgentTask, reason: str) -> None:
    """子任务失败时按 failure_policy 通知主人格。默认 notify_persona。"""
    policy = root.failure_policy or "notify_persona"
    if policy == "auto_abort":
        await kanban.fail_task_tree(root.id, f"子任务 {child.display_name} 失败：{reason[:200]}")
        await _notify(child, f"⚠️ 任务「{root.display_name}」整树终止：{reason[:200]}")
        return
    # notify_persona：把失败原因转告人格，让主人格走 respawn / fail 决策
    spoken = (
        f"⚠️ 子任务「{child.display_name}」失败：{reason[:300]}\n"
        f"请用 respawn_subtask 修参数重派（达上限会自动转 waiting_approval）；"
        f"或 fail_task_tree 终结整树。"
    )
    await _notify(child, spoken)


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
