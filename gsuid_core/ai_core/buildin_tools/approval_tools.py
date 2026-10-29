"""统一审批交互工具（审批中心的 LLM 侧薄封装）。

裁决不经过模型。主人用私聊确认码，或在审批中心点批准 / 拒绝。
``check_approval`` 只报告本会话的状态，不批准，也不返回确认码。

审批能力族（capability_domain="审批交互"）：
- ``ask_user``                : question × user —— 单个澄清提问（选项 + 超时默认；
                                同会话并行调用自动排队逐个呈现）
- ``ask_user_form``           : question × user —— 多问题表单（选项同时呈现、
                                任意顺序作答、收齐才返回；基于 mutiply 缓冲收集）
- ``request_user_approval``   : approval × user —— 花用户自己的资源前请求授权
- ``request_master_approval`` : approval × master —— 敏感权限请求主人
- ``check_approval``          : 本会话有待审批时才可见
"""

import time
import asyncio
from typing import Any, Dict, List, Tuple, Optional

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core import approval as approval_center
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.wall_clock import pause_wall_clock
from gsuid_core.ai_core.approval.halt import APPROVAL_HALT_MARK, halt_line
from gsuid_core.ai_core.approval.models import AIApprovalRequest
from gsuid_core.ai_core.approval.session_ctx import session_has_pending, current_caller_session_id


def _has_pending(ctx: "RunContext[ToolContext]") -> bool:
    ev = ctx.deps.ev if ctx.deps else None
    if ev is None:
        return False
    return approval_center.has_pending(str(ev.user_id))


def _this_session_pending(ctx: "RunContext[ToolContext]") -> bool:
    """只在当前这个 agent 会话挂着审批时露出 check_approval。"""
    deps = ctx.deps
    if deps is None:
        return False
    parent = deps.parent_session_id or ""
    if session_has_pending(parent) or session_has_pending(current_caller_session_id()):
        return True
    ev = deps.ev
    return ev is not None and session_has_pending(ev.session_id or "")


def _agent_dedupe(summary: str) -> str:
    return "agent:" + " ".join(summary.split()).casefold()[:180]


def _status_text(row: AIApprovalRequest) -> str:
    if row.expires_at:
        left = max(0, int(row.expires_at) - int(time.time()))
        clock = f"剩余 {left} 秒。"
    else:
        clock = "沿用旧时限。"
    body = f"#{row.short_id} 状态 {row.status}。{clock}\n事项：{row.title[:120]}\n批准后：{row.effect[:200]}"
    if row.status != "pending":
        return body
    return f"{APPROVAL_HALT_MARK}\n{body}\n已在等主人。不要再安装，不要再派开发任务。"


@ai_tools(
    category="buildin",
    capability_domain="审批交互",
    visible_when=_this_session_pending,
    code_callable=False,
)
async def check_approval(ctx: RunContext[ToolContext], request_ref: str = "") -> str:
    """查看本会话某条审批的状态、剩余时间和批准后会发生什么。

    不批准，也不返回确认码。还在等的时候，本轮应结束，不要再安装或再派开发任务。

    Args:
        request_ref: 短号（如 ab12）或去重键。留空则看本会话全部待审批。
    """
    deps = ctx.deps
    if deps is None:
        return "⚠️ 无会话信息。"
    sid = deps.parent_session_id or current_caller_session_id()
    ref = request_ref.strip().lstrip("#")
    if ref:
        row = await AIApprovalRequest.get_pending_by_short_id(ref)
        if row is None:
            row = await AIApprovalRequest.get_by_short_id(ref)
        if row is None:
            row = await AIApprovalRequest.get_pending_by_dedupe(ref)
        if row is None or (sid and row.origin_session_id and row.origin_session_id != sid):
            return "没有找到这条审批。"
        return _status_text(row)
    if not sid:
        return "本会话没有待审批。"
    rows = await AIApprovalRequest.list_pending_for_origin(sid)
    if not rows:
        return "本会话没有待审批。"
    if len(rows) == 1:
        return _status_text(rows[0])
    lines = [_status_text(row) for row in rows[:5]]
    return f"{APPROVAL_HALT_MARK}\n" + "\n".join(lines) + "\n已在等主人。不要再安装，不要再派开发任务。"


