"""统一审批中心（ApprovalCenter）。

全框架只有两个审批动词：``submit``（提交请求）与 ``resolve``（裁决）。三个裁决
入口——对话工具 ``respond_approval``、webconsole ``/api/ai/approvals``、领域兼容
端点（Kanban 看板审批按钮）——全部落到本模块；"批准之后干什么"由 category
注册的 ``on_resolve`` 领域回调承担（command_exec 执行快照 / kanban 子任务回
pending / tool_call 发放一次性放行 grant 等）。

三种审批 =（interaction × audience）三个合法组合：
- ``question × user``   ：ask_user 澄清（超时走默认，无权限语义）；
- ``approval × user``   ：花用户自己的资源（可被「完全访问」豁免，仍留 auto 记录）；
- ``approval × master`` ：敏感权限（装插件 / 执行命令 / 花主人的钱），**永不可豁免**。
"""

import json
import time
import uuid as _uuid
import secrets
from typing import Any, Dict, List, Tuple, Callable, Optional, Awaitable
from dataclasses import dataclass

from gsuid_core.logger import logger
from gsuid_core.models import Event

from .models import AIApprovalRequest

# 裁决后领域动作回调：(row, approved, note) -> 面向调用方的结果文本
ResolveHandler = Callable[[AIApprovalRequest, bool, str], Awaitable[str]]


@dataclass
class ApprovalCategory:
    """一个审批领域：名字 + 裁决回调 + TTL。"""

    name: str
    on_resolve: ResolveHandler
    ttl_seconds: int = 1800


_CATEGORIES: Dict[str, ApprovalCategory] = {}

# 内存待审批标记（visible_when 谓词的廉价内存判定；允许略微过可见）
_PENDING_OPERATORS: set[str] = set()

# 「完全访问」豁免名单：user 级 approval 直接放行（master 级永不豁免）。
# 运行时状态，由前端 / 插件（如画布授权配置）经 set_full_access 维护。
_FULL_ACCESS_USERS: set[str] = set()

# 「完全访问」可插拔解析器：插件可按会话上下文提供更细粒度的判定
# （如画布按 user × canvas 存储授权模式）。返回 None = 不表态，回落默认名单。
_FULL_ACCESS_RESOLVER: Optional[Callable[[str, Optional[Event]], Optional[bool]]] = None

# tool_call 策略门的一次性放行 grant：(user_id, tool_name) -> 过期时间戳
_TOOL_GRANTS: Dict[Tuple[str, str], float] = {}
_TOOL_GRANT_TTL = 600.0


def register_approval_category(name: str, on_resolve: ResolveHandler, ttl_seconds: int = 1800) -> None:
    """注册一个审批领域（同名后写覆盖）。"""
    _CATEGORIES[name] = ApprovalCategory(name=name, on_resolve=on_resolve, ttl_seconds=ttl_seconds)
    logger.debug(f"✅ [Approval] 注册审批领域: {name} (ttl={ttl_seconds}s)")


def is_master(user_id: str) -> bool:
    """是否为机器人主人（委托全框架唯一实现 ``ai_core.utils._is_master_user``）。"""
    from gsuid_core.ai_core.utils import _is_master_user

    return _is_master_user(user_id)


def set_full_access(user_id: str, enabled: bool) -> None:
    """设置某用户的「完全访问」豁免（仅作用于 user 级 approval）。"""
    if enabled:
        _FULL_ACCESS_USERS.add(str(user_id))
    else:
        _FULL_ACCESS_USERS.discard(str(user_id))


def set_full_access_resolver(fn: Optional[Callable[[str, Optional[Event]], Optional[bool]]]) -> None:
    """注册「完全访问」解析器（同步、廉价内存判定；返回 None 回落默认名单）。

    供需要比"全局按用户"更细粒度的调用方使用——如画布插件按
    user × canvas(=ev.group_id) 存储授权模式。传 None 可注销。
    """
    global _FULL_ACCESS_RESOLVER
    _FULL_ACCESS_RESOLVER = fn


