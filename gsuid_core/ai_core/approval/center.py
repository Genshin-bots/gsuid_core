"""统一审批中心（ApprovalCenter）。

全框架只有两个审批动词：``submit``（提交请求）与 ``resolve``（裁决）。裁决入口
是私聊确认码和 webconsole（含 Kanban 看板兼容端点）。批准之后干什么由 category
的 ``on_resolve`` 承担。带确认码的新票批准后回灌调用会话；没有哈希的旧票仍走
原来的回调（含 kick_root），不回灌。

三种审批 =（interaction × audience）三个合法组合：
- ``question × user``   ：ask_user 澄清（超时走默认，无权限语义）；
- ``approval × user``   ：花用户自己的资源（可被「完全访问」豁免，仍留 auto 记录）；
- ``approval × master`` ：敏感权限（装插件 / 执行命令 / 花主人的钱），**永不可豁免**。
"""

import json
import time
import uuid as _uuid
import secrets
from typing import Dict, List, Tuple, Callable, Optional, Awaitable, TypedDict
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event

from .halt import APPROVAL_HOLD_MARK, DEFAULT_APPROVAL_TTL, halt_line, action_failed
from .codes import hashes_equal, draw_confirm_code, hash_confirm_code, looks_like_confirm_code
from .models import AIApprovalRequest
from .session_ctx import note_pending_session, clear_pending_session, current_caller_session_id

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
# 运行时状态，由前端 / 插件经 set_full_access 维护。
_FULL_ACCESS_USERS: set[str] = set()

# 「完全访问」可插拔解析器：插件可按会话上下文提供更细粒度的判定
# （如按 user × 会话键存储授权模式）。返回 None = 不表态，回落默认名单。
_FULL_ACCESS_RESOLVER: Optional[Callable[[str, Optional[Event]], Optional[bool]]] = None


class ApprovalPayload(TypedDict, total=False):
    """提交时冻结的快照。只收审批链路会读的键。"""

    parent_session_id: str
    ws_bot_id: str
    args: str
    raw: str
    argv: list[str]
    risk: str
    reason: str
    task_id: str
    root_task_id: str
    agent_profile: str
    plugin_name: str
    plugin_action: str
    # 领域扩展字段（如画布业务审批）
    canvas_id: str
    node_ids: list[str]
    account_id: int
    chat_session: str
    point_cost: int


@dataclass
class _ToolGrant:
    expires_at: float
    args_repr: str


# tool_call 策略门的一次性放行。参数必须和批准时的那一份相同。
_TOOL_GRANTS: Dict[Tuple[str, str], _ToolGrant] = {}
_TOOL_GRANT_TTL = 600.0


