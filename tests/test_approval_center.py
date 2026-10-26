"""统一审批中心单测：裁决权矩阵 × 完全访问豁免 × tool_call 策略门 × v1→v2 迁移。

覆盖：
- _can_resolve: webconsole / master 级 / user 级（本人、他人、主人代裁）的允许与拒绝
- is_full_access / set_full_access / set_full_access_resolver 的判定与回落
- submit: 完全访问豁免只作用于 user 级 approval 且调用方显式允许，master 级永不豁免
- grant_tool_call / consume_tool_grant: 一次性消费与过期
- tool_call_gate: 同 (operator, tool) 的 pending 复用（不重复开票）
- persistence._dto_to_node: v1 旧画像 JSON 自动迁移为 AgentNode v2

测试用 asyncio.run 包装（不依赖 pytest-asyncio），DB 访问全部 mock。
"""

import time
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

CENTER = "gsuid_core.ai_core.approval.center"


def _run(coro):
    return asyncio.run(coro)


def _row(audience: str = "master", operator: str = "u1", status: str = "pending", short_id: str = "ab12"):
    row = MagicMock()
    row.audience = audience
    row.operator_user_id = operator
    row.status = status
    row.short_id = short_id
    return row


# --- _can_resolve 裁决权矩阵 ---
def test_can_resolve_webconsole_always_allowed():
    from gsuid_core.ai_core.approval.center import CONSOLE_RESOLVER, _can_resolve

    allowed, _ = _can_resolve(_row(audience="master"), CONSOLE_RESOLVER)
    assert allowed
    allowed, _ = _can_resolve(_row(audience="user", operator="someone"), CONSOLE_RESOLVER)
    assert allowed
    print("[OK] webconsole 登录态等同主人，master/user 级均可裁决")


def test_can_resolve_master_audience():
    from gsuid_core.ai_core.approval.center import _can_resolve

    with patch(f"{CENTER}.is_master", side_effect=lambda uid: uid == "master_1"):
        allowed, msg = _can_resolve(_row(audience="master", operator="u1"), "u1")
        assert not allowed and "主人" in msg
        allowed, _ = _can_resolve(_row(audience="master", operator="u1"), "master_1")
        assert allowed
    print("[OK] master 级：发起者本人被拒，仅主人可裁决")


def test_can_resolve_user_audience():
    from gsuid_core.ai_core.approval.center import _can_resolve

    with patch(f"{CENTER}.is_master", side_effect=lambda uid: uid == "master_1"):
        allowed, _ = _can_resolve(_row(audience="user", operator="u1"), "u1")
        assert allowed
        allowed, msg = _can_resolve(_row(audience="user", operator="u1"), "u2")
        assert not allowed and "本人" in msg
        allowed, _ = _can_resolve(_row(audience="user", operator="u1"), "master_1")
        assert allowed
    print("[OK] user 级：本人可裁、他人被拒、主人可代裁")


# --- 完全访问豁免 ---
def test_full_access_toggle_and_resolver():
    from gsuid_core.ai_core.approval import center

    center.set_full_access("fa_user", True)
    assert center.is_full_access("fa_user")
    center.set_full_access("fa_user", False)
    assert not center.is_full_access("fa_user")

    # 解析器优先：返回 True/False 直接生效，返回 None 回落默认名单
    center.set_full_access("fa_user", True)
    center.set_full_access_resolver(lambda uid, ev: False if uid == "fa_user" else None)
    assert not center.is_full_access("fa_user")
    center.set_full_access_resolver(lambda uid, ev: None)
    assert center.is_full_access("fa_user")
    center.set_full_access_resolver(None)
    center.set_full_access("fa_user", False)
    print("[OK] 完全访问名单开关 + 可插拔解析器（None 回落默认）")