def is_full_access(user_id: str, ev: Optional[Event] = None) -> bool:
    if _FULL_ACCESS_RESOLVER is not None:
        try:
            verdict = _FULL_ACCESS_RESOLVER(str(user_id), ev)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"✅ [Approval] 完全访问解析器异常，回落默认名单: {e}")
            verdict = None
        if verdict is not None:
            return verdict
    return str(user_id) in _FULL_ACCESS_USERS


def has_pending(user_id: str) -> bool:
    """该用户名下（或其可裁决范围内）是否可能有待审批项——内存快判，宽松无害。"""
    return str(user_id) in _PENDING_OPERATORS or (bool(_PENDING_OPERATORS) and is_master(user_id))


async def prime_pending() -> None:
    """重启后回填内存待审批标记：否则 DB 有 pending 但 respond/list 工具全被隐藏成死锁。"""
    for uid in await AIApprovalRequest.pending_operator_ids():
        _PENDING_OPERATORS.add(str(uid))


async def submit(
    category: str,
    title: str,
    ev: Optional[Event] = None,
    audience: str = "master",
    interaction: str = "approval",
    ref_key: str = "",
    payload: Optional[Dict[str, Any]] = None,
    operator_user_id: str = "",
    origin_session_id: str = "",
    allow_full_access_exempt: bool = False,
) -> AIApprovalRequest:
    """提交一条审批 / 交互请求，返回落库行（status 可能是 auto_approved）。

    「完全访问」豁免只对 ``audience="user"`` 且调用方显式允许时生效，且照常落
    auto_approved 记录（豁免 ≠ 不记账，审计链完整）。
    """
    operator = operator_user_id or (str(ev.user_id) if ev is not None else "")
    session_id = origin_session_id or (ev.session_id if ev is not None else "")
    status = "pending"
    if allow_full_access_exempt and audience == "user" and interaction == "approval" and is_full_access(operator, ev):
        status = "auto_approved"
    row = await AIApprovalRequest.add(
        request_id=_uuid.uuid4().hex,
        short_id=secrets.token_hex(2),
        interaction=interaction,
        audience=audience,
        category=category,
        ref_key=ref_key,
        origin_session_id=session_id,
        operator_user_id=operator,
        bot_id=ev.bot_id if ev is not None else "",
        bot_self_id=ev.bot_self_id if ev is not None else "",
        user_type=ev.user_type if ev is not None else "direct",
        group_id=ev.group_id if ev is not None else None,
        title=title[:2000],
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        status=status,
    )
    if status == "pending" and operator:
        _PENDING_OPERATORS.add(operator)
    logger.info(f"✅ [Approval] 提交请求 #{row.short_id} category={category} audience={audience} status={status}")
    return row


async def log_question(
    ev: Optional[Event],
    question: str,
    answer: str,
    answered: bool,
    category: str = "ask_user",
) -> None:
    """把一次澄清问答落账本（``interaction="question"``，提交即终态，无裁决语义）。

    answered=True 落 ``approved``（resolved_note=用户回答）；超时落 ``expired``
    （resolved_note=默认值或空）。让 ask_user 与审批共用同一条审计链。
    """
    operator = str(ev.user_id) if ev is not None else ""
    await AIApprovalRequest.add(
        request_id=_uuid.uuid4().hex,
        short_id=secrets.token_hex(2),
        interaction="question",
        audience="user",
        category=category,
        origin_session_id=ev.session_id if ev is not None else "",
        operator_user_id=operator,
        bot_id=ev.bot_id if ev is not None else "",
        bot_self_id=ev.bot_self_id if ev is not None else "",
        user_type=ev.user_type if ev is not None else "direct",
        group_id=ev.group_id if ev is not None else None,
        title=question[:2000],
        status="approved" if answered else "expired",
        resolved_by=operator if answered else "",
        resolved_note=answer[:2000],
        resolved_via="chat",
        resolved_at=int(time.time()),
    )


async def _refresh_pending(operator: str) -> None:
    rows = await AIApprovalRequest.list_pending(operator_user_id=operator)
    if rows:
        _PENDING_OPERATORS.add(operator)
    else:
        _PENDING_OPERATORS.discard(operator)


