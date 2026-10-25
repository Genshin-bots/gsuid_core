"""作息、模式名和自主行动配置的读写。"""

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import SQLModel

from gsuid_core.ai_core.autonomy.gates import gate_reason, autonomy_is_due
from gsuid_core.ai_core.persona.config import (
    AUTONOMY_MODE,
    AI_MODE_OPTIONS,
    AutonomySettings,
    PersonaConfigManager,
    normalize_ai_modes,
    is_within_active_hours,
)
from gsuid_core.ai_core.database.models import AIAutonomyActivity, first_row_per_persona

_SH = ZoneInfo("Asia/Shanghai")


def _mgr(tmp_path: Path) -> PersonaConfigManager:
    mgr = PersonaConfigManager()
    mgr._base_path = tmp_path
    mgr._cache = {}
    return mgr


def test_four_modes_replace_the_retired_rescue_chip() -> None:
    assert AI_MODE_OPTIONS == ["提及应答", "定时巡检", "命令补充", AUTONOMY_MODE]
    assert normalize_ai_modes(["提及应答", "困境救场(暂不可用)", "自由活动", "自由活动"]) == [
        "提及应答",
        AUTONOMY_MODE,
    ]


def test_active_hours_cover_edges_midnight_and_bad_input() -> None:
    assert is_within_active_hours(["08:00-23:30"], "Asia/Shanghai", datetime(2026, 10, 26, 8, 0, tzinfo=_SH))
    assert is_within_active_hours(["08:00-23:30"], "Asia/Shanghai", datetime(2026, 10, 26, 23, 30, tzinfo=_SH))
    assert not is_within_active_hours(["08:00-23:30"], "Asia/Shanghai", datetime(2026, 10, 26, 23, 31, tzinfo=_SH))
    assert not is_within_active_hours(["08:00-23:30"], "Asia/Shanghai", datetime(2026, 10, 26, 7, 59, tzinfo=_SH))
    assert is_within_active_hours(["22:00-02:00"], "Asia/Shanghai", datetime(2026, 10, 26, 23, 0, tzinfo=_SH))
    assert is_within_active_hours(["22:00-02:00"], "Asia/Shanghai", datetime(2026, 10, 26, 2, 0, tzinfo=_SH))
    assert not is_within_active_hours(["22:00-02:00"], "Asia/Shanghai", datetime(2026, 10, 26, 12, 0, tzinfo=_SH))
    noon = datetime(2026, 10, 26, 12, 0, tzinfo=_SH)
    assert is_within_active_hours(["09:00-12:00", "14:00-18:00"], "Asia/Shanghai", noon)
    assert not is_within_active_hours(["09:00-11:00", "bad", "14:00-18:00"], "Asia/Shanghai", noon)
    assert not is_within_active_hours(["not-a-window"], "Asia/Shanghai", noon)
    assert not is_within_active_hours([], "Asia/Shanghai", noon)
    assert not is_within_active_hours(["08:00-23:30"], "Not/AZone", noon)


def test_autonomy_settings_round_trip_and_reject_bad_windows(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.set_scope("alice", "disabled")[0]
    ok, _msg = mgr.set_autonomy_settings(
        "alice",
        AutonomySettings(
            active_hours=["22:00-02:00"],
            timezone="Asia/Shanghai",
            interval_minutes=30,
            care_min_zone="close",
        ),
    )
    assert ok
    data = mgr.get_persona_config_dict("alice")
    assert data is not None
    assert data["active_hours"] == ["22:00-02:00"]
    assert data["autonomy_interval_minutes"] == 30
    assert data["autonomy_care_min_zone"] == "close"
    assert "interests" not in data
    assert "autonomy_max_steps" not in data
    assert "autonomy_model" not in data
    rejected, message = mgr.set_autonomy_settings("alice", AutonomySettings(active_hours=["25:00-26:00"]))
    assert rejected is False
    assert "HH:MM" in message
    again = mgr.get_persona_config_dict("alice")
    assert again is not None
    assert again["active_hours"] == ["22:00-02:00"]


def test_gate_lets_a_manual_run_skip_hours_but_not_budget() -> None:
    assert (
        gate_reason(
            ai_enabled=True,
            mode_on=True,
            scope_enabled=True,
            within_hours=False,
            chat_busy=True,
            due=False,
            budget_ok=True,
            force=True,
        )
        == ""
    )
    assert (
        gate_reason(
            ai_enabled=True,
            mode_on=True,
            scope_enabled=True,
            within_hours=True,
            chat_busy=False,
            due=True,
            budget_ok=False,
            force=True,
        )
        == "budget"
    )
    assert (
        gate_reason(
            ai_enabled=True,
            mode_on=False,
            scope_enabled=True,
            within_hours=True,
            chat_busy=False,
            due=True,
            budget_ok=True,
            force=False,
        )
        == "mode_off"
    )


def test_due_clock_uses_the_table_then_a_first_delay_then_backoff() -> None:
    assert autonomy_is_due(100.0, now=200.0, cooldown_seconds=50.0, first_due_at=None, backoff_until=None)
    assert not autonomy_is_due(100.0, now=140.0, cooldown_seconds=50.0, first_due_at=None, backoff_until=None)
    assert not autonomy_is_due(None, now=100.0, cooldown_seconds=50.0, first_due_at=130.0, backoff_until=None)
    assert autonomy_is_due(None, now=130.0, cooldown_seconds=50.0, first_due_at=130.0, backoff_until=None)
    assert not autonomy_is_due(None, now=130.0, cooldown_seconds=50.0, first_due_at=130.0, backoff_until=200.0)
    assert not autonomy_is_due(None, now=100.0, cooldown_seconds=50.0, first_due_at=None, backoff_until=None)


def test_activity_table_follows_the_class_name() -> None:
    table = SQLModel.metadata.tables["aiautonomyactivity"]
    assert table.name == "aiautonomyactivity"
    column_names = {column.name for column in table.columns}
    assert set(AIAutonomyActivity.model_fields) >= column_names
    assert {
        "persona_name",
        "drive_kind",
        "target_user_id",
        "delivery_mode",
        "steps_json",
        "fact_pack_json",
        "session_id",
        "created_at",
    } <= column_names
    assert "id" in column_names


def test_feed_keeps_one_row_per_persona_in_given_order() -> None:
    newer = AIAutonomyActivity(id=2, persona_name="a", created_at=2.0)
    other = AIAutonomyActivity(id=3, persona_name="b", created_at=2.0)
    older = AIAutonomyActivity(id=1, persona_name="a", created_at=1.0)
    kept = first_row_per_persona([newer, other, older])
    assert [row.id for row in kept] == [2, 3]


def test_activity_view_exposes_session_log_id() -> None:
    from gsuid_core.webconsole.autonomy_api import _view

    spoken = AIAutonomyActivity(persona_name="p", session_id="autonomy:p:abc", summary="看过了")
    view = _view(spoken)
    assert view["session_id"] == "autonomy:p:abc"
    assert view["summary"] == "看过了"
    assert _view(AIAutonomyActivity(persona_name="p"))["session_id"] == ""
