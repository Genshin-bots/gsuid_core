"""测试私聊亲和契约租约（DM Affinity Lease）与多人格路由 / 退避隔离。

不依赖真实人格目录：人格配置一律用临时 StringConfig 桩。
"""

import time
from typing import Dict
from pathlib import Path

import pytest

from gsuid_core.ai_core.persona.config import persona_config_manager
from gsuid_core.ai_core.proactive.dm_lease import (
    DM_LEASE_TTL_SECONDS,
    grant_dm_lease,
    renew_dm_lease,
    revoke_dm_lease,
    get_active_dm_lease,
    maybe_switch_dm_lease,
)
from gsuid_core.utils.plugins_config.models import GsStrConfig, GsListStrConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig


def _persona_cfg(tmp_path: Path, name: str, scope: str, groups: list[str] | None = None) -> StringConfig:
    return StringConfig(
        f"test_lease_persona_{name}",
        tmp_path / f"{name}.json",
        {
            "scope": GsStrConfig("启用范围", "", scope),
            "target_groups": GsListStrConfig("目标群聊", "", list(groups or [])),
        },
    )


def _patch_lease_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_file = tmp_path / "dm_leases.json"
    monkeypatch.setattr("gsuid_core.ai_core.proactive.dm_lease._path", lambda: test_file)


def _patch_personas(monkeypatch: pytest.MonkeyPatch, configs: Dict[str, StringConfig]) -> None:
    monkeypatch.setattr(persona_config_manager, "get_all_configs", lambda: configs)
    monkeypatch.setattr(persona_config_manager, "get_config", lambda name: configs[name])
    monkeypatch.setattr(
        "gsuid_core.buildin_plugins.core_command.core_ai_control.state.get_persona_override",
        lambda sid: None,
    )


def test_dm_affinity_lease_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lease_path(tmp_path, monkeypatch)
    _patch_personas(monkeypatch, {"sayu": _persona_cfg(tmp_path, "sayu", "global")})

    user_id = "test_user_dm_999"
    bot_id = "test_bot_dm"
    persona = "sayu"

    # 1. 签发租约
    lease = grant_dm_lease(
        user_id=user_id,
        persona_name=persona,
        bot_id=bot_id,
        source_group_id="999888",
    )
    assert lease.persona_name == persona
    assert lease.source_group_id == "999888"
    assert lease.expires_at - lease.issued_at == DM_LEASE_TTL_SECONDS

    # 2. 查询活跃租约（支持 bot_id + user_id 或纯 user_id）
    found = get_active_dm_lease(user_id, bot_id)
    assert found is not None
    assert found.persona_name == persona

    found_by_user = get_active_dm_lease(user_id)
    assert found_by_user is not None
    assert found_by_user.persona_name == persona

    # 3. 续期双写一致性测试：bot_id+user 续期后，纯 user 查找也同步延期
    old_exp = found.expires_at
    time.sleep(0.01)
    renewed = renew_dm_lease(user_id, bot_id)
    assert renewed is True

    found_renewed = get_active_dm_lease(user_id, bot_id)
    assert found_renewed is not None
    assert found_renewed.expires_at >= old_exp

    found_user_renewed = get_active_dm_lease(user_id)
    assert found_user_renewed is not None
    assert found_user_renewed.expires_at == found_renewed.expires_at

    # 4. 撤销租约
    revoked = revoke_dm_lease(user_id, bot_id)
    assert revoked is True
    assert get_active_dm_lease(user_id, bot_id) is None
    assert get_active_dm_lease(user_id) is None


def test_dm_affinity_lease_session_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lease_path(tmp_path, monkeypatch)
    _patch_personas(monkeypatch, {"sayu": _persona_cfg(tmp_path, "sayu", "global")})

    user_id = "test_user_routing_101"
    bot_id = "mock_bot"

    grant_dm_lease(user_id=user_id, persona_name="sayu", bot_id=bot_id)
    session_id = f"mock_ws:{bot_id}:mock_self:private:{user_id}"
    resolved = persona_config_manager.get_persona_for_session(session_id)
    assert resolved == "sayu"


def test_dm_affinity_lease_zombie_eviction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_lease_path(tmp_path, monkeypatch)

    grant_dm_lease(user_id="u_zombie", persona_name="nonexistent_persona_xyz", bot_id="bot1")
    assert get_active_dm_lease("u_zombie", "bot1") is not None

    renewed = renew_dm_lease("u_zombie", "bot1")
    assert renewed is False
    assert get_active_dm_lease("u_zombie", "bot1") is None