def _submit_status(audience: str, allow_exempt: bool, operator: str) -> str:
    """跑一次 submit（mock 落库），返回写库时的 status。"""
    from gsuid_core.ai_core.approval import center

    captured = {}

    async def fake_add(**kw):
        captured.update(kw)
        row = _row()
        row.short_id = "xx99"
        row.request_id = "req-submit"
        return row

    with (
        patch(f"{CENTER}.AIApprovalRequest") as mock_model,
        patch("gsuid_core.ai_core.approval.notify.send_confirm_dm", new=AsyncMock(return_value=True)),
    ):
        mock_model.add = AsyncMock(side_effect=fake_add)
        mock_model.get_by_code_hash = AsyncMock(return_value=None)
        mock_model.get_by_short_id = AsyncMock(return_value=None)
        mock_model.get_pending_by_dedupe = AsyncMock(return_value=None)
        _run(
            center.submit(
                category="agent_request",
                title="t",
                ev=None,
                audience=audience,
                operator_user_id=operator,
                allow_full_access_exempt=allow_exempt,
            )
        )
    return str(captured["status"])


def test_submit_full_access_exemption_matrix():
    from gsuid_core.ai_core.approval import center

    center.set_full_access("fa_user", True)
    try:
        assert _submit_status("user", True, "fa_user") == "auto_approved"
        assert _submit_status("user", False, "fa_user") == "pending"  # 调用方未显式允许
        assert _submit_status("master", True, "fa_user") == "pending"  # master 级永不豁免
        assert _submit_status("user", True, "nobody") == "pending"  # 未开完全访问
    finally:
        center.set_full_access("fa_user", False)
    print("[OK] 豁免矩阵：仅 user 级 × 显式允许 × 已开完全访问 → auto_approved")


# --- tool_call 一次性 grant 与策略门去重 ---
def test_tool_grant_consume_once_and_expiry():
    from gsuid_core.ai_core.approval import center

    center.grant_tool_call("u1", "paint_tool", "args-a")
    assert not center.consume_tool_grant("u1", "paint_tool", "args-b")
    assert center.consume_tool_grant("u1", "paint_tool", "args-a")
    assert not center.consume_tool_grant("u1", "paint_tool", "args-a")

    center.grant_tool_call("u1", "paint_tool", "args-a")
    center._TOOL_GRANTS[("u1", "paint_tool")].expires_at = time.time() - 1
    assert not center.consume_tool_grant("u1", "paint_tool", "args-a")
    print("[OK] grant 只放行相同参数，一次性消费，过期失效")


def test_tool_call_gate_reuses_pending():
    from gsuid_core.ai_core.approval import center

    ev = MagicMock(user_id="u1")
    with patch(f"{CENTER}.AIApprovalRequest") as mock_model, patch(f"{CENTER}.submit", new=AsyncMock()) as mock_submit:
        mock_model.get_pending_by_dedupe = AsyncMock(return_value=_row(short_id="ab12"))
        msg = _run(center.tool_call_gate(ev, "paint_tool", "user", "{}"))
    assert msg is not None and msg.startswith("[[APPROVAL_HALT]]") and "ab12" in msg
    mock_submit.assert_not_awaited()
    print("[OK] 同 (operator, tool) 的 pending 复用，不重复开票")


def test_tool_call_gate_no_ev_passes():
    from gsuid_core.ai_core.approval import center

    assert _run(center.tool_call_gate(None, "paint_tool", "user", "{}")) is None
    print("[OK] 无 ev 的后台链路放行（权限由各自 check_func 承担）")


# --- 问答留档（interaction="question"） ---
def test_log_question_ledger_shapes():
    from gsuid_core.ai_core.approval import center

    captured = {}

    async def fake_add(**kw):
        captured.update(kw)
        return _row()

    ev = MagicMock(user_id="u1", session_id="s1", bot_id="b", bot_self_id="bs", user_type="group", group_id="g1")
    with patch(f"{CENTER}.AIApprovalRequest") as mock_model:
        mock_model.add = AsyncMock(side_effect=fake_add)
        mock_model.get_by_short_id = AsyncMock(return_value=None)
        _run(center.log_question(ev, "画面比例?", "16:9", answered=True))
    assert captured["interaction"] == "question"
    assert captured["status"] == "approved"
    assert captured["resolved_note"] == "16:9"
    assert captured["resolved_by"] == "u1"

    with patch(f"{CENTER}.AIApprovalRequest") as mock_model:
        mock_model.add = AsyncMock(side_effect=fake_add)
        mock_model.get_by_short_id = AsyncMock(return_value=None)
        _run(center.log_question(ev, "画面比例?", "", answered=False))
    assert captured["status"] == "expired"
    assert captured["resolved_by"] == ""
    print("[OK] 问答留档：回答→approved、超时→expired，账本形状正确")


