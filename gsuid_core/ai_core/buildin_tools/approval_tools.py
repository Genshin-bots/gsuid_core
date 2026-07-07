"""统一审批交互工具（审批中心的 LLM 侧薄封装）。

「三个入口一个工具」中的**一个工具**：``respond_approval`` 是全框架唯一的审批
转达工具——命令执行、Kanban 子任务、插件安装、工具策略门、Agent 主动请求全部
经它裁决（另两个入口是 webconsole ``/api/ai/approvals`` 与 Kanban 看板兼容端点）。

审批能力族（capability_domain="审批交互"）：
- ``ask_user``                : question × user —— 澄清提问（选项 + 超时默认）
- ``request_user_approval``   : approval × user —— 花用户自己的资源前请求授权
- ``request_master_approval`` : approval × master —— 敏感权限请求主人
"""

from typing import List, Optional

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


@ai_tools(category="common", capability_domain="审批交互")
async def ask_user(
    ctx: RunContext[ToolContext],
    question: str,
    options: Optional[List[str]] = None,
    timeout_seconds: int = 60,
    default_choice: str = "",
) -> str:
    """向当前用户提出一个澄清问题并等待回复（question × user，无权限语义）。

    交互式会话下发送问题（有 options 时以按钮呈现）并等待；超时返回
    default_choice（无默认值则告知超时）。仅在需要用户决定且无法从上下文推断时
    使用，不要用它确认权限（那是 request_user/master_approval 的事）。

    Args:
        question: 要问的问题（一句话，附上必要上下文）。
        options: 可选项列表（≤5 个短语；提供时用户可点选）。
        timeout_seconds: 等待秒数（10~300）。
        default_choice: 超时后的默认选择；空=超时视为未回答。
    """
    bot = ctx.deps.bot
    if bot is None:
        return f"⚠️ 当前无交互通道，无法向用户提问。默认选择：{default_choice or '（无）'}"
    timeout = max(10, min(300, timeout_seconds))
    try:
        if options:
            resp = await bot.receive_resp(question, option_list=options[:5], timeout=timeout)
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
