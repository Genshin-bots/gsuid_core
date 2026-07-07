"""统一审批交互工具（审批中心的 LLM 侧薄封装）。

「三个入口一个工具」中的**一个工具**：``respond_approval`` 是全框架唯一的审批
转达工具——命令执行、Kanban 子任务、插件安装、工具策略门、Agent 主动请求全部
经它裁决（另两个入口是 webconsole ``/api/ai/approvals`` 与 Kanban 看板兼容端点）。

审批能力族（capability_domain="审批交互"）：
- ``ask_user``                : question × user —— 单个澄清提问（选项 + 超时默认；
                                同会话并行调用自动排队逐个呈现）
- ``ask_user_form``           : question × user —— 多问题表单（选项同时呈现、
                                任意顺序作答、收齐才返回；基于 mutiply 缓冲收集）
- ``request_user_approval``   : approval × user —— 花用户自己的资源前请求授权
- ``request_master_approval`` : approval × master —— 敏感权限请求主人
"""

import asyncio
from typing import Any, Dict, List, Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core import approval as approval_center
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

# 「主人亲口表态」证据校验关键词（原 kanban_tools 同款）。审批是人在环的信任闸门，
# 转达工具绝不允许代理替人拍板；拒绝词优先匹配（"不同意"含子串"同意"）。
_APPROVE_WORDS = (
    "同意",
    "批准",
    "可以",
    "答应",
    "准了",
    "通过",
    "去吧",
    "装吧",
    "安装吧",
    "去装",
    "可装",
    "好的",
    "好啊",
    "好呀",
    "行吧",
    "没问题",
    "允许",
    "认可",
    "ok",
    "okay",
    "yes",
    "approve",
    "go ahead",
    "agree",
)
_REJECT_WORDS = (
    "不同意",
    "不批准",
    "不可以",
    "不行",
    "不要",
    "不用",
    "别装",
    "别安装",
    "先别",
    "拒绝",
    "取消",
    "算了",
    "驳回",
    "否决",
    "不准",
    "no",
    "deny",
    "reject",
)


def _has_pending(ctx: "RunContext[ToolContext]") -> bool:
    ev = ctx.deps.ev if ctx.deps else None
    if ev is None:
        return False
    return approval_center.has_pending(str(ev.user_id))


@ai_tools(category="buildin", capability_domain="审批交互", visible_when=_has_pending)
async def respond_approval(
    ctx: RunContext[ToolContext],
    approved: bool,
    request_ref: str = "",
    note: str = "",
) -> str:
    """转达用户 / 主人对某条待审批请求的同意 / 拒绝（全框架唯一审批转达入口）。

    覆盖所有待审批类型：命令执行、Kanban 子任务（含插件安装）、工具调用授权、
    Agent 主动请求。多条待决时用 request_ref 指明编号（如 "#ab12"）。

    ⚠️ **人在环审批闸门**：本工具只用于【转达用户/主人**亲口**说的同意 / 拒绝】，
    你绝不能替人拍板——只有当前这一轮消息里有明确表态（"同意/可以/批准" 或
    "拒绝/不要/取消"）才调用，且 approved 必须与话里的意思一致；框架会校验，
    伪造会被拒绝。没表态时请转告"请求正在等待审批"并请其回复。

    Args:
        approved: True=同意，False=拒绝。
        request_ref: 待审批请求编号（多条时必填，形如 "ab12" 或 "#ab12"）。
        note: 用户 / 主人的附加说明。
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无会话信息。"

    # 防"代理替人伪造审批"：当前这条用户消息必须有与 approved 一致的明确表态
    # （实测会话 2df150：人格曾自行 approved=True 放行自己发起的安装请求）。
    user_msg = (ev.raw_text or ev.text or "").strip().lower()
    said_reject = any(w in user_msg for w in _REJECT_WORDS)
    said_approve = (not said_reject) and any(w in user_msg for w in _APPROVE_WORDS)
    if approved and not said_approve:
        return (
            "⛔ 拒绝执行：不能替用户/主人做审批决定。当前这条用户消息里没有"
            "「同意 / 可以 / 批准」之类的明确批准表达"
            f"（原文：{user_msg[:60]!r}）。请先把「请求正在等待审批」转告对方并请其"
            "明确回复；等对方**这一轮亲口**说了同意，再调用本工具。"
        )
    if (not approved) and not said_reject:
        return (
            "⛔ 拒绝执行：不能替用户/主人做拒绝决定。当前这条用户消息里没有"
            f"「拒绝 / 不要 / 取消」之类的明确拒绝表达（原文：{user_msg[:60]!r}）。"
            "等对方亲口表态再转达。"
        )

    return await approval_center.resolve(
        request_ref=request_ref,
        approved=approved,
        resolver_user_id=str(ev.user_id),
        note=note,
        via="chat",
    )


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


# ask_user 会话级串行锁：``bot.receive_resp`` 在同一会话上**不可并发**——
# ``Bot.instances[session_id]`` 是单槽位,且两个并发 receive_resp 共用同一个
# Bot 对象时,后者会覆盖前者的 ``self.event``,导致用户的回答被喂给错误的问题、
# 先注册的问题永远等不到回复只能超时(2026-07-07 画布多问题错配事故)。
# 串行化后多个问题按序逐个呈现,每次回答精确对应当前挂起的问题。
_ASK_USER_LOCKS: Dict[str, asyncio.Lock] = {}


def _ask_user_lock_key(ev) -> str:  # noqa: ANN001
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
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
        return list(value.values())
    if value is None:
        return []
    return [value]


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
            s = str(op.get("label") or op.get("value") or op.get("text") or "")
        else:
            s = str(op)
        s = s.strip()
        if s:
            out.append(s)
    return out


# timeout=None:排队等待(前序问题各自 ≤300s)+ 自身等待(≤300s)可能超过默认
# 300s 的工具包装超时;ask_user 的每一段等待都有自己的上界,不会永久挂起。
@ai_tools(category="common", capability_domain="审批交互", timeout=None)
async def ask_user(
    ctx: RunContext[ToolContext],
    question: str,
    options: Optional[List[Any]] = None,
    timeout_seconds: int = 60,
    default_choice: str = "",
) -> str:
    """向当前用户提出一个澄清问题并等待回复（question × user，无权限语义）。

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
        async with lock:
            if opts:
                resp = await bot.receive_resp(question, option_list=opts[:5], timeout=timeout)
            else:
                resp = await bot.receive_resp(question, timeout=timeout)
    except Exception as e:
        logger.debug(f"✅ [Approval] ask_user 等待回复失败: {e}")
        resp = None
    if resp is None:
        if default_choice:
            return f"（用户超时未回答，按默认值处理）默认选择：{default_choice}"
        return "（用户超时未回答，且无默认值——请按最合理的方案继续并说明原因）"
    answer = resp.raw_text if resp.raw_text else resp.text
    return f"用户回答：{answer}"