# --- respawn 达上限：必须经统一入口开中心票据 ---
def test_respawn_limit_opens_center_ticket():
    from gsuid_core.ai_core.planning import kanban

    task = MagicMock()
    task.id = "sub_1"
    task.status = "failed"
    task.respawn_count = 3
    task.failure_reason = "第三方 API 连续超时"

    with patch("gsuid_core.ai_core.planning.kanban.request_subtask_approval", new=AsyncMock()) as mock_req:
        ok, msg = _run(kanban.respawn_child_task(task, respawn_limit=3))
    assert not ok and "待审批" in msg
    mock_req.assert_awaited_once()
    assert mock_req.await_args is not None
    prompt = mock_req.await_args.args[1]
    assert "重派次数达上限" in prompt and "API 连续超时" in prompt
    print("[OK] respawn 达上限走 request_subtask_approval（开票+挂起统一入口）")


# --- persistence: v1 旧画像 JSON → AgentNode v2 自动迁移 ---
def test_dto_to_node_migrates_v1():
    from gsuid_core.ai_core.capability_agents.persistence import _dto_to_node

    v1 = {
        "profile_id": "stock_agent",
        "display_name": "股票代理",
        "system_prompt": "你是股票代理",
        "when_to_use": "炒股",
        "match_keywords": ["股票"],
        "tool_names": ["send_stock_info"],
        "max_iterations": 12,
        "max_tokens": 20000,
    }
    node = _dto_to_node(v1)
    assert node is not None
    assert node.node_id == "stock_agent"
    assert node.prompt == "你是股票代理"
    assert node.tool_packs == ["task_basics"]  # v1 无 packs → 补默认
    assert node.tool_names == ["send_stock_info"]
    assert node.source == "user"
    assert not hasattr(node, "max_iterations")  # 预算字段已抹平
    print("[OK] v1 画像 JSON 迁移：字段改名 + 默认 packs + 预算丢弃")


def test_confirm_code_shape_and_hash():
    from gsuid_core.ai_core.approval.codes import (
        hashes_equal,
        draw_confirm_code,
        hash_confirm_code,
        looks_like_confirm_code,
    )

    alphabet = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    for sample in ("同意", "ok", "AB12CD 同意", "同意AB2CDE", "ABCDEF", "234567", "0O1IAB", "ab12"):
        assert not looks_like_confirm_code(sample)
    code = draw_confirm_code()
    assert looks_like_confirm_code(code)
    assert set(code) <= alphabet
    assert any(ch.isalpha() for ch in code) and any(ch.isdigit() for ch in code)
    digest = hash_confirm_code(code.lower())
    assert digest == hash_confirm_code(code)
    assert hashes_equal(digest, digest)
    assert not hashes_equal(digest, "")
    assert not hashes_equal("", digest)
    print("[OK] 确认码：整句、字母数字混合、大小写同一哈希")


def test_submit_dedupe_does_not_add():
    from gsuid_core.ai_core.approval import center

    existing = _row(short_id="ab12")
    existing.request_id = "req-old"
    existing.origin_session_id = "sess-1"
    with patch(f"{CENTER}.AIApprovalRequest") as mock_model:
        mock_model.get_pending_by_dedupe = AsyncMock(return_value=existing)
        mock_model.add = AsyncMock()
        row = _run(
            center.submit(
                category="kanban_subtask",
                title="装插件",
                ev=None,
                dedupe_key="plugin_install:demo",
            )
        )
    assert row is existing
    mock_model.add.assert_not_awaited()
    delivered, reused = center.submit_meta("req-old")
    assert reused and not delivered
    print("[OK] 同一去重键不另开票、不另发私聊")


def test_resume_card_is_not_a_delivery_frame():
    from gsuid_core.ai_core.approval.halt import halt_line, text_is_approval_halt
    from gsuid_core.ai_core.control.lines import approval_resume_line

    card = approval_resume_line(
        task_id="task-9",
        ordinal=3,
        display_name="安装",
        goal_line="把插件装上",
        short_id="ab12",
        effect="复制到 plugins/demo/",
        happened="已复制",
        phase="installed",
        next_step="加载，然后自测",
        handle_line="无句柄，正文如下",
    )
    assert card.startswith("审批续作。")
    assert "task-9" in card
    for banned in ("[框架·任务完成]", "【子任务交付", "（系统：", "（系统校验", "[框架·"):
        assert not card.startswith(banned)
    halted = halt_line("ab12", delivered=True, reused=False)
    assert text_is_approval_halt(halted)
    assert not text_is_approval_halt("同意")
    print("[OK] 续作卡不是交付帧；挂起标记能被运行器认出")


