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
webconsole 或对话回复审批（统一转达工具 ``respond_approval``）。
"""

import re
import time
import asyncio
from typing import List, Tuple, Optional

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.proactive import emit_proactive_message
from gsuid_core.ai_core.control.mailbox import drain_one, post_to_session
from gsuid_core.ai_core.control.directive import Directive

from . import kanban
from .models import AIAgentTask, AIAgentTaskLog, AIAgentArtifact
from .runtime import PlanRunContext, bind_plan_context, reset_plan_context
from .workspace import put_artifact, ensure_workspace

_VALID_USER_TYPES = ("group", "direct", "channel", "sub_channel")

# 围栏代码块匹配（含语言标注的 ```python ... ```）。用于在"转译兜底"时剥离能力代理
# 原始产出里的大段代码 / 原始数据——它们绝不该直接回灌给用户（群聊刷屏与污染）。
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _artifact_text_excerpt(arts: List[AIAgentArtifact], *, limit: int = 4000) -> str:
    """拼接 text/* artifact 正文摘要（优先 inline，再读 path），供主人格转述。"""
    from gsuid_core.ai_core.planning.tool_output_protocol import load_payload_text

    if limit <= 0 or not arts:
        return ""
    chunks: List[str] = []
    remaining = limit
    for a in arts:
        if remaining <= 0:
            break
        mime = (a.mime or "text/plain").strip().lower()
        if mime.startswith("image/"):
            continue
        body, err = load_payload_text(
            payload_inline=a.payload_inline,
            payload_path=a.payload_path or "",
        )
        if err or not body:
            summary = (a.summary or "").strip()
            if not summary:
                continue
            piece = summary[:remaining]
        else:
            piece = body[:remaining]
        if piece:
            chunks.append(piece)
            remaining -= len(piece)
    return "\n---\n".join(chunks)


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


_RELAY_META_LINE_RE = re.compile(
    r"(?im)^.*(?:artifact|res_[0-9a-f]{6,}|img_[0-9a-f]{6,}|send_message_by_ai|"
    r"create_subagent|主人格|句柄|Kanban|tool_return).*$"
)


def _sanitize_relay_spoken(text: str) -> str:
    """转译产物清洗：去掉内部句柄/工具名/元流程台词，并过 OOC。"""
    from gsuid_core.ai_core.output_firewall import check_ooc

    raw = (text or "").strip()
    if not raw:
        return ""
    cleaned = _RELAY_META_LINE_RE.sub("", raw)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        return "唔…搞定了…呼。"
    hit = check_ooc(cleaned)
    if hit is not None:
        # 再剥一轮框架泄漏后仍脏 → 极短角色兜底，绝不把 API 念给用户
        cleaned2 = _RELAY_META_LINE_RE.sub("", cleaned).strip()
        if not cleaned2 or check_ooc(cleaned2) is not None:
            return "唔…搞定了…图里有…呼。"
        return cleaned2[:400]
    return cleaned[:600]


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
    # 叶子根常塞完整事实包（create_subagent）；过短上限会裁掉后半段字段
    _LEAF_GOAL_MAX = 100_000
    _CHILD_GOAL_MAX = 24_000
    _ROOT_GOAL_MAX = 4_000
    if is_leaf_root:
        goal = (
            child.goal
            if len(child.goal) <= _LEAF_GOAL_MAX
            else (child.goal[:_LEAF_GOAL_MAX] + "\n…[任务描述过长已截断；请 artifact_get 上游 res_ 取全文]")
        )
        parts = [
            "【Kanban 单步任务】你是被任务树调度器派来的专职执行体，请独立完成本任务。",
            f"任务描述：{goal}",
            f"分配画像：{child.agent_profile or '（未指定）'}",
        ]
    else:
        parts = [
            "【Kanban 子任务】你是被任务树调度器派来的专职执行体，请独立完成本节点。",
            f"任务树根目标：{root.goal[:_ROOT_GOAL_MAX]}",
            f"本子任务描述：{child.goal[:_CHILD_GOAL_MAX]}",
            f"分配画像：{child.agent_profile or '（未指定）'}",
        ]
    # 断点续作提示：审批挂起→批准→重新调度后，能力代理 history 为空、会从头重做；
    # 这段提示放在任务描述紧后面（高显著位），让它直接接着上一轮的断点往下做。
    if resume_hint:
        parts.append(resume_hint)
    if child.params_override:
        # JSON 而非 Python repr——避免 dict 渲染成 {'k': 'v'} 让 LLM 误以为是
        # Python 字面量；JSON 格式更接近代理实际要往 record_put / state_set 里塞
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
        "只有真实文件 artifact 才能被上游作为图片/文件发出。"
        '\n- 纯文本结论 / 报告正文：`artifact_put(payload="...", summary="...")`。'
        "\n- 持久化业务数据（签到名单、流水、任务条目等）：用 `record_put` / "
        "`record_append` / `record_update` 写入框架统一的 `record:<集合名>` 集合，"
        "**不要**只塞进 state_set 大 JSON 块或自己写文件——其它子任务读不到。"
        "\n- 返回值：一两句**事实结论摘要**（给上游编排用），禁止写「交给主人格/请转译/发图」"
        "等流程元话语，禁止对用户会话直发。"
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
        logger.debug(t("log.ai.kanban_construct_resume_checkpoint_fail", e=e))
        return ""


async def _collect_upstream_artifacts(child: AIAgentTask) -> List[AIAgentArtifact]:
    """汇总上游产出：显式 ``input_artifact_ids`` + 依赖子任务登记的 artifact。

    叶子根（``create_subagent``）常在 goal 里带上游 ``res_``；建树时会写入
    ``input_artifact_ids``，此处一并注入提示，避免仅靠 agent 自觉 ``artifact_get``。
    """
    bag: List[AIAgentArtifact] = []
    seen: set = set()

    for rid in child.input_artifact_ids if isinstance(child.input_artifact_ids, list) else []:
        if not rid or rid in seen:
            continue
        art = await AIAgentArtifact.get_by_id(str(rid))
        if art is not None:
            bag.append(art)
            seen.add(art.id)

    deps = child.dependency_task_ids if isinstance(child.dependency_task_ids, list) else []
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
            hint = ""
            if recommended is not None:
                hint = (
                    f"\n【待发媒体】优先把 `{recommended.id}`（{recommended.mime}）"
                    "作为图片/文件发出；不要发纯文本预览档。"
                )
            # 句柄只给工具参数用；对用户可见台词禁止提 id / 工具名
            artifact_block = (
                "\n\n【内部·勿写入对用户台词】可用媒体/文件列表：\n"
                + "\n".join(lines)
                + hint
                + "\n有图片时：用发送工具把图发出，台词只写一两句角色短句（结论/情绪）。"
                "\n**严禁**在台词里出现：工具名、res_/img_ 句柄、artifact、主人格、"
                "「交给谁发」、流程说明。"
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
                f"【播报转译·审批】助手完成了「{task.display_name}」，需要主人点头才能继续。"
                "用你自己的角色口吻简短转告，并请主人同意或拒绝。"
                "不要复述代码/细节，不要提工具名或内部流程，不要替主人做决定。"
            )
        else:
            instruction = (
                f"【播报转译】助手完成了「{task.display_name}」。"
                "用你自己的角色口吻、一两句把结论告诉用户；"
                "有图就调用发送工具把图发出去，台词只留角色短句。"
                "**禁止**：复述大段数据；说「主人格/代理/句柄/artifact/工具名」；"
                "把内部流程说明念给用户听；原样贴代码或原始报告。"
            )
        spoken: str = await agent.run(
            user_message=f"{instruction}\n---\n{raw_result[:1500]}" + artifact_block,
            ev=ev,
            bot=_get_bot(task, ev),
            tools=relay_tools,
            return_mode="return",
        )
        clean = _sanitize_relay_spoken(spoken)
        if clean:
            return clean, relay_log_files
        return _sanitize_relay_spoken(_sanitize_for_user(raw_result)), relay_log_files
    except Exception as e:
        logger.debug(t("log.ai.kanban_persona_rendition_code_fail", e=e))
        return _sanitize_for_user(raw_result), relay_log_files
    finally:
        # 无论成功 / 异常，关闭转译 SubAgent logger；relay_log_files 在 return 表达式求值后才被 append（list 是引用
        # append 对返回值同样可见）。
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
        logger.warning(t("log.ai.kanban_fail_send_msg_task_failed", p0=task.ordinal))


# 子任务播报静默信号 能力代理在最终输出里以本标记单独成段/作行首前缀，声明"本轮没有值得播报的
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


# 交互式 create_subagent：主人格转述、执行体不推群（进程内 set，见 §能力代理）。
_INTERACTIVE_RELAY_ROOTS: set[str] = set()
# 同步等待已超时：完成后唤醒主人格交付，禁止 _persona_relay 替发（2026-08-04）。
_DEFERRED_MAIN_DELIVERY_ROOTS: set[str] = set()


def mark_interactive_relay_root(root_id: str) -> None:
    """登记「主人格转述、执行体静默」的交互式叶子根。"""
    _INTERACTIVE_RELAY_ROOTS.add(root_id)


def discard_interactive_relay_root(root_id: str) -> None:
    """撤销 interactive + deferred 登记。"""
    _INTERACTIVE_RELAY_ROOTS.discard(root_id)
    _DEFERRED_MAIN_DELIVERY_ROOTS.discard(root_id)


def mark_deferred_main_delivery(root_id: str) -> None:
    """超时后保持静默，完成后改走 ``_wake_main_agent_for_delivery``。"""
    _INTERACTIVE_RELAY_ROOTS.add(root_id)
    _DEFERRED_MAIN_DELIVERY_ROOTS.add(root_id)


def try_claim_deferred_for_inline_return(root_id: str) -> bool:
    """超时边界：deferred 仍在则 claim 并 True（本轮 tool_return，避免与 wake 双份）。"""
    if root_id in _DEFERRED_MAIN_DELIVERY_ROOTS:
        _DEFERRED_MAIN_DELIVERY_ROOTS.discard(root_id)
        return True
    return False


def _consume_interactive_relay(root_id: str) -> bool:
    """读即弃：是否 interactive 静默。"""
    if root_id in _INTERACTIVE_RELAY_ROOTS:
        _INTERACTIVE_RELAY_ROOTS.discard(root_id)
        return True
    return False


def _consume_deferred_main_delivery(root_id: str) -> bool:
    """读即弃：是否需唤醒主人格。"""
    if root_id in _DEFERRED_MAIN_DELIVERY_ROOTS:
        _DEFERRED_MAIN_DELIVERY_ROOTS.discard(root_id)
        return True
    return False


def _format_delivery_for_main_agent(task: AIAgentTask, raw_result: str, arts: List[AIAgentArtifact]) -> str:
    """拼给主人格的交付包：与 PersistedHandleCard 同形，禁止全文塞 prompt。"""
    _ = raw_result
    from gsuid_core.ai_core.planning.tool_output_protocol import PersistedHandleCard

    cards: List[str] = []
    primary = ""
    primary_is_image = False
    for a in arts[:8]:
        mime = a.mime or "text/plain"
        is_img = bool(mime.startswith("image/"))
        size = len((a.payload_inline or "").encode("utf-8")) if a.payload_inline else 0
        card = PersistedHandleCard(
            id=a.id,
            kind="image" if is_img else "artifact",
            mime=mime,
            summary=(a.summary or "")[:200],
            size_bytes=size,
            read_tool="read_handle",
            long_structured=not is_img,
            speech_expand=False,
        )
        cards.append(card.format())
        if is_img and not primary:
            primary = a.id
            primary_is_image = True
    if not primary and arts:
        primary = arts[0].id
        primary_is_image = bool((arts[0].mime or "").startswith("image/"))

    parts = [
        f"【子任务交付·需你亲自完成收尾】任务#{task.ordinal}「{task.display_name}」已完成。",
        "你是主人格：角色短句给结论；有图则 send_message_by_ai(image_id=)；",
        '长文尚未出图 → create_subagent(agent_profile="render_agent", task=句柄+版式)；',
        "禁止把句柄写进对用户台词。",
        "禁止为写台词去展开长文——句柄卡 summary 足够一句结论；出图节点自己读全文。",
        "出图委派发出后本轮只许 <SILENCE> 或一句等待，禁止把事实包数字念成群聊台词。",
    ]
    if cards:
        parts.append("产物句柄卡：")
        parts.extend(cards)
        if primary and primary_is_image:
            parts.append(f"💡 主图：`{primary}` → send_message_by_ai(image_id=)。")
    elif task.failure_reason:
        parts.append(f"失败原因: {task.failure_reason[:500]}")
    return "\n".join(parts)


# 回灌合并：payload 按 session|root 存最新，邮箱记「有消息」，flush 控节奏
_delivery_pending: dict[str, tuple[AIAgentTask, str]] = {}
_delivery_flush_tasks: dict[str, asyncio.Task] = {}
_DELIVERY_COALESCE_SEC = 0.45


async def _wake_main_agent_for_delivery(task: AIAgentTask, raw_result: str) -> None:
    """能力代理完成后回灌主 session；同 root 短窗合并为一次。

    payload 与「有消息」分开存：邮箱槽位按 (kind, root) 精确消费，绝不用会话级
    drain 当布尔量——那会把兄弟 root 的待投递一并抽走，后到 flush 就静默丢单。
    """
    session_id = (task.session_id or "").strip()
    root_id = task.root_task_id or task.id
    key = f"{session_id}|{root_id}"
    # 同 root 二次完成：payload 覆盖为最新，避免 wake 用过期结果
    _delivery_pending[key] = (task, raw_result)
    post_to_session(
        session_id,
        Directive(
            kind="delivery",
            reason_code="kanban_delivery",
            observation=f"子任务 {task.display_name or root_id} 已结束。",
        ),
        merge_key=root_id,
    )
    existing = _delivery_flush_tasks.get(key)
    if existing is not None and not existing.done():
        return

    async def _flush() -> None:
        try:
            await asyncio.sleep(_DELIVERY_COALESCE_SEC)
            _delivery_flush_tasks.pop(key, None)
            item = _delivery_pending.pop(key, None)
            # 邮箱只作顾问记录（prepare 尚未 drain）。唤醒只看 payload，避免 AND 闩丢单。
            drain_one(session_id, "delivery", root_id)
            if item is not None:
                await _wake_main_agent_for_delivery_now(item[0], item[1])
        except Exception as e:
            logger.debug(t("log.ai.delivery_coalesce_flush_skip", e=e))
            _delivery_flush_tasks.pop(key, None)
            _delivery_pending.pop(key, None)

    _delivery_flush_tasks[key] = asyncio.create_task(_flush())


async def _wake_main_agent_for_delivery_now(task: AIAgentTask, raw_result: str) -> None:
    """实际唤醒主人格（合并后单次）。"""
    arts = await AIAgentArtifact.list_for_task(task.id)
    delivery = _format_delivery_for_main_agent(task, raw_result, arts)
    ev = _build_event(task)
    bot = _get_bot(task, ev)

    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    session_id = (task.session_id or "").strip()
    session = get_ai_session_registry().get_ai_session(session_id) if session_id else None
    if session is None and session_id and bot is not None:
        from gsuid_core.ai_core.ai_router import get_ai_session

        session = await get_ai_session(ev)

    if session is None or bot is None:
        logger.warning(
            t(
                "log.ai.kanban_deferred_wake_fallback_relay",
                task=task.id[:8],
                reason=f"session={session is not None},bot={bot is not None}",
            )
        )
        if bot is not None:
            short = _sanitize_relay_spoken(_sanitize_for_user(raw_result or ""))
            if short:
                await _notify(
                    task,
                    short,
                    trigger_reason=f"delivery_no_session:{task.display_name}",
                )
        return

    owner = (task.owner_user_id or "").strip()
    at_hint = f"收尾时 @发起人 `@{owner}`。" if owner else ""
    frame_text = (
        "[框架·任务完成]\n"
        f"{delivery}\n\n"
        "（框架注入：子任务已完成，请你以主人格身份收尾——"
        "角色短句 + 有图则用发送工具把图发出；"
        f"{at_hint}"
        "禁止把内部句柄/工具名念给用户；不要重做同一子任务。）"
    )
    await session.run(
        user_message=frame_text,
        bot=bot,
        ev=ev,
        return_mode="by_bot",
        has_active_task=True,
        is_framework_injection=True,
    )
    logger.info(t("log.ai.kanban_deferred_main_delivery_done", task=task.id[:8]))


async def _finish_capability_delivery(
    *,
    root: AIAgentTask,
    child: AIAgentTask,
    raw_result: str,
    bot: Optional[Bot],
    no_broadcast: bool = False,
    is_failure: bool = False,
    is_approval: bool = False,
) -> None:
    """能力代理终态统一出口：交互/有主 session → 回灌主人格；否则极简推群。

    **交互路径绝不 ``_persona_relay``**——完成后只 ``_wake_main_agent_for_delivery``。
    """
    interactive = _consume_interactive_relay(root.id)
    deferred = _consume_deferred_main_delivery(root.id)

    if no_broadcast and not deferred:
        # 交互同步等待中：主人格会自己收 tool_return；非交互无播报则静默
        return

    # 交互：仅 deferred 时唤醒（同步已完成则主人格已在 create_subagent 内拿到结果）
    if interactive:
        if deferred and bot is not None:
            await _wake_main_agent_for_delivery(child, raw_result)
        elif deferred and bot is None:
            logger.warning(t("log.ai.kanban_fail_send_msg_task_failed", p0=child.ordinal))
        return

    # 非交互但绑定了主 session：同样回灌主人格（像真人触发），不用 Relay
    sid = (child.session_id or root.session_id or "").strip()
    if sid and bot is not None and not no_broadcast:
        await _wake_main_agent_for_delivery(child, raw_result)
        return

    # 无主 session 的纯后台：失败/审批/结论走 notify，不跑 Kanban_Relay LLM
    if is_failure:
        await _notify_failure(root, child, raw_result)
        return
    if is_approval or (raw_result and not no_broadcast):
        short = _sanitize_relay_spoken(_sanitize_for_user(raw_result))
        if short:
            await _notify(
                child,
                short,
                trigger_reason=(
                    f"approval_request:{child.display_name}" if is_approval else f"subtask={child.display_name}"
                ),
            )


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
            logger.exception(t("log.ai.kanban_subtask_raised_fail", e=e))
            await kanban.mark_subtask_failed(fresh, f"{type(e).__name__}: {e}")
            await _finish_capability_delivery(
                root=root,
                child=fresh,
                raw_result=f"{type(e).__name__}: {e}",
                bot=bot,
                is_failure=True,
            )
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
                payload=raw_result[:120000],
                summary=f"子任务自动留档：{fresh.display_name}"[:512],
                mime="text/plain",
                artifact_kind="output",
                plan_ctx=plan_ctx,
            )
            if art is not None:
                output_id = art.id
        # 子代理终态落盘（FileOS）；I/O 失败不阻断终态
        from gsuid_core.ai_core.planning.tool_output_helper import persist_subagent_result

        try:
            await persist_subagent_result(
                profile=fresh.agent_profile or "",
                content=raw_result,
                task=fresh,
                res_handle=output_id or "",
            )
        except Exception as _pe:
            logger.debug(t("log.ai.persist_subagent_result_skip", e=_pe))

        # 5) 落终态
        from gsuid_core.ai_core.capability_agents.runner import (
            CAPABILITY_AGENT_ERROR_PREFIX,
        )

        # 交互 / 有主 session → 回灌主人格；绝不 Kanban_Relay
        if latest is not None and latest.status == "waiting_approval":
            body = latest.failure_reason or raw_result
            await _finish_capability_delivery(
                root=root,
                child=fresh,
                raw_result=body,
                bot=bot,
                no_broadcast=no_broadcast,
                is_approval=True,
            )
        elif (raw_result or "").startswith(CAPABILITY_AGENT_ERROR_PREFIX):
            await kanban.mark_subtask_failed(fresh, raw_result[:1000])
            await _finish_capability_delivery(
                root=root,
                child=fresh,
                raw_result=raw_result[:1000],
                bot=bot,
                no_broadcast=no_broadcast,
                is_failure=True,
            )
        else:
            await kanban.mark_subtask_completed(fresh, output_artifact_id=output_id)
            if no_broadcast:
                logger.debug(
                    t(
                        "log.ai.kanban_subtask_declared_silence",
                        p0=fresh.display_name,
                        KANBAN_NO_BROADCAST_MARK=KANBAN_NO_BROADCAST_MARK,
                    )
                )
            await _finish_capability_delivery(
                root=root,
                child=fresh,
                raw_result=raw_result or "",
                bot=bot,
                no_broadcast=no_broadcast,
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
        logger.debug(
            t(
                "log.ai.kanban_skipping_direct_scheduling_skip",
                root_task_id=root_task_id,
            )
        )
        return

    # 叶子根：直接把 root 当作单一执行节点派出
    if kanban.is_leaf_root(root, len(children)):
        if root.status == "pending":
            logger.info(
                t(
                    "log.ai.kanban_scheduling_leaf_root_task",
                    root_task_id=root_task_id,
                    p0=root.agent_profile,
                )
            )
            await _run_one_task_node(root, root)
        # 叶子根状态由 _run_one_task_node 自己写完，不需要 refresh_root_status
        return

    # 先把"依赖已满足、可以 arm"的周期子任务模板挂到 APScheduler—— 这一步对一棵新树第一次 kick 时把 init
    await _maybe_arm_recurring_subtasks(root, children)

    ready = kanban.get_ready_child_tasks(children, root_status=root.status)
    if not ready:
        await kanban.refresh_root_status(root_task_id)
        return

    logger.info(t("log.ai.kanban_scheduling_root_task_id", root_task_id=root_task_id, p0=len(ready)))
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
                logger.warning(t("log.ai.kanban_arm_recurring_subtask_fail", p0=tpl.id, p1=root.id, msg=msg))
        except Exception as e:
            logger.exception(t("log.ai.kanban_arming_recurring_subtask_fail", p0=tpl.id, p1=root.id, e=e))


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
        logger.exception(t("log.ai.kanban_kick_root_fail", e=e))
        await AIAgentTaskLog.add_log(root_task_id, "decision", f"调度异常：{type(e).__name__}: {e}")