async def expire_stale() -> None:
    """按各 category 的 TTL 批量过期 pending 请求（list / resolve 前调用）。"""
    for cat in _CATEGORIES.values():
        n = await AIApprovalRequest.expire_stale(cat.name, cat.ttl_seconds)
        if n:
            logger.info(f"✅ [Approval] category={cat.name} 过期清理 {n} 条")


# webconsole 裁决身份：已过控制台登录认证，等同主人权限
CONSOLE_RESOLVER = "webconsole"


def _can_resolve(row: AIApprovalRequest, resolver_user_id: str) -> Tuple[bool, str]:
    """裁决权校验：master 级只认主人；user 级认 operator 本人或主人代裁。"""
    if resolver_user_id == CONSOLE_RESOLVER:
        return True, ""
    if row.audience == "master":
        if not is_master(resolver_user_id):
            return False, "⛔ 该请求需要主人（PM=0）裁决。"
        return True, ""
    if resolver_user_id == row.operator_user_id or is_master(resolver_user_id):
        return True, ""
    return False, "⛔ 该请求只能由发起用户本人（或主人）裁决。"


async def list_pending_for_resolver(resolver_user_id: str) -> List[AIApprovalRequest]:
    """列出某裁决者可见的全部 pending：本人 user 级 + （主人时）所有 master/user 级。"""
    await expire_stale()
    if is_master(resolver_user_id):
        return await AIApprovalRequest.list_pending()
    return await AIApprovalRequest.list_pending(operator_user_id=resolver_user_id, audience="user")


async def locate(resolver_user_id: str, request_ref: str) -> Tuple[Optional[AIApprovalRequest], str]:
    """按 ref 定位待裁决请求；ref 空时名下唯一 pending 直接命中，多条要求指名。"""
    ref = request_ref.strip().lstrip("#")
    if ref:
        row = await AIApprovalRequest.get_by_request_id(ref)
        if row is None:
            row = await AIApprovalRequest.get_by_short_id(ref)
        if row is None or row.status != "pending":
            return None, f"ℹ️ 未找到编号 #{ref} 的待审批请求（可能已处理或失效）。"
        return row, ""
    rows = await list_pending_for_resolver(resolver_user_id)
    if not rows:
        return None, "ℹ️ 当前没有等待你裁决的请求。"
    if len(rows) > 1:
        listing = "、".join(f"#{r.short_id} [{r.category}] {r.title[:40]}" for r in rows[:5])
        return None, f"❓ 存在多个待审批请求（{listing}），请指明编号（如 #{rows[0].short_id}）。"
    return rows[0], ""


async def resolve(
    request_ref: str,
    approved: bool,
    resolver_user_id: str,
    note: str = "",
    via: str = "chat",
) -> str:
    """裁决一条请求：定位 → 权限校验 → 落状态 → 触发 category 领域回调。"""
    await expire_stale()
    row, err = await locate(resolver_user_id, request_ref)
    if err:
        return err
    assert row is not None
    return await resolve_row(row, approved, resolver_user_id, note, via)


async def resolve_row(
    row: AIApprovalRequest,
    approved: bool,
    resolver_user_id: str,
    note: str = "",
    via: str = "webconsole",
) -> str:
    """对已定位的行执行裁决（webconsole / 领域兼容端点直调）。"""
    if row.status != "pending":
        return f"ℹ️ 请求 #{row.short_id} 已是 {row.status}，无需重复裁决。"
    allowed, deny_msg = _can_resolve(row, resolver_user_id)
    if not allowed:
        return deny_msg
    if row.id is None:
        return "⚠️ 请求行缺主键，无法裁决。"
    await AIApprovalRequest.mark(
        row.id, "approved" if approved else "rejected", resolved_by=resolver_user_id, note=note, via=via
    )
    await _refresh_pending(row.operator_user_id)
    cat = _CATEGORIES.get(row.category)
    if cat is None:
        logger.warning(f"✅ [Approval] 请求 #{row.short_id} 的领域 {row.category} 未注册回调，仅落状态")
        return f"{'✅ 已批准' if approved else '🚫 已拒绝'} #{row.short_id}（该领域无后续动作）。"
    try:
        return await cat.on_resolve(row, approved, note)
    except Exception as e:
        logger.exception(f"✅ [Approval] 领域回调 {row.category} 执行异常: {e}")
        return f"⚠️ 裁决已记录（#{row.short_id} {'批准' if approved else '拒绝'}），但后续动作执行失败：{e}"