def _resolve_row(hash_value: str, callback_text: str) -> tuple[str, AsyncMock]:
    from gsuid_core.ai_core.approval import center

    row = _row()
    row.id = 7
    row.request_id = "req-7"
    row.category = "unit_confirm"
    row.confirm_code_hash = hash_value
    row.operator_user_id = "u1"
    row.origin_session_id = "sess-1"
    row.ref_key = ""
    row.expires_at = int(time.time()) + 600
    row.created_at = int(time.time())

    async def _cb(req, approved: bool, note: str) -> str:
        del req, approved, note
        return callback_text

    center.register_approval_category("unit_confirm", _cb, ttl_seconds=60)
    with (
        patch(f"{CENTER}._can_resolve", return_value=(True, "")),
        patch(f"{CENTER}._refresh_pending", new=AsyncMock()),
        patch(f"{CENTER}._drop_session_if_clear", new=AsyncMock()),
        patch(f"{CENTER}.AIApprovalRequest.claim_status", new=AsyncMock(return_value=True)),
        patch("gsuid_core.ai_core.approval.resume.reinject_approved", new=AsyncMock()) as reinject,
    ):
        msg = _run(center.resolve_row(row, True, "master_1", note="", via="webconsole"))
    return msg, reinject


def test_legacy_approve_does_not_reinject():
    msg, reinject = _resolve_row("", "旧票已转达")
    assert msg == "旧票已转达"
    reinject.assert_not_awaited()
    print("[OK] 没有确认码哈希的旧票不回灌")


def test_coded_approve_reinjects():
    msg, reinject = _resolve_row("a" * 64, "已安装")
    assert msg == "已安装"
    reinject.assert_awaited_once()
    print("[OK] 带确认码的批准会回灌")


def test_hold_strips_mark_and_does_not_reinject():
    from gsuid_core.ai_core.approval.halt import APPROVAL_HOLD_MARK

    msg, reinject = _resolve_row("b" * 64, APPROVAL_HOLD_MARK + "已另开删除确认")
    assert msg == "已另开删除确认"
    reinject.assert_not_awaited()
    print("[OK] 另开下一张票时不回灌，标记不留给控制台")


def test_dto_to_node_v2_roundtrip_and_invalid():
    from gsuid_core.ai_core.agent_node import AgentNode
    from gsuid_core.ai_core.capability_agents.persistence import _dto_to_node, _node_to_dto

    node = AgentNode(
        node_id="my_agent",
        display_name="我的代理",
        prompt="p",
        tool_packs=["task_basics", "dynamic"],
        boundary_override="边界",
        source="user",
    )
    back = _dto_to_node(dict(_node_to_dto(node)))
    assert back is not None
    assert back.node_id == node.node_id
    assert back.tool_packs == node.tool_packs
    assert back.boundary_override == node.boundary_override

    assert _dto_to_node({"display_name": "缺 id"}) is None
    print("[OK] v2 DTO 往返一致；缺 id 返回 None")


def test_resolve_row_rejects_expired_ticket():
    from gsuid_core.ai_core.approval import center

    row = _row(audience="user", operator="u1")
    row.id = 3
    row.expires_at = int(time.time()) - 5
    row.confirm_code_hash = "h" * 64
    row.operator_user_id = "u1"
    row.origin_session_id = ""
    row.request_id = "req-exp"
    row.category = "expire_probe"
    row.ref_key = ""
    row.created_at = int(time.time()) - 100
    hits = {"n": 0}

    async def _cb(req: object, approved: bool, note: str) -> str:
        hits["n"] += 1
        return "不应该执行"

    center.register_approval_category("expire_probe", _cb, ttl_seconds=60)
    with (
        patch(f"{CENTER}.AIApprovalRequest.claim_status", new=AsyncMock(return_value=True)),
        patch(f"{CENTER}._refresh_pending", new=AsyncMock()),
        patch(f"{CENTER}._drop_session_if_clear", new=AsyncMock()),
        patch(f"{CENTER}._fail_linked_task", new=AsyncMock()),
        patch("gsuid_core.ai_core.approval.notify.notify_closed", new=AsyncMock()),
    ):
        msg = _run(center.resolve_row(row, True, "u1", note="", via="webconsole"))
    assert hits["n"] == 0
    assert "已超时" in msg
    assert row.status == "expired"
    print("[OK] 过期票在批准入口直接失效，不跑领域回调")