def test_lease_beats_specific_persona_in_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """私聊路由顺序：租约 > specific 绑定 > fallback。租约人格必须赢。"""
    _patch_lease_path(tmp_path, monkeypatch)
    configs = {
        "spec": _persona_cfg(tmp_path, "spec", "specific", ["u_target"]),
        "leased": _persona_cfg(tmp_path, "leased", "global_private"),
    }
    _patch_personas(monkeypatch, configs)

    user_id = "u_target"
    bot_id = "bot1"
    grant_dm_lease(user_id=user_id, persona_name="leased", bot_id=bot_id)

    session_id = f"ws:{bot_id}:self:private:{user_id}"
    resolved = persona_config_manager.get_persona_for_session(session_id)
    assert resolved == "leased", "租约必须排在 specific 绑定之前"


def test_disabled_persona_lease_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """指向已禁用人格的租约视同不存在，回落到正常路由。"""
    _patch_lease_path(tmp_path, monkeypatch)
    configs = {
        "dis": _persona_cfg(tmp_path, "dis", "disabled"),
        "main": _persona_cfg(tmp_path, "main", "global"),
    }
    _patch_personas(monkeypatch, configs)

    user_id = "u1"
    bot_id = "bot1"
    grant_dm_lease(user_id=user_id, persona_name="dis", bot_id=bot_id)

    session_id = f"ws:{bot_id}:self:private:{user_id}"
    resolved = persona_config_manager.get_persona_for_session(session_id)
    assert resolved == "main", "禁用人格的租约不能生效"
    assert resolved != "dis"


def test_maybe_switch_dm_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """私聊开头点名其他启用人格 → 切租约；句中提到不算点名。"""
    _patch_lease_path(tmp_path, monkeypatch)
    configs = {
        "Alpha": _persona_cfg(tmp_path, "Alpha", "global"),
        "Beta": _persona_cfg(tmp_path, "Beta", "global"),
        "off": _persona_cfg(tmp_path, "off", "disabled"),
    }
    _patch_personas(monkeypatch, configs)

    # 正向：开头点名 Beta → 切换并签约
    switched = maybe_switch_dm_lease("u1", "bot1", "Beta，今晚有空吗", "Alpha")
    assert switched == "Beta"
    lease = get_active_dm_lease("u1", "bot1")
    assert lease is not None and lease.persona_name == "Beta"

    # 负向：句中提到人格名不是点名
    revoke_dm_lease("u1", "bot1")
    assert maybe_switch_dm_lease("u1", "bot1", "我觉得Beta说得对", "Alpha") is None
    assert get_active_dm_lease("u1", "bot1") is None

    # 当前人格不触发切换；禁用人格不触发；前导标点不影响开头判定
    assert maybe_switch_dm_lease("u1", "bot1", "Alpha 你好", "Alpha") is None
    assert maybe_switch_dm_lease("u1", "bot1", "off 你好", "Alpha") is None
    assert maybe_switch_dm_lease("u1", "bot1", "【Beta】在吗", "Alpha") == "Beta"


def test_dispatcher_persona_isolation() -> None:
    """暂存槽 / 复读窗 / 频控按 (persona, target) 分桶；物理冷静期 _last_send 仍共享。"""
    from gsuid_core.ai_core.heartbeat.dispatcher import UnifiedProactiveDispatcher, make_target_key

    d = UnifiedProactiveDispatcher()
    key = make_target_key("g1", None)

    d.stage_merge_context(key, "A 暂存的事实", ttl_seconds=300, source="autonomy", persona_name="A")
    assert d.peek_staged(key, source="autonomy", persona_name="B") == ""
    assert d.peek_staged(key, source="autonomy", persona_name="A") == "A 暂存的事实"

    d.register_send(key, "heartbeat", "A 的问候", persona_name="A")
    assert d.get_recent_heartbeat_count(key, persona_name="A") == 1
    assert d.get_recent_heartbeat_count(key, persona_name="B") == 0
    assert d.should_suppress_by_rate(key, max_per_hour=1, max_per_day=8, persona_name="A")
    assert not d.should_suppress_by_rate(key, max_per_hour=1, max_per_day=8, persona_name="B")

    assert d.would_repeat_heartbeat(key, "A 的问候", persona_name="A")
    assert not d.would_repeat_heartbeat(key, "A 的问候", persona_name="B")

    # 物理冷静期不按人格分桶：刚发过言的目标对谁都冷静
    assert d.should_suppress_heartbeat(key)