@ai_tools(category="buildin", capability_domain="审批交互", visible_when=_has_pending)
async def list_pending_approvals(ctx: RunContext[ToolContext]) -> str:
    """列出当前用户可裁决的全部待审批请求（多条时用于定位编号 request_ref）。"""
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无会话信息。"
    rows = await approval_center.list_pending_for_resolver(str(ev.user_id))
    if not rows:
        return "ℹ️ 当前没有待审批请求。"
    lines = [
        f"#{r.short_id} [{r.category}｜{'主人级' if r.audience == 'master' else '用户级'}] {r.title[:80]}" for r in rows
    ]
    return "⏳ 待审批请求：\n" + "\n".join(lines)


# 会话级串行锁：receive_resp 等待器是单槽位,并发会互相覆盖 event 导致答案错配
# (2026-07-07 ) 同会话的 ask_user/ask_user_form 必须排队逐个呈现。
_ASK_USER_LOCKS: Dict[str, asyncio.Lock] = {}


async def _log_question_safe(ev: Event, question: str, answer: str, answered: bool) -> None:
    """问答留档（interaction="question"）。落库失败不吞掉用户已给出的回答。"""
    try:
        await approval_center.log_question(ev, question, answer, answered=answered)
    except Exception as e:
        logger.warning(t("log.ai.approval_archival_affect_answer_fail", e=e))


def _ask_user_lock_key(ev: Event) -> str:
    """与 Bot.session_id 同构的会话键：{bot_id}%%%{temp_gid}%%%{uid}。"""
    uid = str(ev.user_id or "0")
    gid = str(ev.group_id or "0") if ev.user_type != "direct" else uid
    return f"{ev.bot_id}%%%{gid}%%%{uid}"


def _as_list(value: Any) -> List[Any]:
    """把"本应是数组"的入参归一化为 list。

    弱模型对无类型数组参数有固定幻觉形状:``{"item": [...]}`` /
    ``{"items": [...]}`` / ``{"values": [...]}``(2026-07-07 实测 MiniMax
    连续 5 次输出该形状)。这里剥掉包装层;单个标量包成单元素列表。
    """
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("item", "items", "values", "options", "list"):
            if key in value and isinstance(value[key], list):
                return value[key]
        return list(value.values())
    if value is None:
        return []
    return [value]