def test_resume_event_keeps_stored_pm():
    from gsuid_core.ai_core.approval.resume import _event_for

    row = MagicMock()
    row.user_type = "group"
    row.bot_id = "b"
    row.operator_user_id = "u1"
    row.bot_self_id = "bs"
    row.group_id = "g"
    row.operator_user_pm = 6
    row.payload_json = "{}"
    ev = _event_for(row)
    assert ev.user_pm == 6
    print("[OK] 续作沿用提交时的权限")


def test_submit_stores_operator_pm():
    from gsuid_core.ai_core.approval import center

    captured: dict[str, object] = {}

    async def fake_add(**kw: object) -> MagicMock:
        captured.update(kw)
        row = _row()
        row.request_id = "req"
        row.short_id = str(kw["short_id"])
        row.origin_session_id = ""
        return row

    ev = MagicMock()
    ev.user_id = "u1"
    ev.user_pm = 4
    ev.session_id = "sess"
    ev.bot_id = "b"
    ev.bot_self_id = "bs"
    ev.user_type = "direct"
    ev.group_id = None
    ev.WS_BOT_ID = "ws"
    with (
        patch(f"{CENTER}.AIApprovalRequest") as mock_model,
        patch("gsuid_core.ai_core.approval.notify.send_confirm_dm", new=AsyncMock(return_value=True)),
    ):
        mock_model.add = AsyncMock(side_effect=fake_add)
        mock_model.get_by_code_hash = AsyncMock(return_value=None)
        mock_model.get_by_short_id = AsyncMock(return_value=None)
        mock_model.get_pending_by_dedupe = AsyncMock(return_value=None)
        _run(
            center.submit(
                category="agent_request",
                title="写插件",
                ev=ev,
                audience="master",
                effect="会改插件目录",
            )
        )
    assert captured["operator_user_pm"] == 4
    print("[OK] 提交时记下操作者权限")


def test_failed_action_does_not_reinject():
    from gsuid_core.ai_core.approval import center

    row = _row(audience="user", operator="u1")
    row.id = 4
    row.expires_at = int(time.time()) + 600
    row.created_at = int(time.time())
    row.confirm_code_hash = "h" * 64
    row.operator_user_id = "u1"
    row.origin_session_id = "capagent_x"
    row.request_id = "req-fail"
    row.category = "fail_probe"
    row.ref_key = "task-1"

    async def _cb(req: object, approved: bool, note: str) -> str:
        return "安装失败：磁盘满"

    center.register_approval_category("fail_probe", _cb)
    with (
        patch(f"{CENTER}.AIApprovalRequest.claim_status", new=AsyncMock(return_value=True)),
        patch(f"{CENTER}._refresh_pending", new=AsyncMock()),
        patch(f"{CENTER}._drop_session_if_clear", new=AsyncMock()),
        patch(f"{CENTER}._fail_linked_task", new=AsyncMock()) as fail,
        patch("gsuid_core.ai_core.approval.notify.notify_closed", new=AsyncMock()),
        patch("gsuid_core.ai_core.approval.resume.reinject_approved", new=AsyncMock()) as reinject,
    ):
        msg = _run(center.resolve_row(row, True, "u1", note="可以", via="webconsole"))
    reinject.assert_not_awaited()
    fail.assert_awaited()
    assert msg.startswith("安装失败")
    print("[OK] 副作用失败不回灌")


def test_locate_prefers_pending_short_id():
    from gsuid_core.ai_core.approval import center

    pending = _row(status="pending", short_id="abcd1234")
    resolved = _row(status="approved", short_id="abcd1234")
    with (
        patch(f"{CENTER}.AIApprovalRequest.get_by_request_id", new=AsyncMock(return_value=None)),
        patch(f"{CENTER}.AIApprovalRequest.get_pending_by_short_id", new=AsyncMock(return_value=pending)),
        patch(f"{CENTER}.AIApprovalRequest.get_by_short_id", new=AsyncMock(return_value=resolved)),
    ):
        row, err = _run(center.locate("u1", "abcd1234"))
    assert err == ""
    assert row is pending
    print("[OK] 短号优先命中仍在等的票")