# timeout=None:表单收集自身有总时限(≤600s),不需要外层包装超时。
@ai_tools(category="common", capability_domain="审批交互", timeout=None)
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
        text = str(q.get("question") or q.get("q") or "").strip()
        opts = _coerce_option_strings(q.get("options"))
        if not text or len(opts) < 2:
            continue
        parsed.append(
            {
                "question": text,
                "options": opts[:5],
                "default": str(q.get("default_choice") or q.get("default") or "").strip(),
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
    async with lock:
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
            # mutiply 注册是持久的,必须显式注销——否则该会话后续所有消息
            # 都会被 mutiply 分发吞掉,再也到不了正常对话链路。
            bot.mutiply_tag = False
            bot.mutiply_resp.clear()
            Bot.mutiply_instances.pop(bot.session_id, None)
            if Bot.mutiply_map.get(bot.temp_gid) == bot.session_id:
                Bot.mutiply_map.pop(bot.temp_gid, None)

    lines: List[str] = []
    for i, q in enumerate(parsed):
        if q["answer"] is not None:
            lines.append(f"{i + 1}. {q['question']} → 用户选择:{q['answer']}")
        elif q["default"]:
            lines.append(f"{i + 1}. {q['question']} → (超时未答,按默认):{q['default']}")
        else:
            lines.append(f"{i + 1}. {q['question']} → (超时未答,无默认——请按最合理方案继续并说明)")
    return "用户表单回答:\n" + "\n".join(lines)


@ai_tools(category="common", capability_domain="审批交互")
async def request_user_approval(ctx: RunContext[ToolContext], summary: str) -> str:
    """请求**当前用户**授权一项会消耗其资源 / 积分的操作（approval × user）。

    用户配置了「完全访问」时自动放行（照常留审计记录）；否则提交审批请求，
    用户回复同意后经 respond_approval 裁决。

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
    )
    if row.status == "auto_approved":
        return f"✅ 用户已配置完全访问，操作自动放行（已留记录 #{row.short_id}）。"
    return f"⏳ 已向用户发起授权请求 #{row.short_id}：{summary[:80]}。请转告用户并请其回复同意 / 拒绝。"


@ai_tools(category="common", capability_domain="审批交互")
async def request_master_approval(ctx: RunContext[ToolContext], summary: str) -> str:
    """请求**主人**授权一项敏感操作（approval × master，永不可被完全访问豁免）。

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
    )
    return f"⏳ 已向主人发起授权请求 #{row.short_id}：{summary[:80]}。请转告主人并请其回复同意 / 拒绝。"