def _first_str(data: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    """按优先序取字典里第一个非空字符串值（LLM 入参无 schema，须逐键守卫）。"""
    for key in keys:
        if key in data and data[key]:
            return str(data[key])
    return ""


def _coerce_option_strings(options: Any) -> List[str]:
    """把选项归一化为字符串列表(宽进严出)。

    容忍:``{"item": [...]}`` 包装层(经 _as_list 剥除)、
    ``[{"label"|"value"|"text": "..."}]`` 对象数组(取展示文本,
    label 优先——按钮上显示什么,用户就回什么,答案匹配才成立)。
    """
    out: List[str] = []
    for op in _as_list(options):
        if isinstance(op, str):
            s = op
        elif isinstance(op, dict):
            s = _first_str(op, ("label", "value", "text"))
        else:
            s = str(op)
        s = s.strip()
        if s:
            out.append(s)
    return out


# timeout=None:排队等待(前序问题各自 ≤300s)+ 自身等待(≤300s)可能超过默认
# 60s 的工具包装超时;ask_user 的每一段等待都有自己的上界,不会永久挂起。
@ai_tools(category="common", capability_domain="审批交互", timeout=None, code_callable=False)
async def ask_user(
    ctx: RunContext[ToolContext],
    question: str,
    options: Optional[List[Any]] = None,
    timeout_seconds: int = 60,
    default_choice: str = "",
) -> str:
    """向当前用户发出一个澄清问题并等待回复（question × user，无权限语义）。

    交互式会话下发送问题（有 options 时以按钮呈现）并等待；超时返回
    default_choice（无默认值则告知超时）。仅在需要用户决定且无法从上下文推断时
    使用，不要用它确认权限（那是 request_user/master_approval 的事）。

    同一会话内并行发起的多个 ask_user 会**自动排队逐个呈现**（问题一 → 用户
    回答 → 问题二 → ...），每次回答精确对应当前挂起的问题。

    Args:
        question: 要问的问题（一句话，附上必要上下文）。
        options: 可选项列表，**纯字符串数组**如 ["16:9", "9:16"]（≤5 个短语；
            提供时用户可点选。也兼容 {value|label|text} 对象，将取其文本）。
        timeout_seconds: 等待秒数（10~300）。
        default_choice: 超时后的默认选择；空=超时视为未回答。
    """
    bot = ctx.deps.bot
    ev = ctx.deps.ev
    if bot is None or ev is None:
        return f"⚠️ 当前无交互通道，无法向用户提问。默认选择：{default_choice or '（无）'}"
    timeout = max(10, min(300, timeout_seconds))
    opts = _coerce_option_strings(options)
    # 会话级串行：receive_resp 的等待器是单槽位,并发会互相覆盖(答案错配/超时)
    lock = _ASK_USER_LOCKS.setdefault(_ask_user_lock_key(ev), asyncio.Lock())
    try:
        # 排队 + 等人的时长不计入 C-4 墙钟软预算：用户思考慢不该把 Agent 逼进收敛
        async with pause_wall_clock(), lock:
            if opts:
                resp = await bot.receive_resp(question, option_list=opts[:5], timeout=timeout)
            else:
                resp = await bot.receive_resp(question, timeout=timeout)
    except Exception as e:
        logger.debug(t("log.ai.approval_ask_user_waiting", e=e))
        resp = None
    answer = "" if resp is None else (resp.raw_text if resp.raw_text else resp.text)
    await _log_question_safe(ev, question, answer or default_choice, answered=resp is not None)
    if resp is None:
        if default_choice:
            return f"（用户超时未回答，按默认值处理）默认选择：{default_choice}"
        return "（用户超时未回答，且无默认值——请按最合理的方案继续并说明原因）"
    return f"用户回答：{answer}"


# timeout=None:表单收集自身有总时限(≤600s),不需要外层包装超时。
@ai_tools(category="common", capability_domain="审批交互", timeout=None, code_callable=False)
async def ask_user_form(
    ctx: RunContext[ToolContext],
    questions: List[Any],
    timeout_seconds: int = 180,
) -> str:
    """一次性向用户提出多个选择题(表单),收齐全部回答后一并返回。

    与连续调用 ask_user 的区别:所有问题的选项按钮**同时呈现**,用户可按任意
    顺序作答;全部答完(或总超时)才返回。回答按「与哪组选项文本匹配」归属到
    对应问题;对不上任何选项的自由文本,按顺序补给最早未回答的问题。
    需要一次确认 2 个以上参数(尺寸/风格/时长...)时**优先用本工具**,
    避免多轮往返。

    Args:
        questions: 问题列表(≤4 个),每项为对象:
            {"question": "画面比例?", "options": ["16:9", "9:16"], "default_choice": "16:9"}
            options 必填(2~5 个短语);default_choice 为超时兜底,可省略。
        timeout_seconds: 收集全部回答的总时限(30~600 秒)。

    Returns:
        逐条列出「问题 → 用户回答」;超时未答的问题标注(超时,按默认)。
    """
    from gsuid_core.bot import Bot

    bot = ctx.deps.bot
    ev = ctx.deps.ev
    if bot is None or ev is None:
        return "⚠️ 当前无交互通道，无法向用户提问。"
    if not isinstance(bot, Bot):
        return "⚠️ 当前通道不支持表单提问，请改用 ask_user 逐个询问。"

    # 解析问题列表(_as_list 剥掉弱模型的 {"item": [...]} 包装幻觉)
    parsed: List[Dict[str, Any]] = []
    for q in _as_list(questions)[:4]:
        if not isinstance(q, dict):
            continue
        text = _first_str(q, ("question", "q")).strip()
        opts = _coerce_option_strings(q["options"] if "options" in q else None)
        if not text or len(opts) < 2:
            continue
        parsed.append(
            {
                "question": text,
                "options": opts[:5],
                "default": _first_str(q, ("default_choice", "default")).strip(),
                "answer": None,
            }
        )
    if not parsed:
        return (
            "⚠️ questions 格式不合法。请严格使用如下 JSON 形状(options 是**纯字符串数组**,"
            "不要包 item/items 等外层键):\n"
            '{"questions": [{"question": "画面比例?", "options": ["16:9", "9:16"], '
            '"default_choice": "16:9"}], "timeout_seconds": 180}'
        )

    timeout_total = max(30, min(600, timeout_seconds))
    deadline = asyncio.get_running_loop().time() + timeout_total

    # 与 ask_user 共用会话级串行锁：任一时刻同会话只有一套提问在收集
    lock = _ASK_USER_LOCKS.setdefault(_ask_user_lock_key(ev), asyncio.Lock())
    # 收表单全程(排队 + 等人作答)不计入 C-4 墙钟软预算
    async with pause_wall_clock(), lock:
        # 1) 同时呈现全部问题(send_option 只发按钮不等待)
        for i, q in enumerate(parsed):
            await bot.send_option(f"{i + 1}. {q['question']}", option_list=q["options"])

        # 2) 用多轮收集模式(mutiply)逐条收回答:回答缓冲在 mutiply_resp 列表,
        #    两次点选之间没有"等待器未注册"的丢失窗口(单槽位 receive_resp 做不到)。
        try:
            while any(q["answer"] is None for q in parsed):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    resp = await bot.receive_mutiply_resp(timeout=remaining)
                except (asyncio.TimeoutError, TimeoutError):
                    break
                if resp is None:
                    break
                answer = (resp.raw_text or resp.text or "").strip()
                if not answer:
                    continue
                # 归属:优先匹配"选项文本一致且未回答"的问题;否则给最早未回答的
                target = next(
                    (q for q in parsed if q["answer"] is None and answer in q["options"]),
                    None,
                ) or next((q for q in parsed if q["answer"] is None), None)
                if target is not None:
                    target["answer"] = answer
        finally:
            # mutiply 注册是持久的,必须显式注销——否则该会话后续所有消息都被吞掉
            bot.mutiply_tag = False
            bot.mutiply_resp.clear()
            Bot.mutiply_instances.pop(bot.session_id, None)
            if bot.temp_gid in Bot.mutiply_map and Bot.mutiply_map[bot.temp_gid] == bot.session_id:
                Bot.mutiply_map.pop(bot.temp_gid)

    lines: List[str] = []
    for i, q in enumerate(parsed):
        await _log_question_safe(
            ev, str(q["question"]), str(q["answer"] or q["default"] or ""), answered=q["answer"] is not None
        )
        if q["answer"] is not None:
            lines.append(f"{i + 1}. {q['question']} → 用户选择:{q['answer']}")
        elif q["default"]:
            lines.append(f"{i + 1}. {q['question']} → (超时未答,按默认):{q['default']}")
        else:
            lines.append(f"{i + 1}. {q['question']} → (超时未答,无默认——请按最合理方案继续并说明)")
    return "用户表单回答:\n" + "\n".join(lines)


@ai_tools(category="common", capability_domain="审批交互", code_callable=False)
async def request_user_approval(ctx: RunContext[ToolContext], summary: str) -> str:
    """向当前用户发出授权请求，询问一项会消耗其资源 / 积分的操作（approval × user）。

    用户配置了「完全访问」时自动放行（照常留审计记录）。否则提交审批并结束本轮。
    用户在私聊回复确认码，或在审批中心点批准。群里说「同意」不会通过。

    Args:
        summary: 要授权的操作说明（做什么、消耗什么、影响什么，一句话）。
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无会话信息，无法发起授权请求。"
    row = await approval_center.submit(
        category="agent_request",
        title=summary,
        ev=ev,
        audience="user",
        allow_full_access_exempt=True,
        dedupe_key=_agent_dedupe(summary),
        effect=summary,
    )
    if row.status == "auto_approved":
        return f"✅ 用户已配置完全访问，操作自动放行（已留记录 #{row.short_id}）。"
    delivered, reused = approval_center.submit_meta(row.request_id)
    return halt_line(row.short_id, delivered=delivered, reused=reused)


@ai_tools(category="common", capability_domain="审批交互", code_callable=False)
async def request_master_approval(ctx: RunContext[ToolContext], summary: str) -> str:
    """向主人发出授权请求，询问一项敏感操作（approval × master，永不可被完全访问豁免）。

    提交后本轮结束。主人用私聊确认码或审批中心批准。

    Args:
        summary: 要授权的操作说明（做什么、为什么需要主人点头，一句话）。
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无会话信息，无法发起授权请求。"
    row = await approval_center.submit(
        category="agent_request",
        title=summary,
        ev=ev,
        audience="master",
        dedupe_key=_agent_dedupe(summary),
        effect=summary,
    )
    delivered, reused = approval_center.submit_meta(row.request_id)
    return halt_line(row.short_id, delivered=delivered, reused=reused)