def test_confirm_window_follows_ttl():
    from gsuid_core.ai_core.approval.notify import format_confirm_dm

    text = format_confirm_dm("事项", "会执行", "AB23CD", "abcd", 1800)
    assert "30 分钟" in text
    assert "10 分钟" not in text
    print("[OK] 私聊写的是这张票自己的有效期")


def test_confirm_dm_only_reaches_listed_masters():
    from gsuid_core.ai_core.approval.notify import _dm_via_subscribe

    sent: list[str] = []

    class _Sub:
        def __init__(self, uid: str) -> None:
            self.user_id = uid
            self.bot_id = "b"

        async def send(self, text: str, force_direct: bool = False) -> None:
            sent.append(self.user_id)

    subs = [_Sub("m1"), _Sub("other"), _Sub("m1")]
    with (
        patch("gsuid_core.ai_core.approval.notify.core_config.get_config", return_value=["m1"]),
        patch("gsuid_core.ai_core.approval.notify.gs_subscribe.get_subscribe", new=AsyncMock(return_value=subs)),
    ):
        n = _run(_dm_via_subscribe("确认码"))
    assert n == 1
    assert sent == ["m1"]
    print("[OK] 确认码只发给名单里的主人，重复订阅只发一次")


def test_command_submit_uses_configured_ttl():
    from gsuid_core.ai_core.command_exec.analyzer import CommandPlan, SimpleCommand
    from gsuid_core.ai_core.command_exec.approval import submit as command_submit

    captured: dict[str, object] = {}

    async def fake_add(**kw: object) -> MagicMock:
        captured.update(kw)
        row = _row()
        row.request_id = str(kw["request_id"])
        row.short_id = str(kw["short_id"])
        row.origin_session_id = ""
        expires_at = kw["expires_at"]
        row.expires_at = expires_at if isinstance(expires_at, int) else 0
        return row

    plan = CommandPlan(raw="echo hi", ok=True, commands=[SimpleCommand(argv=["echo", "hi"], executable="echo")])
    with (
        patch(f"{CENTER}.AIApprovalRequest") as mock_model,
        patch("gsuid_core.ai_core.approval.notify.send_confirm_dm", new=AsyncMock(return_value=True)),
        patch("gsuid_core.ai_core.command_exec.approval.cfg_get", return_value=1800),
    ):
        mock_model.add = AsyncMock(side_effect=fake_add)
        mock_model.get_by_code_hash = AsyncMock(return_value=None)
        mock_model.get_by_short_id = AsyncMock(return_value=None)
        mock_model.get_pending_by_dedupe = AsyncMock(return_value=None)
        _run(command_submit(None, plan, "需要确认", "req-cmd"))
    expires_at = captured["expires_at"]
    assert isinstance(expires_at, int)
    assert 1700 <= expires_at - int(time.time()) <= 1800
    print("[OK] 命令审批用配置里的有效期")


def test_empty_resume_does_not_complete_subtask():
    from gsuid_core.ai_core.approval.resume import _settle_capability

    task = MagicMock()
    task.status = "running"
    task.id = "child"
    task.root_task_id = "root"
    root = MagicMock()
    row = MagicMock()
    row.category = "kanban_subtask"
    row.ref_key = "child"
    row.origin_session_id = "capagent_x"

    async def get_by_id(task_id: str) -> MagicMock:
        return task if task_id == "child" else root

    with (
        patch("gsuid_core.ai_core.planning.models.AIAgentTask.get_by_id", new=AsyncMock(side_effect=get_by_id)),
        patch("gsuid_core.ai_core.planning.kanban.mark_subtask_completed", new=AsyncMock()) as done,
        patch("gsuid_core.ai_core.planning.kanban.mark_subtask_failed", new=AsyncMock()) as failed,
        patch("gsuid_core.ai_core.approval.session_ctx.session_has_pending", return_value=False),
        patch("gsuid_core.ai_core.planning.kanban_executor._finish_capability_delivery", new=AsyncMock()),
    ):
        _run(_settle_capability(row, "", None))
    done.assert_not_awaited()
    failed.assert_awaited()
    print("[OK] 空续作把任务标失败")


