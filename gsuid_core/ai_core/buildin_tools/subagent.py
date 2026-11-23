"""Subagent 委派工具模块。

- ``create_subagent(task=..., agent_profile=...)``：
  * 内部轻量节点（``internal_reporter`` / ``memory_curator`` / ``scheduler_assistant``）
    同步直跑，避免在看板堆无产物查询卡；
  * 其余能力代理转为单节点 Kanban 叶子根树（生产路径异步回灌主人格，``TEST`` 同步等到终态）。
- ``summarize_long_input``：内核超长用户输入纯文本摘要防护。
"""

import re
import asyncio
from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.control.lines import RES_HANDLE_RE
from gsuid_core.ai_core.session_registry import get_ai_session_registry
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.configs.task_kinds import TASK_SUBAGENT
from gsuid_core.ai_core.control.delegation import await_delegation, delegation_handle

# 能力代理返回「只有过程句、无事实包」时的判据（供 settle.py 轮数耗尽兜底判空）
_INCOMPLETE_DELIVERY_MARKERS = (
    "停止重复",
    "下面再",
    "再做几次",
    "然后再",
    "然后渲染",
    "先补充",
    "先检索",
    "我去翻",
    "正在搜索",
    "继续搜索",
    "稍后",
    "接下来会",
    "马上整理",
)
# 已登记产物句柄：有 res_ 即视为可消费交付（勿被 OOC 误杀后再判 incomplete）
_ARTIFACT_REGISTERED_RE = re.compile(
    r"(已登记\s*artifact|artifact[_\s-]?put|事实包已登记|登记为\s*\*?`?res_)",
    re.IGNORECASE,
)
# 信息卡 / 图表。命中则不是插画，哪怕句子里带「画」或「立绘」。
_DATA_VISUAL_RE = re.compile(
    r"(?:HTML|html|事实包|数据表|对比表|对比图|指标卡|信息图|图表|流程图|架构图|资料卡|"
    r"柱状图|折线图|饼图|思维导图|\bres_|chart|metrics|dashboard)",
    re.IGNORECASE,
)
_ILLUSTRATION_STRONG_RE = re.compile(
    r"(?:二次元|插画|立绘|全身立图|全身图|动漫图|同人图|同人画|壁纸|素描|生图|anime\s+style|illustration|wallpaper)",
    re.IGNORECASE,
)
# draw / portrait 要整词，避免 withdraw、redraw 误伤。
_ILLUSTRATION_DRAW_RE = re.compile(
    r"(?:画一张|画一个|画个|绘图|(?<![A-Za-z])draw(?![A-Za-z])|(?<![A-Za-z])portrait(?![A-Za-z]))",
    re.IGNORECASE,
)


def looks_like_illustration_request(task: str) -> bool:
    """是否让 render_agent 画插画/立绘/生图。图表、资料卡、流程图不算。"""
    body = (task or "").strip()
    if not body or _DATA_VISUAL_RE.search(body):
        return False
    if _ILLUSTRATION_STRONG_RE.search(body):
        return True
    return _ILLUSTRATION_DRAW_RE.search(body) is not None


def looks_like_incomplete_subagent_delivery(text: str) -> bool:
    """能力代理是否只回了过程句 / 空壳，没有可消费的事实包。"""
    body = (text or "").strip()
    if not body:
        return True
    if body.startswith("⚠️") or "执行失败" in body[:40]:
        return False
    if RES_HANDLE_RE.search(body) or _ARTIFACT_REGISTERED_RE.search(body):
        return False
    has_structure = (
        "|" in body
        or "```" in body
        or body.lstrip().startswith("{")
        or body.lstrip().startswith("[")
        or body.count("\n") >= 5
        or len(re.findall(r"(?m)^\s*[-*•]|\d+[\.、]\s+\S", body)) >= 3
    )
    if has_structure and len(body) >= 120:
        return False
    if any(m in body for m in _INCOMPLETE_DELIVERY_MARKERS) and not has_structure:
        return True
    return not has_structure and len(body) < 160


# 全局并发上限信号量：首个子Agent调用时按配置 subagent_max_concurrency 懒创建并缓存。
# 不在导入期读配置（此时配置可能未就绪）；改并发数需重启——给运行中的信号量改容量不安全。
_subagent_semaphore: Optional[asyncio.Semaphore] = None


def _get_subagent_semaphore() -> asyncio.Semaphore:
    global _subagent_semaphore
    if _subagent_semaphore is None:
        _subagent_semaphore = asyncio.Semaphore(ai_config.get_config("subagent_max_concurrency").data)
    return _subagent_semaphore


# create_subagent(agent_profile=...) 转 Kanban：生产立刻 deferred 回灌，不占会话锁。
# create_by 为 TEST 时同步等待，因为该入口等不到回灌。
_KANBAN_TEST_WAIT_TIMEOUT_SEC = 90.0