def test_private_push_backoff_mode_and_tool_guard() -> None:
    """allow_private_push=False → 私聊不直推（staged_merge）且工具拒绝私聊。"""
    import asyncio
    from unittest.mock import MagicMock

    from gsuid_core.ai_core.autonomy.tools import GuardState, bind_guard, reset_guard, send_message_by_ai
    from gsuid_core.ai_core.autonomy.models import DeliveryPin, DeliveryPlan, CareTargetSnapshot
    from gsuid_core.ai_core.autonomy.dispatch import _mode_for
    from gsuid_core.ai_core.relationship.zones import Zone
    from gsuid_core.ai_core.heartbeat.dispatcher import make_target_key

    user_id = "u_backoff"
    pin = DeliveryPin(
        user_id=user_id,
        group_id=None,
        is_master=False,
        zone_value=Zone.CLOSE.value,
        bot_id="bot1",
        bot_self_id="self1",
        ws_bot_id="ws1",
        session_key="s1",
        last_human_ts=0.0,
        allow_private_push=False,
    )
    plan = DeliveryPlan(
        drive="care_user",
        target=user_id,
        summary="s",
        substantive=True,
        within_hours=True,
        hours_waived=False,
        now=1000.0,
        care=[pin],
        groups=[],
        served_user_ids=frozenset(),
        quota_ok_keys=frozenset({make_target_key(None, user_id)}),
        gap_blocked_keys=frozenset(),
    )
    assert _mode_for("care_user", pin, plan) == "staged_merge", "退避期私聊绝不能直推"

    allowed = DeliveryPin(
        user_id=user_id,
        group_id=None,
        is_master=False,
        zone_value=Zone.CLOSE.value,
        bot_id="bot1",
        bot_self_id="self1",
        ws_bot_id="ws1",
        session_key="s1",
        last_human_ts=0.0,
        allow_private_push=True,
    )
    plan2 = DeliveryPlan(
        drive="care_user",
        target=user_id,
        summary="s",
        substantive=True,
        within_hours=True,
        hours_waived=False,
        now=1000.0,
        care=[allowed],
        groups=[],
        served_user_ids=frozenset(),
        quota_ok_keys=frozenset({make_target_key(None, user_id)}),
        gap_blocked_keys=frozenset(),
    )
    assert _mode_for("care_user", allowed, plan2) == "immediate_push", "未退避时近关系私聊仍可直推"

    snap = CareTargetSnapshot(
        user_id=user_id,
        user_name="U",
        score=80,
        zone_value=Zone.CLOSE.value,
        is_master=False,
        bound_uids={},
        recent_lines=[],
        group_id=None,
        bot_id="bot1",
        bot_self_id="self1",
        ws_bot_id="ws1",
        session_key="s1",
        last_human_ts=0.0,
        allow_private_push=False,
    )
    state = GuardState(
        persona_name="p1",
        whitelist=frozenset(),
        sessions={},
        bot_id="bot1",
        bot_self_id="self1",
        ws_bot_id="ws1",
        allowed_groups={},
        allowed_users={user_id: snap},
    )
    token = bind_guard(state)
    try:
        result = asyncio.run(send_message_by_ai(MagicMock(), text="在吗", target_id=user_id, target_type="user"))
        assert result.startswith("拒绝："), f"工具必须拒绝退避中的私聊: {result}"
    finally:
        reset_guard(token)


def test_maybe_switch_dm_lease_longest_name_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同前缀人格名命中最长者：Beta2 不能被 Beta 抢先。"""
    _patch_lease_path(tmp_path, monkeypatch)
    _patch_personas(
        monkeypatch,
        {
            "Alpha": _persona_cfg(tmp_path, "Alpha", "global"),
            "Beta": _persona_cfg(tmp_path, "Beta", "global"),
            "Beta2": _persona_cfg(tmp_path, "Beta2", "global"),
        },
    )

    switched = maybe_switch_dm_lease("u1", "bot1", "Beta2 你好", "Alpha")
    assert switched == "Beta2"
    lease = get_active_dm_lease("u1", "bot1")
    assert lease is not None and lease.persona_name == "Beta2"


def test_maybe_switch_dm_lease_ascii_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """人格名后紧跟 ASCII 字母/数字/下划线视为同词延续，不切租约；中文延续不受限。"""
    _patch_lease_path(tmp_path, monkeypatch)
    _patch_personas(
        monkeypatch,
        {
            "Alpha": _persona_cfg(tmp_path, "Alpha", "global"),
            "Beta": _persona_cfg(tmp_path, "Beta", "global"),
            "早柚": _persona_cfg(tmp_path, "早柚", "global"),
        },
    )

    # ASCII 词字符延续 = 同一个更长的词，不是点名
    assert maybe_switch_dm_lease("u1", "bot1", "Betaabc 你好", "Alpha") is None
    assert maybe_switch_dm_lease("u1", "bot1", "Beta_2 你好", "Alpha") is None
    assert maybe_switch_dm_lease("u1", "bot1", "Beta9", "Alpha") is None
    assert get_active_dm_lease("u1", "bot1") is None

    # 中文延续仍是点名（「早柚在吗」不带标点是常态称呼）
    assert maybe_switch_dm_lease("u1", "bot1", "早柚在吗", "Alpha") == "早柚"
    revoke_dm_lease("u1", "bot1")

    # 恰好结尾 / 标点边界也命中
    assert maybe_switch_dm_lease("u1", "bot1", "Beta", "Alpha") == "Beta"
    revoke_dm_lease("u1", "bot1")
    assert maybe_switch_dm_lease("u1", "bot1", "Beta.在吗", "Alpha") == "Beta"