def test_coded_kanban_approval_does_not_reschedule():
    from gsuid_core.ai_core.approval.center import _CATEGORIES
    from gsuid_core.ai_core.planning.startup import _register_kanban_approval_category

    _register_kanban_approval_category()
    handler = _CATEGORIES["kanban_subtask"].on_resolve
    task = MagicMock()
    task.id = "task-1"
    req = MagicMock()
    req.confirm_code_hash = "abc"
    req.ref_key = "task-1"
    req.short_id = "abcd"
    req.payload_json = "{}"
    req.origin_session_id = "capagent_demo"
    with (
        patch("gsuid_core.ai_core.planning.models.AIAgentTask.get_by_id", new=AsyncMock(return_value=task)),
        patch("gsuid_core.ai_core.planning.models.AIAgentTaskLog.add_log", new=AsyncMock()),
        patch("gsuid_core.ai_core.planning.kanban.approve_subtask", new=AsyncMock()) as approve,
        patch("asyncio.create_task") as create_task,
    ):
        text = _run(handler(req, True, "可以"))
    assert "[[APPROVAL_HOLD]]" not in text
    assert "已批准" in text
    approve.assert_not_awaited()
    create_task.assert_not_called()
    print("[OK] 带确认码的任务批准后留在原会话")


def test_match_confirm_skips_db_when_ai_off():
    from gsuid_core.ai_core.approval import center

    ev = MagicMock(user_type="direct", raw_text="AB23CD", user_id="m1")

    class _Off:
        data = False

    with (
        patch("gsuid_core.ai_core.configs.ai_config.ai_config.get_config", return_value=_Off()),
        patch(f"{CENTER}.AIApprovalRequest.get_by_code_hash", new=AsyncMock()) as lookup,
    ):
        assert _run(center.match_confirm_message(ev)) is False
    lookup.assert_not_awaited()
    print("[OK] AI 关闭时确认码不查表")


def test_match_confirm_queries_hash_when_ai_on():
    from gsuid_core.ai_core.approval import center

    ev = MagicMock(
        user_type="direct",
        raw_text="AB23CD",
        user_id="m1",
        bot_id="b",
        bot_self_id="",
        WS_BOT_ID="ws",
    )

    class _On:
        data = True

    with (
        patch("gsuid_core.ai_core.configs.ai_config.ai_config.get_config", return_value=_On()),
        patch(f"{CENTER}.AIApprovalRequest.get_by_code_hash", new=AsyncMock(return_value=None)) as lookup,
    ):
        assert _run(center.match_confirm_message(ev)) is False
    lookup.assert_awaited()
    print("[OK] AI 开启时确认码形状会查哈希")


if __name__ == "__main__":
    test_can_resolve_webconsole_always_allowed()
    test_can_resolve_master_audience()
    test_can_resolve_user_audience()
    test_full_access_toggle_and_resolver()
    test_submit_full_access_exemption_matrix()
    test_tool_grant_consume_once_and_expiry()
    test_tool_call_gate_reuses_pending()
    test_tool_call_gate_no_ev_passes()
    test_log_question_ledger_shapes()
    test_confirm_code_shape_and_hash()
    test_submit_dedupe_does_not_add()
    test_resume_card_is_not_a_delivery_frame()
    test_legacy_approve_does_not_reinject()
    test_coded_approve_reinjects()
    test_hold_strips_mark_and_does_not_reinject()
    test_resolve_row_rejects_expired_ticket()
    test_resume_event_keeps_stored_pm()
    test_submit_stores_operator_pm()
    test_failed_action_does_not_reinject()
    test_locate_prefers_pending_short_id()
    test_confirm_window_follows_ttl()
    test_confirm_dm_only_reaches_listed_masters()
    test_command_submit_uses_configured_ttl()
    test_empty_resume_does_not_complete_subtask()
    test_coded_kanban_approval_does_not_reschedule()
    test_match_confirm_skips_db_when_ai_off()
    test_match_confirm_queries_hash_when_ai_on()
    test_respawn_limit_opens_center_ticket()
    test_dto_to_node_migrates_v1()
    test_dto_to_node_v2_roundtrip_and_invalid()
    print("ALL OK")