# ─────────────────────────────────────────────
# tool_call 策略门（@ai_tools(approval=...) 的运行时拦截）
# ─────────────────────────────────────────────


def grant_tool_call(user_id: str, tool_name: str) -> None:
    """发放一次性工具放行 grant（TTL 内首次调用消费）。"""
    _TOOL_GRANTS[(str(user_id), tool_name)] = time.time() + _TOOL_GRANT_TTL


def consume_tool_grant(user_id: str, tool_name: str) -> bool:
    key = (str(user_id), tool_name)
    expiry = _TOOL_GRANTS.pop(key, 0.0)
    return expiry > time.time()


async def _on_resolve_tool_call(row: AIApprovalRequest, approved: bool, note: str) -> str:
    """tool_call 领域回调：批准 → 发放一次性 grant，由 Agent 重新调用该工具。"""
    if not approved:
        return f"🚫 已拒绝工具调用请求 #{row.short_id}（{row.ref_key}）。{note}".strip()
    grant_tool_call(row.operator_user_id, row.ref_key)
    return (
        f"✅ 已批准工具调用 #{row.short_id}：`{row.ref_key}` 获得一次性放行"
        f"（{int(_TOOL_GRANT_TTL // 60)} 分钟内有效）。请重新调用该工具完成原操作。"
    )


async def _on_resolve_agent_request(row: AIApprovalRequest, approved: bool, note: str) -> str:
    """agent_request 领域回调：Agent 主动请求（request_user/master_approval）的裁决只落结论。"""
    verdict = "✅ 已批准" if approved else "🚫 已拒绝"
    return f"{verdict} #{row.short_id}：{row.title[:80]}。{note}".strip()


async def tool_call_gate(ev: Optional[Event], tool_name: str, tier: str, args_repr: str) -> Optional[str]:
    """工具执行前的强制审批闸门。返回 None=放行；返回字符串=拦截并把该文本回给模型。

    完全访问豁免只覆盖 user 级；无 ev 的后台调用（无归属人）放行并记 debug——
    后台链路的权限由各自 check_func 承担。
    """
    if ev is None:
        logger.debug(f"✅ [Approval] 工具 {tool_name} 无 ev 上下文，策略门放行（后台链路）")
        return None
    operator = str(ev.user_id)
    if tier == "user" and is_full_access(operator, ev):
        await submit(
            category="tool_call",
            title=f"完全访问自动放行: {tool_name}",
            ev=ev,
            audience="user",
            ref_key=tool_name,
            payload={"args": args_repr[:2000]},
            allow_full_access_exempt=True,
        )
        return None
    if consume_tool_grant(operator, tool_name):
        return None
    who = "主人" if tier == "master" else "当前用户"
    # 同一 (operator, tool) 复用现有 pending：弱模型一轮内重试同一工具不重复开票
    existing = await AIApprovalRequest.list_pending(operator_user_id=operator, category="tool_call", ref_key=tool_name)
    if existing:
        return (
            f"⏳ 工具 `{tool_name}` 的审批 #{existing[0].short_id} 仍在等待{who}裁决，"
            f"请勿重复调用；把这件事转告{who}并请其回复同意 / 拒绝。"
        )
    row = await submit(
        category="tool_call",
        title=f"工具调用请求: {tool_name}（{args_repr[:120]}）",
        ev=ev,
        audience="master" if tier == "master" else "user",
        ref_key=tool_name,
        payload={"args": args_repr[:2000]},
    )
    return (
        f"⏳ 工具 `{tool_name}` 需要{who}授权，已提交审批 #{row.short_id}。"
        f"请把这件事转告{who}并请其回复同意 / 拒绝；获批后重新调用本工具即可执行。"
    )


def register_builtin_categories() -> None:
    """注册框架内置的通用审批领域（tool_call / agent_request）。"""
    register_approval_category("tool_call", _on_resolve_tool_call, ttl_seconds=1800)
    register_approval_category("agent_request", _on_resolve_agent_request, ttl_seconds=3600)
