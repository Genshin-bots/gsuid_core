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


# ============================================================
# _can_resolve 裁决权矩阵
# ============================================================
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


# ============================================================
# 完全访问豁免
# ============================================================
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
        return row

    with patch(f"{CENTER}.AIApprovalRequest") as mock_model:
        mock_model.add = AsyncMock(side_effect=fake_add)
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


# ============================================================
# tool_call 一次性 grant 与策略门去重
# ============================================================
def test_tool_grant_consume_once_and_expiry():
    from gsuid_core.ai_core.approval import center

    center.grant_tool_call("u1", "paint_tool")
    assert center.consume_tool_grant("u1", "paint_tool")
    assert not center.consume_tool_grant("u1", "paint_tool")  # 一次性

    center._TOOL_GRANTS[("u1", "paint_tool")] = time.time() - 1
    assert not center.consume_tool_grant("u1", "paint_tool")  # 已过期
    print("[OK] grant 一次性消费 + 过期失效")


def test_tool_call_gate_reuses_pending():
    from gsuid_core.ai_core.approval import center

    ev = MagicMock(user_id="u1")
    with patch(f"{CENTER}.AIApprovalRequest") as mock_model, patch(f"{CENTER}.submit", new=AsyncMock()) as mock_submit:
        mock_model.list_pending = AsyncMock(return_value=[_row(short_id="ab12")])
        msg = _run(center.tool_call_gate(ev, "paint_tool", "user", "{}"))
    assert msg is not None and "ab12" in msg
    mock_submit.assert_not_awaited()
    print("[OK] 同 (operator, tool) 的 pending 复用，不重复开票")


def test_tool_call_gate_no_ev_passes():
    from gsuid_core.ai_core.approval import center

    assert _run(center.tool_call_gate(None, "paint_tool", "user", "{}")) is None
    print("[OK] 无 ev 的后台链路放行（权限由各自 check_func 承担）")


# ============================================================
# 问答留档（interaction="question"）
# ============================================================
def test_log_question_ledger_shapes():
    from gsuid_core.ai_core.approval import center

    captured = {}

    async def fake_add(**kw):
        captured.update(kw)
        return _row()

    ev = MagicMock(user_id="u1", session_id="s1", bot_id="b", bot_self_id="bs", user_type="group", group_id="g1")
    with patch(f"{CENTER}.AIApprovalRequest") as mock_model:
        mock_model.add = AsyncMock(side_effect=fake_add)
        _run(center.log_question(ev, "画面比例?", "16:9", answered=True))
    assert captured["interaction"] == "question"
    assert captured["status"] == "approved"
    assert captured["resolved_note"] == "16:9"
    assert captured["resolved_by"] == "u1"

    with patch(f"{CENTER}.AIApprovalRequest") as mock_model:
        mock_model.add = AsyncMock(side_effect=fake_add)
        _run(center.log_question(ev, "画面比例?", "", answered=False))
    assert captured["status"] == "expired"
    assert captured["resolved_by"] == ""
    print("[OK] 问答留档：回答→approved、超时→expired，账本形状正确")


# ============================================================
# respawn 达上限：必须经统一入口开中心票据
# ============================================================
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


# ============================================================
# persistence: v1 旧画像 JSON → AgentNode v2 自动迁移
# ============================================================
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
    test_respawn_limit_opens_center_ticket()
    test_dto_to_node_migrates_v1()
    test_dto_to_node_v2_roundtrip_and_invalid()
    print("ALL OK")