def _kanban_wait_sec(parent_create_by: str) -> float:
    """生产派完即走，由执行体回灌；create_by 为 TEST 时同步等到终态。"""
    if parent_create_by == "TEST":
        return _KANBAN_TEST_WAIT_TIMEOUT_SEC
    return 0.0


# 纯 lookup 默认同步 ad-hoc（transient），不建看板卡。
# 外部检索默认 Kanban：超时可回灌，取消不会把事实包扔掉。
_TRANSIENT_DEFAULT_PROFILES = frozenset(
    {
        "internal_reporter",
        "memory_curator",
        "scheduler_assistant",
    }
)


def _delivery_wake_delegate_block(extra: dict[str, object], ev: Optional[Event], target_pid: str = "") -> Optional[str]:
    """回灌帧委派守卫：失败帧禁渲染、有待交付图片先发图、嵌套超限禁止委派。"""
    if "delivery_wake" not in extra or extra["delivery_wake"] is not True:
        return None
    if target_pid == "render_agent" and "delivery_failed" in extra and extra["delivery_failed"] is True:
        return "⚠️ 上游子任务未能完成，无需为报错内容委派 render_agent 出图；请直接用角色口吻简短回应发起人。"
    from gsuid_core.ai_core.planning.kanban_executor import delivery_wake_blocks_new_delegate

    sid = str(ev.session_id) if ev is not None and ev.session_id else ""
    has_img = "delivery_has_image" in extra and extra["delivery_has_image"] is True
    img_sent = "image_sent_this_run" in extra and extra["image_sent_this_run"] is True
    if has_img and not img_sent:
        return "⚠️ 本帧有待交付的图片产物，请先用 send_message_by_ai 发送给发起人，禁止直接再委派。"
    if delivery_wake_blocks_new_delegate(sid):
        return "⚠️ 本帧已达到连续嵌套委派上限，请直接向发起人汇报当前进展，禁止再委派。"
    return None


def _caller_persona_name(ctx: RunContext[ToolContext]) -> str:
    """主人格名：从父 session 的 Agent 读，评测强制 persona 也走这里。"""
    sid = ctx.deps.parent_session_id or ""
    if not sid:
        return ""
    agent = get_ai_session_registry().get_ai_session(sid)
    if agent is None:
        return ""
    name = agent.persona_name
    return str(name) if name else ""


@ai_tools(
    category="common",
    capability_domain="长期任务编排",
    timeout=500.0,
    code_callable=False,
)
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,
    agent_profile: str = "",
) -> str:
    """委派专职能力代理执行多步任务（agent_profile 填 system 能力清单中的准确 node_id）。

    常见节点：research_agent（检索交事实包）、render_agent（已有事实包出信息图，须写明禁再检索）、
    code_agent（写代码/脚本真文件，限主人）。多个独立对象可并列派发；周期/多代理接力走 register_kanban_task。

    Args:
        ctx: 工具执行上下文
        task: 完整任务要求（出图时直接附事实包或 res_ 句柄与用户诉求）
        agent_profile: 目标能力代理 node_id，禁止留空或自造名
    """
    from gsuid_core.ai_core.outbound import write_decision_memo, remember_outbound_topic
    from gsuid_core.ai_core.history_format import note_thread_handles_in_text
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        PENDING_DELEGATION_HOLD,
        delegation_is_inflight,
    )

    # 回执之后再补派只会串行。并列窗口在第一次 deferred ack 之前。
    if delegation_is_inflight(ctx.deps.extra):
        return PENDING_DELEGATION_HOLD

    raw = await _create_subagent_impl(ctx, task=task, agent_profile=agent_profile)
    note_thread_handles_in_text(ctx.deps, raw)
    head = (task or "").strip().split("\n", 1)[0][:80]
    remember_outbound_topic(ctx.deps.extra, head)
    ev = ctx.deps.ev
    await write_decision_memo(
        bot_self_id=str(ev.bot_self_id) if ev is not None and ev.bot_self_id else "",
        text=f"委派 {head[:40]}",
        ref=f"decision:sub:{head[:40]}"[:160],
        owner_user_id=str(ev.user_id) if ev is not None and ev.user_id else "",
    )
    body = f"（委派原问：{head}）\n{raw}" if head else raw
    return await _maybe_fold_subagent_receipt(ctx, body)