def register_approval_category(name: str, on_resolve: ResolveHandler, ttl_seconds: int = 1800) -> None:
    """注册一个审批领域（同名后写覆盖）。"""
    _CATEGORIES[name] = ApprovalCategory(name=name, on_resolve=on_resolve, ttl_seconds=ttl_seconds)
    logger.debug(t("log.ai.approval_registered_domain_name", name=name, ttl_seconds=ttl_seconds))


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

    供需要比"全局按用户"更细粒度的调用方使用——例如插件按
    user × 会话键（ev.group_id）存储授权模式。传 None 可注销。
    """
    global _FULL_ACCESS_RESOLVER
    _FULL_ACCESS_RESOLVER = fn


def is_full_access(user_id: str, ev: Optional[Event] = None) -> bool:
    if _FULL_ACCESS_RESOLVER is not None:
        try:
            verdict = _FULL_ACCESS_RESOLVER(str(user_id), ev)
        except Exception as e:  # noqa: BLE001
            logger.debug(t("log.ai.approval_full_access_resolver_fail", e=e))
            verdict = None
        if verdict is not None:
            return verdict
    return str(user_id) in _FULL_ACCESS_USERS


def has_pending(user_id: str) -> bool:
    """该用户名下（或其可裁决范围内）是否可能有待审批项——内存快判，宽松无害。"""
    return str(user_id) in _PENDING_OPERATORS or (bool(_PENDING_OPERATORS) and is_master(user_id))


@dataclass
class _SubmitMeta:
    delivered: bool
    reused: bool


_SUBMIT_META: Dict[str, _SubmitMeta] = {}
_TOOL_BODIES: Dict[str, Callable[[], Awaitable[object]]] = {}


def submit_meta(request_id: str) -> Tuple[bool, bool]:
    """(私聊是否发出, 是否复用了已有 pending)。"""
    meta = _SUBMIT_META[request_id] if request_id in _SUBMIT_META else None
    if meta is None:
        return False, False
    return meta.delivered, meta.reused


def note_submit_reused(request_id: str) -> None:
    """调用方自己命中了旧票、没有再走 submit 时，记下「复用」。"""
    if request_id:
        _SUBMIT_META[request_id] = _SubmitMeta(False, True)


def remember_tool_body(request_id: str, body: Callable[[], Awaitable[object]]) -> None:
    _TOOL_BODIES[request_id] = body


async def prime_pending() -> None:
    """重启后回填内存待审批标记，供 check_approval 的可见性使用。"""
    for uid in await AIApprovalRequest.pending_operator_ids():
        _PENDING_OPERATORS.add(str(uid))
    for sid in await AIApprovalRequest.pending_origin_session_ids():
        note_pending_session(sid)


def _merge_payload(
    payload: Optional[ApprovalPayload],
    ev: Optional[Event],
    parent_session_id: str,
) -> ApprovalPayload:
    body: ApprovalPayload = {}
    if payload is not None:
        body.update(payload)
    parent = parent_session_id or (ev.session_id if ev is not None else "")
    if parent and "parent_session_id" not in body:
        body["parent_session_id"] = parent
    ws = ev.WS_BOT_ID if ev is not None and ev.WS_BOT_ID else ""
    if ws and "ws_bot_id" not in body:
        body["ws_bot_id"] = ws
    return body


def _operator_pm(ev: Optional[Event], operator_user_pm: Optional[int]) -> int:
    if operator_user_pm is not None:
        return operator_user_pm
    if ev is not None:
        return ev.user_pm
    return 6


async def _fresh_short_id() -> str:
    """8 位十六进制。撞上已有短号就重抽。"""
    for _ in range(8):
        sid = secrets.token_hex(4)
        if await AIApprovalRequest.get_by_short_id(sid) is None:
            return sid
    return secrets.token_hex(4)


async def _fresh_code_hash() -> Tuple[str, str]:
    code = draw_confirm_code()
    digest = hash_confirm_code(code)
    for _ in range(8):
        taken = await AIApprovalRequest.get_by_code_hash(digest)
        if taken is None:
            return code, digest
        code = draw_confirm_code()
        digest = hash_confirm_code(code)
    return code, digest


async def submit(
    category: str,
    title: str,
    ev: Optional[Event] = None,
    audience: str = "master",
    interaction: str = "approval",
    ref_key: str = "",
    payload: Optional[ApprovalPayload] = None,
    operator_user_id: str = "",
    operator_user_pm: Optional[int] = None,
    origin_session_id: str = "",
    allow_full_access_exempt: bool = False,
    dedupe_key: str = "",
    effect: str = "",
    ttl_seconds: Optional[int] = None,
    parent_session_id: str = "",
    bot_id: str = "",
    bot_self_id: str = "",
    user_type: str = "",
    group_id: Optional[str] = None,
) -> AIApprovalRequest:
    """提交一条审批请求。新票默认 600 秒，并私聊确认码。

    同一 dedupe_key 已有 pending 时返回旧行，不发第二条私聊。
    「完全访问」豁免只对 audience="user" 且调用方显式允许时生效。
    调用会话优先用 origin_session_id，否则用当前 agent 会话，最后才是 Event。
    """
    operator = operator_user_id or (str(ev.user_id) if ev is not None else "")
    caller = origin_session_id or current_caller_session_id()
    if not caller and ev is not None:
        caller = ev.session_id
    status = "pending"
    if allow_full_access_exempt and audience == "user" and interaction == "approval" and is_full_access(operator, ev):
        status = "auto_approved"
    if status == "pending" and dedupe_key:
        existing = await AIApprovalRequest.get_pending_by_dedupe(dedupe_key)
        if existing is not None:
            _SUBMIT_META[existing.request_id] = _SubmitMeta(False, True)
            note_pending_session(existing.origin_session_id)
            return existing
    code = ""
    digest = ""
    expires_at = 0
    if status == "pending" and interaction == "approval":
        code, digest = await _fresh_code_hash()
        ttl = ttl_seconds if ttl_seconds is not None and ttl_seconds > 0 else DEFAULT_APPROVAL_TTL
        expires_at = int(time.time()) + ttl
    body = _merge_payload(payload, ev, parent_session_id)
    row = await AIApprovalRequest.add(
        request_id=_uuid.uuid4().hex,
        short_id=await _fresh_short_id(),
        interaction=interaction,
        audience=audience,
        category=category,
        ref_key=ref_key,
        origin_session_id=caller,
        operator_user_id=operator,
        operator_user_pm=_operator_pm(ev, operator_user_pm),
        bot_id=ev.bot_id if ev is not None else bot_id,
        bot_self_id=ev.bot_self_id if ev is not None else bot_self_id,
        user_type=ev.user_type if ev is not None else (user_type or "direct"),
        group_id=ev.group_id if ev is not None else group_id,
        title=title[:2000],
        payload_json=json.dumps(body, ensure_ascii=False),
        status=status,
        confirm_code_hash=digest,
        dedupe_key=dedupe_key[:512],
        effect=effect[:2000],
        expires_at=expires_at,
    )
    if status == "pending" and operator:
        _PENDING_OPERATORS.add(operator)
    delivered = False
    if status == "pending" and code:
        from .notify import send_confirm_dm

        delivered = await send_confirm_dm(row, code)
        note_pending_session(caller)
    _SUBMIT_META[row.request_id] = _SubmitMeta(delivered, False)
    logger.info(
        t(
            "log.ai.approval_submitted_request_category",
            p0=row.short_id,
            category=category,
            audience=audience,
            status=status,
        )
    )
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
        short_id=await _fresh_short_id(),
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


async def _drop_session_if_clear(origin_session_id: str) -> None:
    if not origin_session_id:
        return
    left = await AIApprovalRequest.list_pending_for_origin(origin_session_id)
    if not left:
        clear_pending_session(origin_session_id)


async def _fail_linked_task(row: AIApprovalRequest, reason: str, *, rejected: bool = True) -> None:
    """把还挂着的 Kanban 任务标失败。拒绝走审批日志，执行失败只记失败原因。"""
    if row.category != "kanban_subtask" or not row.ref_key:
        return
    from gsuid_core.ai_core.planning import kanban
    from gsuid_core.ai_core.planning.models import AIAgentTask

    task = await AIAgentTask.get_by_id(row.ref_key)
    if task is None or task.status in ("failed", "completed", "cancelled"):
        return
    if rejected:
        if task.status != "waiting_approval":
            return
        await kanban.approve_subtask(task, False, reason)
        return
    await kanban.mark_subtask_failed(task, reason[:1000])


async def _expire_one(row: AIApprovalRequest) -> bool:
    if row.id is None:
        return False
    claimed = await AIApprovalRequest.claim_status(row.id, "expired", note="超时", via="timer")
    if not claimed:
        return False
    row.status = "expired"
    await _refresh_pending(row.operator_user_id)
    await _drop_session_if_clear(row.origin_session_id)
    _TOOL_BODIES.pop(row.request_id, None)
    if row.confirm_code_hash:
        await _fail_linked_task(row, "超时")
        from .notify import notify_closed

        await notify_closed(row, expired=True)
    logger.info(t("log.ai.approval_category_expired_entries_cleanup", p0=row.category, n=1))
    return True


def _row_is_due(row: AIApprovalRequest, now: int) -> bool:
    """这一行现在就该过期。新票看 expires_at，旧票看 category TTL。"""
    if row.status != "pending":
        return False
    if row.expires_at > 0:
        return row.expires_at <= now
    return _legacy_is_due(row, now)


def _legacy_is_due(row: AIApprovalRequest, now: int) -> bool:
    """旧票 expires_at 为空，沿用该 category 原来的 TTL。"""
    if row.expires_at > 0 or row.created_at <= 0:
        return False
    cat = _CATEGORIES.get(row.category)
    ttl = cat.ttl_seconds if cat is not None else 0
    if ttl <= 0:
        return False
    return now - row.created_at >= ttl


async def expire_due() -> int:
    """30 秒扫描和 list/resolve 共用这一条过期路径。"""
    now = int(time.time())
    n = 0
    for row in await AIApprovalRequest.list_due_coded(now):
        if await _expire_one(row):
            n += 1
    for row in await AIApprovalRequest.list_legacy_pending():
        if _legacy_is_due(row, now) and await _expire_one(row):
            n += 1
    return n


async def expire_stale() -> None:
    """list / resolve 前调用。新票看 expires_at，旧票看 category TTL。"""
    await expire_due()


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
            row = await AIApprovalRequest.get_pending_by_short_id(ref)
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
    """对已定位的行执行裁决。新票批准后回灌；拒绝和旧票不回灌。"""
    if row.status != "pending":
        return f"ℹ️ 请求 #{row.short_id} 已是 {row.status}，无需重复裁决。"
    allowed, deny_msg = _can_resolve(row, resolver_user_id)
    if not allowed:
        return deny_msg
    if row.id is None:
        return "⚠️ 请求行缺主键，无法裁决。"
    if _row_is_due(row, int(time.time())):
        await _expire_one(row)
        return f"ℹ️ #{row.short_id} 已超时，未执行。"
    new_status = "approved" if approved else "rejected"
    claimed = await AIApprovalRequest.claim_status(row.id, new_status, resolved_by=resolver_user_id, note=note, via=via)
    if not claimed:
        return f"ℹ️ 请求 #{row.short_id} 已裁决。"
    row.status = new_status
    await _refresh_pending(row.operator_user_id)
    await _drop_session_if_clear(row.origin_session_id)
    cat = _CATEGORIES.get(row.category)
    text = ""
    if cat is None:
        logger.warning(t("log.ai.approval_request_domain_registered_register", p0=row.short_id, p1=row.category))
        text = f"{'✅ 已批准' if approved else '🚫 已拒绝'} #{row.short_id}（该领域无后续动作）。"
        if not approved:
            await _fail_linked_task(row, note or "拒绝")
    else:
        try:
            text = await cat.on_resolve(row, approved, note)
        except Exception as e:
            logger.exception(t("log.ai.approval_domain_callback_fail", p0=row.category, e=e))
            await _fail_linked_task(row, f"{type(e).__name__}: {e}", rejected=False)
            return f"⚠️ 裁决已记录（#{row.short_id} {'批准' if approved else '拒绝'}），但后续动作执行失败：{e}"
    if not row.confirm_code_hash:
        return text
    if not approved:
        from .notify import notify_closed

        await notify_closed(row, expired=False)
        return text
    if action_failed(text):
        await _fail_linked_task(row, text, rejected=False)
        return text
    if text.startswith(APPROVAL_HOLD_MARK):
        return text[len(APPROVAL_HOLD_MARK) :]
    from .resume import reinject_approved

    await reinject_approved(row, text)
    return text


# --- tool_call 策略门（@ai_tools(approval=...) 的运行时拦截） ---


def grant_tool_call(user_id: str, tool_name: str, args_repr: str = "") -> None:
    """发放一次性工具放行。只认批准时冻结的那份参数。"""
    _TOOL_GRANTS[(str(user_id), tool_name)] = _ToolGrant(time.time() + _TOOL_GRANT_TTL, args_repr)


def consume_tool_grant(user_id: str, tool_name: str, args_repr: str = "") -> bool:
    key = (str(user_id), tool_name)
    if key not in _TOOL_GRANTS:
        return False
    grant = _TOOL_GRANTS[key]
    if grant.expires_at <= time.time():
        del _TOOL_GRANTS[key]
        return False
    if grant.args_repr != args_repr:
        return False
    del _TOOL_GRANTS[key]
    return True


async def _on_resolve_tool_call(row: AIApprovalRequest, approved: bool, note: str) -> str:
    """tool_call：批准则执行提交时冻住的那一次函数体。"""
    body = _TOOL_BODIES.pop(row.request_id, None)
    if not approved:
        return f"已拒绝 #{row.short_id}（{row.ref_key}）。{note}".strip()
    if body is None:
        from .notify import _payload_str

        grant_tool_call(row.operator_user_id, row.ref_key, _payload_str(row.payload_json, "args"))
        return f"已批准 #{row.short_id}。原来的调用不在这个进程里了。用相同参数再调用一次该工具会放行。"
    result = await body()
    if isinstance(result, str):
        return result
    return f"已执行 {row.ref_key}。"


async def _on_resolve_agent_request(row: AIApprovalRequest, approved: bool, note: str) -> str:
    """agent_request：结论交给回灌，不在这里再跑一轮计划。"""
    verdict = "已批准" if approved else "已拒绝"
    extra = f"。{note}" if note else ""
    return f"{verdict} #{row.short_id}：{row.title[:80]}{extra}"


def _sender_may_confirm(row: AIApprovalRequest, user_id: str) -> bool:
    if row.audience == "master":
        return is_master(user_id)
    return user_id == row.operator_user_id


async def match_confirm_message(ev: Event) -> bool:
    """私聊整句等于某条确认码时裁决并返回 True。群消息和普通句子返回 False。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return False
    if ev.user_type != "direct":
        return False
    body = (ev.raw_text or "").strip()
    if not looks_like_confirm_code(body):
        return False
    digest = hash_confirm_code(body)
    row = await AIApprovalRequest.get_by_code_hash(digest)
    if row is None or not hashes_equal(row.confirm_code_hash, digest):
        return False
    if not _sender_may_confirm(row, str(ev.user_id)):
        return False
    from .notify import ack_direct

    ws = ev.WS_BOT_ID or ""
    if row.status != "pending":
        await ack_direct(ev.bot_id, ev.bot_self_id or "", str(ev.user_id), ws, f"#{row.short_id} 已是 {row.status}。")
        return True
    msg = await resolve_row(row, True, str(ev.user_id), note="确认码", via="chat")
    ack = msg if msg.startswith(("ℹ️", "⛔", "⚠️", "❌", "🚫", "安装失败")) else f"已确认 #{row.short_id}。"
    await ack_direct(ev.bot_id, ev.bot_self_id or "", str(ev.user_id), ws, ack)
    return True


async def tool_call_gate(
    ev: Optional[Event],
    tool_name: str,
    tier: str,
    args_repr: str,
    run_body: Optional[Callable[[], Awaitable[object]]] = None,
) -> Optional[str]:
    """工具执行前的审批闸。返回 None=放行；返回字符串=挂起，运行器应结束本轮。"""
    if ev is None:
        logger.debug(t("log.ai.approval_name_ev_context_policy", tool_name=tool_name))
        return None
    operator = str(ev.user_id)
    args_key = args_repr[:2000]
    if tier == "user" and is_full_access(operator, ev):
        await submit(
            category="tool_call",
            title=f"完全访问自动放行: {tool_name}",
            ev=ev,
            audience="user",
            ref_key=tool_name,
            payload={"args": args_key},
            allow_full_access_exempt=True,
        )
        return None
    if consume_tool_grant(operator, tool_name, args_key):
        return None
    dedupe = f"tool:{tool_name}:{operator}"
    existing = await AIApprovalRequest.get_pending_by_dedupe(dedupe)
    if existing is not None:
        return halt_line(existing.short_id, delivered=False, reused=True)
    row = await submit(
        category="tool_call",
        title=f"工具调用请求: {tool_name}（{args_repr[:120]}）",
        ev=ev,
        audience="master" if tier == "master" else "user",
        ref_key=tool_name,
        payload={"args": args_key},
        dedupe_key=dedupe,
        effect=f"调用工具 {tool_name}",
    )
    if row.status != "pending":
        return None
    if run_body is not None:
        remember_tool_body(row.request_id, run_body)
    delivered, reused = submit_meta(row.request_id)
    return halt_line(row.short_id, delivered=delivered, reused=reused)


async def plugin_install_block_reason() -> str:
    """有未完成的插件安装审批时，create_subagent 用这段拒绝再派。"""
    rows = await AIApprovalRequest.list_pending_plugin_installs()
    if not rows:
        return ""
    row = rows[0]
    name = row.dedupe_key.split(":", 1)[1] if ":" in row.dedupe_key else row.title[:40]
    return f"已有插件安装在等主人确认（#{row.short_id} {name}）。不要再派开发任务。"


def register_builtin_categories() -> None:
    """注册框架内置的通用审批领域（tool_call / agent_request）。"""
    register_approval_category("tool_call", _on_resolve_tool_call, ttl_seconds=1800)
    register_approval_category("agent_request", _on_resolve_agent_request, ttl_seconds=3600)