async def _maybe_fold_subagent_receipt(ctx: RunContext[ToolContext], text: str) -> str:
    """回执 >1500 字落 FileOS 折句柄卡，堵整包回灌。"""
    if len(text) <= 1500:
        return text
    from gsuid_core.ai_core.planning.tool_output_helper import persist_and_fold_tool_return

    ev = ctx.deps.ev
    session_id = ctx.deps.parent_session_id or ""
    card = await persist_and_fold_tool_return(
        "create_subagent",
        text,
        ev,
        session_id,
        is_group=bool(ev and ev.group_id),
    )
    return card if card else text[:1500] + "\n…[过长已截断，详见句柄]"


async def summarize_long_input(text: str, *, max_tokens: int = 18000) -> str:
    """内核长度防护：超长用户输入摘要（纯文本归纳，不挂载业务工具）。"""
    import hashlib

    from gsuid_core.ai_core.gs_agent import create_agent

    async with _get_subagent_semaphore():
        sid = f"subagent_{hashlib.md5(text.encode()).hexdigest()[:8]}"
        agent = create_agent(
            system_prompt="请精炼归纳用户输入的核心诉求、关键数据与上下文，保留全部事实要点，不编造。",
            max_tokens=max_tokens,
            create_by="AutoPlanner",
            task_kind=TASK_SUBAGENT,
            task_level="high",
            session_id=sid,
            is_subagent=True,
        )
        try:
            res = await agent.run(user_message=f"请总结以下用户输入，保留关键信息：\n\n{text}", return_mode="return")
            return str(res)
        except Exception as e:
            logger.error(i18n_t("log.ai.subagent_fail_execution_failed", e=e))
            return f"⚠️ 超长输入摘要失败: {e}"
        finally:
            if agent._session_logger is not None:
                agent._session_logger.close()


async def _create_subagent_impl(
    ctx: RunContext[ToolContext],
    *,
    task: str,
    agent_profile: str,
) -> str:
    """create_subagent 实现体（已在 pause_wall_clock 内）。"""
    from gsuid_core.ai_core.agent_node import get_node, resolve_node
    from gsuid_core.ai_core.agent_node.registry import (
        list_persona_capability_nodes,
        persona_allows_capability_agent,
    )
    from gsuid_core.ai_core.capability_agents.runner import delegator_persona_name

    persona_name = delegator_persona_name(ctx.deps.ev, _caller_persona_name(ctx))
    if not agent_profile.strip():
        ids = [n.node_id for n in list_persona_capability_nodes(persona_name)][:8]
        return f"⚠️ 未指定 agent_profile。请从花名册填写 node_id：{'、'.join(ids) if ids else '（花名册为空）'}"

    pid = resolve_node(agent_profile)
    profile = get_node(pid) if pid else None
    if not pid or profile is None:
        ids = [n.node_id for n in list_persona_capability_nodes(persona_name)][:8]
        return (
            f"⚠️ 未匹配到能力节点 `{agent_profile.strip()}`。可用 node_id：{'、'.join(ids) if ids else '（花名册为空）'}"
        )
    if not persona_allows_capability_agent(persona_name, pid):
        ids = [n.node_id for n in list_persona_capability_nodes(persona_name)][:8]
        return f"⚠️ 当前人格未启用能力代理 `{pid}`。可用 node_id：{'、'.join(ids) if ids else '（无）'}"
    if pid == "plugin_developer_agent":
        from gsuid_core.ai_core.approval import plugin_install_block_reason

        blocked_install = await plugin_install_block_reason()
        if blocked_install:
            return f"⚠️ {blocked_install}"
    from gsuid_core.ai_core.tool_risk import refuse_master_only_node

    blocked = refuse_master_only_node(ctx.deps.ev, profile)
    if blocked:
        return blocked
    _wake_block = _delivery_wake_delegate_block(ctx.deps.extra, ctx.deps.ev, pid)
    if _wake_block is not None:
        return _wake_block
    if pid == "render_agent" and looks_like_illustration_request(task):
        return (
            "⚠️ render_agent 只把事实包、表格和对比结论渲成 HTML 信息卡或图表，"
            "不画二次元插画、人物立绘或 Diffusion 生图。"
            "角色已有的立绘走 self_info；部署了别的生图工具就改调那个工具，不要派给 render_agent。"
        )
    if pid in _TRANSIENT_DEFAULT_PROFILES:
        return await _dispatch_transient_capability_agent(ctx, task, pid, persona_name)
    return await _dispatch_via_kanban(ctx, task, pid, profile.display_name, persona_name)


async def _dispatch_transient_capability_agent(
    ctx: RunContext[ToolContext],
    task: str,
    pid: str,
    persona_name: str,
) -> str:
    """内部轻量节点（记忆/日程/内部报表）同步直跑，不创建 Kanban 任务卡。"""
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息，create_subagent 派发失败。"

    from gsuid_core.ai_core.capability_agents.runner import (
        CAPABILITY_AGENT_ERROR_PREFIX,
        run_capability_agent,
    )

    logger.info(i18n_t("log.ai.subagent_transient_mode_direct", pid=pid, p0=repr(task[:60])))
    try:
        raw_result = await run_capability_agent(
            profile_id=pid,
            task=task,
            ev=ev,
            bot=ctx.deps.bot,
            session_id_suffix=f"transient_{pid}",
            persona_name=persona_name,
        )
    except Exception as e:
        logger.exception(i18n_t("log.ai.subagent_transient_agent_fail", e=e))
        return f"⚠️ {pid} 临时代理执行失败: {type(e).__name__}: {e}"

    body = (raw_result or "").strip()
    if not body:
        return f"⚠️ {pid} 未返回有效结果。"
    if body.startswith(CAPABILITY_AGENT_ERROR_PREFIX):
        return body
    return f"【{pid} 已完成】\n{body}"


async def _dispatch_via_kanban(
    ctx: RunContext[ToolContext],
    task: str,
    pid: str,
    display_name: str,
    persona_name: str,
) -> str:
    """把 create_subagent 转为 Kanban 叶子根任务派发（生产异步回灌，TEST 同步等待）。"""
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息，create_subagent 派发失败。"

    root_goal = task[:96].replace("\n", " ").strip() or f"{display_name} 临时任务"

    from gsuid_core.ai_core.planning import kanban
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.planning.kanban_tools import extract_res_ids
    from gsuid_core.ai_core.planning.kanban_executor import (
        kick_root,
        claim_inline_delivery,
        mark_deferred_main_delivery,
        format_task_delivery_receipt,
    )

    scope_key = make_scope_key(
        ScopeType.GROUP if ev.group_id else ScopeType.USER_GLOBAL,
        str(ev.group_id or ev.user_id),
    )

    root, _children = await kanban.create_kanban_tree(
        goal=task,
        owner_user_id=str(ev.user_id),
        scope_key=scope_key,
        bot_id=ev.bot_id,
        persona_name=persona_name or None,
        bot_self_id=ev.bot_self_id or "",
        group_id=ev.group_id,
        user_type=ev.user_type or "direct",
        WS_BOT_ID=ev.WS_BOT_ID,
        session_id=ev.session_id,
        user_pm=ev.user_pm,
        broadcast_targets=[],
        display_name=root_goal[:64],
        subtasks=[],
        root_agent_profile=pid,
    )

    handoff_ids = extract_res_ids(task)
    if handoff_ids:
        await AIAgentTask.update_data_by_data(
            select_data={"id": root.id},
            update_data={"input_artifact_ids": handoff_ids},
        )
        root.input_artifact_ids = handoff_ids

    logger.info(
        i18n_t(
            "log.ai.subagent_convert_kanban_leaf",
            p0=root.ordinal,
            p1=root.id[:6],
            pid=pid,
            p2=repr(task[:60]),
        )
    )
    mark_deferred_main_delivery(root.id)
    asyncio.create_task(kick_root(root.id))

    from gsuid_core.ai_core.agent_run.opening_line import release_opening_line
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        format_deferred_subagent_ack,
    )

    opening = await release_opening_line(ctx.deps)
    spoken = opening.text if opening is not None else ""
    opening_missed = opening is not None and not opening.text

    extra = ctx.deps.extra
    _raw_cb = extra["parent_create_by"] if "parent_create_by" in extra else ""
    parent_cb = _raw_cb if isinstance(_raw_cb, str) else ""
    wait_sec = _kanban_wait_sec(parent_cb)
    handle = delegation_handle(root.id)
    if wait_sec <= 0:
        return format_deferred_subagent_ack(
            ordinal=root.ordinal,
            pid=pid,
            handle=handle,
            spoken=spoken,
            opening_missed=opening_missed,
        )
    deleg = await await_delegation(handle, wait_sec=wait_sec)
    if deleg is None:
        return f"⚠️ Kanban 任务记录消失（task_id={root.id}）；可能被并发删除，请到 webconsole 看任务列表。"
    final = await AIAgentTask.get_by_id(root.id)
    if final is None or final.status not in ("completed", "failed", "cancelled", "waiting_approval"):
        return format_deferred_subagent_ack(
            ordinal=root.ordinal,
            pid=pid,
            handle=handle,
            spoken=spoken,
            opening_missed=opening_missed,
        )

    if not claim_inline_delivery(root.id):
        return (
            f"✅ 任务#{root.ordinal} 刚好完成，但交付已被框架接手。"
            "请只输出 <SILENCE>，勿向用户说话、勿重复 create_subagent。"
        )

    from gsuid_core.ai_core.agent_node.registry import persona_allows_capability_agent

    allow_render = persona_allows_capability_agent(persona_name, "render_agent")
    return await format_task_delivery_receipt(final, allow_render=allow_render)
