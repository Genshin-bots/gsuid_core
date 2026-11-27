"""人格管理契约：来源标记、整包定位、手账分片与做梦、主动发言、关系与重置还原。"""

from __future__ import annotations

import io
import os
import json
import time
import shutil
import asyncio
import zipfile
from typing import TYPE_CHECKING
from pathlib import Path
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from collections.abc import Iterator, Sequence, AsyncIterator

import pytest
from pydantic_ai.tools import Tool
from pydantic_ai.messages import TextPart, ModelMessage, ModelResponse
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from gsuid_core.bot import Bot, _Bot
from tests._support import (
    make_run_ctx,
    isolated_history,
    setup_journal_env,
    isolated_sqlite_db,
    make_session_record,
    clear_journal_caches,
)
from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.persona import store as persona_store
from gsuid_core.ai_core.persona.bundle import BundleError
from gsuid_core.utils.plugins_config.models import GsStrConfig, GsListStrConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

if TYPE_CHECKING:
    from pydantic_ai.models import Model
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from gsuid_core.ai_core.journal import dream as jd, storage as jstorage
    from gsuid_core.ai_core.autonomy import tools as auto_tools
    from gsuid_core.ai_core.agent_run.state import ReturnMode
    from gsuid_core.ai_core.autonomy.models import (
        GroupObservation,
        ObservationDigest,
        CareTargetSnapshot,
    )


def _patch_persona_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core import resource as core_res
    from gsuid_core.ai_core.persona import persona as persona_mod

    monkeypatch.setattr(core_res, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr(persona_mod, "PERSONA_PATH", tmp_path)


def _make_source() -> persona_store.PersonaSource:
    return persona_store.PersonaSource(
        source="git",
        repo_url="https://github.com/x/persona_a",
        repo_name="persona_a",
        branch="main",
        bundle_dir="persona_a",
        commit="abc1234",
        installed_at=1.0,
        packs=["测试人格"],
    )


def _persona_cfg(tmp_path: Path, name: str, scope: str, groups: list[str] | None = None) -> StringConfig:
    return StringConfig(
        f"test_persona_{name}",
        tmp_path / f"{name}.json",
        {
            "scope": GsStrConfig("启用范围", "", scope),
            "target_groups": GsListStrConfig("目标群聊", "", list(groups or [])),
        },
    )


def _setup_isolated_persona_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.persona import config as persona_cfg_mod
    from gsuid_core.ai_core.proactive import dm_lease as dm_lease_mod

    monkeypatch.setattr(persona_cfg_mod, "PERSONA_PATH", tmp_path)
    monkeypatch.setattr(persona_cfg_mod.persona_config_manager, "_base_path", tmp_path)
    persona_cfg_mod.persona_config_manager.clear_cache()

    lease_file = tmp_path / "dm_leases.json"
    monkeypatch.setattr(dm_lease_mod, "_path", lambda: lease_file)
    monkeypatch.setattr(dm_lease_mod, "_cached_path", None)
    monkeypatch.setattr(dm_lease_mod, "_cached_mtime", -1.0)
    monkeypatch.setattr(dm_lease_mod, "_cached_leases", {})


def _seed_persona(
    tmp_path: Path,
    name: str,
    scope: str,
    groups: list[str] | None = None,
    *,
    group_scope: str | None = None,
    private_scope: str | None = None,
    users: list[str] | None = None,
) -> None:
    from gsuid_core.ai_core.persona.config import persona_config_manager

    pdir = tmp_path / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "persona.md").write_text(f"# {name}\n", encoding="utf-8")
    if group_scope is not None or private_scope is not None:
        ok, msg = persona_config_manager.set_channel_scopes(
            name,
            group_scope=group_scope if group_scope is not None else "off",
            private_scope=private_scope if private_scope is not None else "off",
        )
        assert ok, msg
    else:
        ok, msg = persona_config_manager.set_scope(name, scope)
        assert ok, msg
    ok_g, msg_g = persona_config_manager.set_target_groups(name, list(groups or []))
    assert ok_g, msg_g
    if users is not None:
        ok_u, msg_u = persona_config_manager.set_target_users(name, list(users))
        assert ok_u, msg_u


async def _drain_background_tasks() -> None:
    """确定性排空当前事件循环派生的后台异步任务。"""
    current = asyncio.current_task()
    for _ in range(5):
        pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


async def _run_offline_agent(
    monkeypatch: pytest.MonkeyPatch,
    model: Model,
    user_message: str,
    *,
    create_by: str = "Chat",
    session_id: str = "ws1:onebot:1:group:group_1",
    persona_name: str = "alpha",
    ev: Event | None = None,
    bot: Bot | None = None,
    tools: list[Tool[ToolContext]] | None = None,
    return_mode: ReturnMode = "return",
    dynamic_tools: bool = False,
    is_framework_injection: bool = False,
) -> str:
    from gsuid_core.ai_core.gs_agent import create_agent

    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _l, _k=None: model)
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_by_full_name", lambda _n: model)
    agent = create_agent(
        system_prompt="测试角色",
        persona_name=persona_name,
        create_by=create_by,
        session_id=session_id,
        dynamic_tools=dynamic_tools,
    )
    try:
        return await agent.run(
            user_message,
            bot=bot,
            ev=ev,
            tools=[] if tools is None else tools,
            return_mode=return_mode,
            budget_gate=False,
            is_framework_injection=is_framework_injection,
        )
    finally:
        agent._session_logger.close()


def _make_group_obs(
    group_id: str,
    *,
    now: float,
    recent_lines: list[str] | None = None,
    last_assistant_ts: float = 0.0,
) -> GroupObservation:
    from gsuid_core.ai_core.autonomy.models import GroupObservation as _GroupObs

    return _GroupObs(
        group_id=group_id,
        summary="",
        recent_lines=list(recent_lines or []),
        last_human_ts=now - 300.0,
        bot_id="onebot",
        bot_self_id="10001",
        ws_bot_id="ws1",
        session_key=f"group:{group_id}",
        last_assistant_ts=last_assistant_ts,
    )


def _make_event(
    *,
    group_id: str | None = None,
    user_id: str = "100000001",
    text: str = "",
    bot_self_id: str = "10001",
    ws_bot_id: str | None = "ws1",
) -> Event:
    return Event(
        bot_id="onebot",
        bot_self_id=bot_self_id,
        user_id=user_id,
        user_pm=6,
        user_type="group" if group_id else "direct",
        group_id=group_id,
        text=text,
        WS_BOT_ID=ws_bot_id,
    )


def _make_digest(
    persona_name: str = "alpha",
    *,
    care_targets: list[CareTargetSnapshot] | None = None,
    group_digests: list[GroupObservation] | None = None,
) -> ObservationDigest:
    from gsuid_core.ai_core.autonomy.models import ObservationDigest as _ObsDigest

    return _ObsDigest(
        persona_name=persona_name,
        bot_id="onebot",
        bot_self_id="10001",
        ws_bot_id="ws1",
        mood_score=0.5,
        care_targets=list(care_targets or []),
        group_digests=list(group_digests or []),
    )


def _make_guard(
    *,
    now: float,
    persona_name: str = "alpha",
    allowed_groups: dict[str, GroupObservation] | None = None,
    allowed_users: dict[str, CareTargetSnapshot] | None = None,
    whitelist: frozenset[str] = frozenset(),
    hour_cap: int = 2,
    day_cap: int = 8,
) -> auto_tools.GuardState:
    from gsuid_core.ai_core.autonomy import tools as _auto_tools

    return _auto_tools.GuardState(
        persona_name=persona_name,
        whitelist=whitelist,
        sessions={},
        bot_id="onebot",
        bot_self_id="10001",
        ws_bot_id="ws1",
        allowed_groups=dict(allowed_groups or {}),
        allowed_users=dict(allowed_users or {}),
        now=now,
        hour_cap=hour_cap,
        day_cap=day_cap,
    )


def _read_timeline_now(path: Path) -> list[str]:
    from gsuid_core.ai_core.journal import storage as jstorage

    return jstorage.parse_shard_markdown(path.read_text(encoding="utf-8")).timeline if path.exists() else []


def test_source_marker_bundle_root_and_default_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.migrations.persona_scope_v1 import pick_default_persona

    _patch_persona_path(tmp_path, monkeypatch)
    p = tmp_path / "persona_demo"
    p.mkdir(parents=True)
    (p / "persona.md").write_text("# persona_demo\n", encoding="utf-8")

    assert persona_store.read_source("persona_demo") is None and not persona_store.is_git_managed("persona_demo")

    persona_store.write_source("persona_demo", _make_source())
    src = persona_store.read_source("persona_demo")
    assert src is not None and src["repo_name"] == "persona_a" and persona_store.is_git_managed("persona_demo")

    persona_store.drop_source("persona_demo")
    assert not persona_store.is_git_managed("persona_demo")
    (p / persona_store.SOURCE_MARKER).write_text("{bad", encoding="utf-8")
    assert persona_store.read_source("persona_demo") is None

    bundle_dir = tmp_path / "bundle_test"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text("{}", encoding="utf-8")
    assert persona_store.find_bundle_root(bundle_dir) == bundle_dir
    (bundle_dir / "manifest.json").unlink()
    sub = bundle_dir / "persona_x"
    sub.mkdir()
    (sub / "manifest.json").write_text("{}", encoding="utf-8")
    assert persona_store.find_bundle_root(bundle_dir) == sub
    (sub / "manifest.json").unlink()
    with pytest.raises(BundleError):
        persona_store.find_bundle_root(bundle_dir)

    configs: dict[str, StringConfig] = {
        "sp1": _persona_cfg(tmp_path, "sp1", "specific", ["g1"]),
        "gg": _persona_cfg(tmp_path, "gg", "global_group"),
        "gp": _persona_cfg(tmp_path, "gp", "global_private"),
        "gl": _persona_cfg(tmp_path, "gl", "global"),
        "off": _persona_cfg(tmp_path, "off", "disabled"),
    }
    assert pick_default_persona(configs) == "gl"
    assert pick_default_persona({k: v for k, v in configs.items() if k != "gl"}) == "gg"
    assert pick_default_persona({k: v for k, v in configs.items() if k not in ("gl", "gg")}) == "gp"
    assert (
        pick_default_persona(
            {
                "za": _persona_cfg(tmp_path, "za", "specific", ["g1"]),
                "zc": _persona_cfg(tmp_path, "zc", "specific", ["g1", "g2", "g3"]),
            }
        )
        == "zc"
    )


def test_persona_isolation_for_relationship_cognition_presence_and_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gsuid_core.config import core_config
    from gsuid_core.ai_core.relationship import fetch_relationship
    from gsuid_core.ai_core.self_cognition import add_self_note, build_self_cognition_context
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.persona.presence import PresenceRibbon, render_presence
    from gsuid_core.ai_core.relationship.engine import settle_semantic
    from gsuid_core.ai_core.command_aside.recall import save_advice, lookup_advice
    from gsuid_core.ai_core.relationship.signals import NegSignal

    target = tmp_path / "aside.json"
    monkeypatch.setattr("gsuid_core.ai_core.command_aside.recall._path", lambda: target)
    save_advice(plugin="p", trigger_id="t1", receipt="命令 foo 执行成功", advice="用中文解释结果", persona_name="A")
    assert lookup_advice("p", "命令 foo 执行成功", "t1", "A") == "用中文解释结果"
    assert lookup_advice("p", "命令 foo 执行成功", "t1", "B") == ""

    # off-screen 行踪只投放到同一对话：他群/他人私聊一律不渲染
    now = 1_800_000_000.0
    r_group_a = PresenceRibbon(
        offscreen_doing="正在整理测试群A季度报告",
        offscreen_at=now - 600.0,
        offscreen_scope="group_a",
        offscreen_user_id="100000001",
    )
    r_legacy = PresenceRibbon(offscreen_doing="旧无归属动作", offscreen_at=now - 600.0, offscreen_scope="")
    for ribbon, target_key, is_private, uid, expected_sub in (
        (r_group_a, "group_a", False, "", "季度报告"),
        (r_group_a, "group_b", False, "", ""),
        (r_group_a, "private:100000002", True, "100000002", ""),
        (r_group_a, "private:100000001", True, "100000001", "季度报告"),
        (r_legacy, "group_b", False, "", ""),
    ):
        rendered = render_presence(ribbon, "", target_key=target_key, is_private=is_private, now=now, user_id=uid)
        assert (expected_sub in rendered) if expected_sub else (rendered == "")

    async def _noop_distill(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("gsuid_core.ai_core.cognition.distill.distill_self_note", _noop_distill)

    async def scenario() -> None:
        monkeypatch.setattr("gsuid_core.ai_core.state_store.store._table_ensured", True)
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="persona_iso.db") as (_, maker):
            async with maker() as session:
                session.add(UserFavorability(user_id="u1", bot_id="b1", persona_name="A", favorability=85))
                await session.commit()

            view_a = await fetch_relationship("u1", "b1", "A")
            assert view_a.scored and view_a.score == 85
            assert not (await fetch_relationship("u1", "b1", "B")).scored

            assert await add_self_note("b1", "A", "我对用户承诺过每天汇报", "self_notes")
            assert "每天汇报" in await build_self_cognition_context("b1", "A", include_relationship=False)
            assert "每天汇报" not in await build_self_cognition_context("b1", "B", include_relationship=False)

            # 负面关系信号对主人免疫，对普通用户按信号等级扣减
            monkeypatch.setitem(core_config.config, "masters", ["master_123"])
            for uid, signal, expected_delta in (
                ("master_123", NegSignal.HOSTILE, 0),
                ("u_456", NegSignal.PROVOKE, -3),
                ("u_456", NegSignal.HOSTILE, -6),
            ):
                res = await settle_semantic(user_id=uid, bot_id="b1", persona_name="A", signal=signal, evidence="test")
                assert res.delta == expected_delta

    asyncio.run(scenario())


def test_journal_compaction_and_active_hours_gating(
    tmp_path: Path,
    journal_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手账跨日整理：直跑真实 synthesize_shard_with_llm（离线 TestModel）、429 保护、规则兜底与作息门禁。"""
    from gsuid_core.ai_core import session_logger as sl_mod
    from gsuid_core.ai_core.configs import models as cfg_models
    from gsuid_core.ai_core.journal import dream as jd, manager as jm, storage as jstorage
    from gsuid_core.ai_core.configs.ai_config import ai_config

    monkeypatch.setattr(sl_mod, "AI_SESSION_LOGS_PATH", tmp_path / "session_logs")
    monkeypatch.setattr(sl_mod, "AI_SUBAGENT_LOGS_PATH", tmp_path / "session_logs" / "subagents")
    monkeypatch.setattr(
        "gsuid_core.ai_core.autonomy.tools.artifact_dir",
        lambda pname, root=None: tmp_path / "notes" / pname,
    )

    async def _gt(persona_name: str, shard_id: str, now_ts: float) -> jd.GroundTruthDigest:
        del persona_name, shard_id, now_ts
        return jd.GroundTruthDigest(completed=("测试项目报告",))

    monkeypatch.setattr(jd, "collect_ground_truth", _gt)
    monkeypatch.setattr(cfg_models, "get_config_name_for_task", lambda _lvl, _kind=None: "test_high")
    monkeypatch.setattr(ai_config.get_config("enable"), "data", True)

    def _set_dream_model(output_text: str) -> None:
        m = TestModel(call_tools=[], custom_output_text=output_text)
        monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _l, _k=None: m)
        monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_by_full_name", lambda _n: m)

    async def scenario() -> None:
        _set_dream_model(
            json.dumps(
                {
                    "persistent": ["保持核实后再作答的习惯。"],
                    "monthly": ["[已完结] 测试项目报告 已闭环交付。"],
                    "yesterday": ["做梦整理：已跟进各项事务。"],
                },
                ensure_ascii=False,
            )
        )
        assert await jd.compact_journal("tester") is False

        # 旧版单文件写盘 → 自动无损拆迁至 self.md 与 groups/group_1001.md
        legacy_file = journal_tmp / "tester.md"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_text(
            "# tester的生活手账\n\n"
            "## 常驻状态与近期小结\n"
            "- 目前一切正常，日常处理群聊交互与各项委派。\n\n"
            "## 今日行动流水\n"
            "- [10:00 | 查天气] 查了测试城市天气 (群:group_1001)\n"
            "- [11:30 | 自主探索] 整理了个人读书摘要\n",
            encoding="utf-8",
        )

        assert await jd.compact_journal("tester", force=True) is True
        assert not legacy_file.exists()

        self_path = jm.get_journal_path("tester")
        grp_path = jm.get_journal_path("tester", group_id="group_1001")
        assert self_path.exists() and grp_path.exists()

        self_doc = jstorage.parse_shard_markdown(self_path.read_text(encoding="utf-8"))
        grp_doc = jstorage.parse_shard_markdown(grp_path.read_text(encoding="utf-8"), "group_1001")
        assert len(self_doc.timeline) == 0 and len(grp_doc.timeline) == 0
        assert any("做梦整理：已跟进各项事务" in item for item in self_doc.yesterday)
        assert any("[已完结] 测试项目报告" in item for item in grp_doc.monthly)
        assert jstorage.get_last_compact_ts("tester") > 0.0

        # 429/错误回执经真实 synthesize_shard_with_llm 判为 transient_error，非强制整理绝不清空流水
        await jm.record_journal_entry(
            jstorage.JournalEntry(
                timestamp=time.time(),
                date_str="2026-11-17",
                time_str="14:00",
                tag="查资料",
                summary="核对了第一份数据；核对了第二份数据；核对了第三份数据。",
                group_id="group_1001",
            ),
            target_personas=["tester"],
        )
        _set_dream_model("执行出错: 429 rate limit exceeded")
        assert await jd.compact_journal("tester", force=False) is False
        assert len(jstorage.parse_shard_markdown(grp_path.read_text(encoding="utf-8"), "group_1001").timeline) == 1

        # AI 关闭时 synthesize_shard_with_llm 返回 None，走确定性规则跨日兜底并写入归档
        monkeypatch.setattr(ai_config.get_config("enable"), "data", False)
        assert await jd.compact_journal("tester", force=True) is True
        fb_doc = jstorage.parse_shard_markdown(grp_path.read_text(encoding="utf-8"), "group_1001")
        fb_summary = "\n".join((*fb_doc.persistent, *fb_doc.monthly, *fb_doc.yesterday))
        assert len(fb_doc.timeline) == 0
        assert "核对了第一份数据" in fb_summary and "journal:tester:group_1001:" in fb_summary
        assert await jd.compact_journal("tester") is False

        # 作息窗口门禁与自然睡眠日去重
        await jm.record_journal_entry(
            jstorage.JournalEntry(
                timestamp=time.time(),
                date_str="2026-11-17",
                time_str="20:10",
                tag="自主探索",
                summary="晚间记录了一条新的自主观察。",
            ),
            target_personas=["tester"],
        )
        monkeypatch.setattr(jd, "is_within_active_hours", lambda *args, **kwargs: True)
        await jd.check_and_compact_all_journals()
        assert len(jstorage.parse_shard_markdown(self_path.read_text(encoding="utf-8")).timeline) == 1

        def _write_test_compact_ts(ts_val: float) -> None:
            cur = jstorage.read_journal_state_sync("tester")
            jstorage.put_journal_state_sync(
                "tester",
                {
                    "last_compact_ts": ts_val,
                    "last_dream_date": jd.logical_sleep_date("Asia/Shanghai", ts_val),
                    "dirty_shards": list(cur["dirty_shards"]) if cur is not None else [],
                    "light_carry": dict(cur["light_carry"]) if cur is not None else {},
                    "last_gc_month": cur["last_gc_month"] if cur is not None else "",
                    "schema_version": 2,
                },
            )

        monkeypatch.setattr(jd, "is_within_active_hours", lambda *args, **kwargs: False)
        _write_test_compact_ts(time.time() - 3600)
        await jd.check_and_compact_all_journals()
        assert len(jstorage.parse_shard_markdown(self_path.read_text(encoding="utf-8")).timeline) == 1

        _write_test_compact_ts(time.time() - 90000)
        await jd.check_and_compact_all_journals()
        assert len(jstorage.parse_shard_markdown(self_path.read_text(encoding="utf-8")).timeline) == 0

    asyncio.run(scenario())


def test_journal_sanitization_routing_and_shard_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手账清洗、跨群/私聊分片隔离、静默巡检折叠、洪峰定长、增量去重与旧 self 零删除迁移。"""
    import gsuid_core.ai_core.migrations.cross_group_isolation_v1 as mig_v1
    from gsuid_core.ai_core.journal import dream as jd, manager as jm, storage as jstorage, sanitizer as js
    from gsuid_core.ai_core.autonomy.models import FactPack, DeliveryPin, DeliveryDecision, AutonomyStepTrace
    from gsuid_core.ai_core.agent_run.prepare import _journal_delta_in_recent_history
    from gsuid_core.ai_core.autonomy.delivery import parse_footer
    from gsuid_core.ai_core.autonomy.scheduler import _record_autonomy_journal

    _setup_isolated_persona_env(tmp_path / "personas_cfg", monkeypatch)
    _seed_persona(tmp_path / "personas_cfg", "alpha", "specific", ["group_1", "group_hot", "group_quiet"])
    journal_dir, _ = setup_journal_env(tmp_path, monkeypatch, patch_autonomy_notes=True)

    # 1. 句界/ASCII 词元截断保护与噪声拦截
    raw_rsi = (
        "查了一下群友提到的测试新材，抓到的数据是：页面（11-13 更新）显示昨收 43.44，"
        "当日区间 42.92~46.82，近一年涨幅约 148%，RSI(14) 41.21，市值约 69.5 亿。"
    )
    cut_rsi = js.sanitize_journal_summary(raw_rsi, max_chars=80)
    assert not cut_rsi.endswith("R") and not cut_rsi.endswith("R…")

    raw_md = "官方机器人平台翻了翻，频道和群聊都能跑，消息能收也能发，支持文本、Markdown、图片、语音和文件。"
    cut_md = js.sanitize_journal_summary(raw_md, max_chars=46)
    assert "Markd…" not in cut_md and not cut_md.endswith("Markd")

    flat = js.sanitize_journal_summary(
        "<reply #12>\n第一段结论核实完毕\n第二段补充细节 <@100000001>",
        max_chars=110,
        nicks={"100000001": "测试用户A"},
    )
    assert "\n" not in flat and "@测试用户A" in flat
    for rejected_body in (
        "执行出错: 模型套餐用量已达上限",
        "这次的定时任务没能执行成功，稍后再让我试试吧",
        "DRIVE: group_topic\nTARGET: group_1",
    ):
        assert js.is_rejected_journal_body(rejected_body) is True

    # 日志经历去重
    hist_j = [ModelResponse(parts=[TextPart(content="刚刚帮测试用户A查了今日天气，结果是晴天")])]
    two_seg = (
        "（系统提示：刚刚经历：[12:00 | 闲聊] 刚刚帮测试用户A查了今日天气，结果是晴天；"
        "[12:01 | 闲聊] 全新的另一条未复述事项记录）"
    )
    for delta_text, expected in (
        ("刚刚帮测试用户A查了今日天气，结果是晴天", True),
        ("完全不同的另一件新日程安排记录", False),
        (two_seg, False),
    ):
        assert _journal_delta_in_recent_history(delta_text, hist_j) is expected

    async def scenario() -> None:
        import gsuid_core.ai_core.cognition.nodes as cog_nodes

        assert cog_nodes.AICogNode is not None
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="journal_iso.db"):
            await _run_scenario()

    async def _run_scenario() -> None:
        now = time.time()

        # 2. 路由归属与未绑定群拒绝
        assert jm.resolve_persona_for_group_or_session(persona_name=None, group_id="group_1") == "alpha"
        assert jm.resolve_persona_for_group_or_session(persona_name="deleted_ghost", group_id="group_1") == "alpha"
        await jm.record_journal_entry(
            jstorage.JournalEntry(
                now, "2026-11-18", "09:00", "查资料", "未绑定群的流水不应污染其它人格", group_id="group_unassigned"
            )
        )
        assert not (journal_dir / "personas" / "alpha" / "groups" / "group_unassigned.md").exists()

        # 3. Autonomy parse_footer 写入 FACT+TAKE+句柄，隔离私聊关怀与 429 报错
        async def _record_auto_j(
            raw_text: str,
            *,
            decision: DeliveryDecision,
            facts: FactPack,
            mode: str,
            spoken_text: str = "",
            topic: str = "",
            fact: str = "",
            take: str = "",
            steps: list[AutonomyStepTrace] | None = None,
            pin: DeliveryPin | None = None,
            tool_sent: bool = False,
            last_sent_user_id: str = "",
        ) -> None:
            await _record_autonomy_journal(
                persona_name="alpha",
                raw_text=raw_text,
                steps=list(steps or []),
                spoken_text=spoken_text,
                topic=topic,
                fact=fact,
                take=take,
                decision=decision,
                facts=facts,
                pin=pin,
                mode=mode,
                tool_sent=tool_sent,
                last_sent_group_id="",
                last_sent_user_id=last_sent_user_id,
            )

        footer_only_out = (
            "DRIVE: group_topic\nTARGET: group_1\nTOPIC: 隔夜指标盘点\nOUTCOME: found\n"
            "FACT: 三大指数回落，波动率抬升\nTAKE: 数字看多了眼花，先记在本子上。"
        )
        parsed = parse_footer(footer_only_out)
        step = AutonomyStepTrace(
            step_index=0,
            tool_name="artifact_put",
            args_summary=('{"summary": "隔夜盘点小记", "body": "' + "详细核查记录文本" * 80)[:500],
            output_text="已写入笔记《隔夜盘点小记》（句柄: note:alpha:隔夜盘点小记）",
        )
        pin = DeliveryPin(
            "", "group_1", False, "familiar", "onebot", "10001", "ws1", "group:group_1", last_human_ts=now - 60
        )
        await _record_auto_j(
            footer_only_out,
            steps=[step],
            spoken_text=parsed.spoken_text,
            topic=parsed.topic,
            fact=parsed.fact,
            take=parsed.take,
            decision=DeliveryDecision("group_topic", "staged_merge", pin, parsed.summary),
            facts=FactPack(["artifact_put"], ["ok"], [], True),
            pin=pin,
            mode="staged_merge",
        )
        dm_pin = DeliveryPin(
            "100000001", "", False, "intimate", "onebot", "10001", "ws1", "direct:100000001", last_human_ts=now - 60
        )
        await _record_auto_j(
            "给你带了一句私聊提醒",
            spoken_text="给你带了一句私聊提醒",
            topic="私聊问候",
            decision=DeliveryDecision("care_user", "immediate_push", dm_pin, "给你带了一句私聊提醒"),
            facts=FactPack(["send_message_by_ai"], ["ok"], [], True),
            pin=dm_pin,
            mode="immediate_push",
            tool_sent=True,
            last_sent_user_id="100000001",
        )
        await _record_auto_j(
            "缺少目标用户的私聊关怀不应污染公共手账",
            spoken_text="缺少目标用户的私聊关怀不应污染公共手账",
            topic="空目标",
            decision=DeliveryDecision("care_user", "silent_only", None, "空目标测试"),
            facts=FactPack(["web_search_tool"], ["ok"], [], True),
            mode="silent_only",
        )
        await _record_auto_j(
            "执行出错: 模型套餐用量已达上限",
            spoken_text="执行出错: 模型套餐用量已达上限",
            decision=DeliveryDecision("rest", "silent_only", None, ""),
            facts=FactPack([], [], [], False),
            mode="silent_only",
        )
        assert not (journal_dir / "personas" / "alpha" / "self.md").exists()
        u1_path = jstorage._user_shard_path("alpha", "100000001")
        assert u1_path.exists() and "给你带了一句私聊提醒" in u1_path.read_text(encoding="utf-8")
        g1_text, _ = jm.read_journal_for_context("alpha", current_group_id="group_1")
        assert "DRIVE:" not in g1_text and "用量已达上限" not in g1_text
        assert "三大指数回落" in g1_text and "note:alpha:隔夜盘点小记" in g1_text

        # 4. 跨行静默巡检折叠与跨日残留过滤
        await jm.record_journal_entry(
            jstorage.JournalEntry(now, "2026-11-18", "09:30", "周期巡检", "<<NO_BROADCAST>>", group_id="group_quiet"),
            target_personas=["alpha"],
        )
        await jm.record_journal_entry(
            jstorage.JournalEntry(
                now + 1, "2026-11-18", "10:00", "查资料", "帮群友查了测试条目。", group_id="group_quiet"
            ),
            target_personas=["alpha"],
        )
        _, ts_after_substantive = jm.read_journal_for_context("alpha", current_group_id="group_quiet")
        await jm.record_journal_entry(
            jstorage.JournalEntry(
                now + 2, "2026-11-18", "10:30", "周期巡检", "（本轮无值得播报的进展）", group_id="group_quiet"
            ),
            target_personas=["alpha"],
        )
        delta_after_silent, _ = jm.read_journal_for_context(
            "alpha", current_group_id="group_quiet", since_ts=ts_after_substantive
        )
        assert delta_after_silent == ""
        gq_doc = jstorage.parse_shard_markdown(jm.get_journal_path("alpha", "group_quiet").read_text(encoding="utf-8"))
        assert len(gq_doc.timeline) == 2 and "<<NO_BROADCAST>>" not in "\n".join(gq_doc.timeline)
        assert "(×2)" in gq_doc.timeline[-1] and "09:30~10:30" in gq_doc.timeline[-1]

        today_str = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        cur_hhmm = datetime.fromtimestamp(now).strftime("%H:%M")
        stale_path = jm.get_journal_path("alpha", "group_rollover")
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_text(
            f"# alpha的驻群手账 (群:group_rollover)\n\n"
            f"## 本群今日行动流水 ({today_str})\n"
            f"- [23:55 | 交付完结] 昨夜遗留的旧播报内容。\n"
            f"- [00:05 | 查资料] 今日早间已完成的新记录。\n"
            f"- [{cur_hhmm} | 查资料] 今日当前已完成记录。\n",
            encoding="utf-8",
        )
        jstorage.forget_persona_cache_sync("alpha")
        delta_roll, _ = jm.read_journal_for_context("alpha", current_group_id="group_rollover", since_ts=now - 30.0)
        assert "昨夜遗留的旧播报内容" not in delta_roll
        anchor_roll, _ = jm.read_journal_for_context("alpha", current_group_id="group_rollover")
        assert "昨夜遗留的旧播报内容" not in anchor_roll and "今日当前已完成记录" in anchor_roll

        # 5. 大群洪峰环形定长 + 私聊/群分片按 (替:uid) 与 (群:gid) 隔离 + 旧 self 零删除迁移
        for i in range(75):
            await jm.record_journal_entry(
                jstorage.JournalEntry(
                    now + 10 + i,
                    "2026-11-18",
                    f"11:{i % 60:02d}",
                    "查资料" if i % 2 == 0 else "看图辨认",
                    f"回应了大群第 {i} 次高频资料核验请求并给出结论。",
                    group_id="group_hot",
                ),
                target_personas=["alpha"],
            )
        gh_doc = jstorage.parse_shard_markdown(jm.get_journal_path("alpha", "group_hot").read_text(encoding="utf-8"))
        assert len(gh_doc.timeline) <= jstorage.GROUP_TIMELINE_RING_CAP
        hot_anchor, _ = jm.read_journal_for_context("alpha", current_group_id="group_hot")
        quiet_anchor, _ = jm.read_journal_for_context("alpha", current_group_id="group_quiet")
        dm_anchor, _ = jm.read_journal_for_context("alpha", current_group_id=None)
        assert len(hot_anchor) <= jstorage.ANCHOR_TOTAL_MAX_CHARS and "早先概览" in hot_anchor
        assert "高频资料核验" not in quiet_anchor and "高频资料核验" not in dm_anchor

        # 私聊分片四层视图只给本人；群手账 (替:uid) 与 self 时间线按边界隔离
        await jm.record_journal_entry(
            jstorage.JournalEntry(
                now, "2026-11-21", "10:30", "查资料", "私聊专属健康清单", group_id="", requested_by="100000001"
            ),
            target_personas=["iso_p"],
        )
        u_iso_path = jstorage._user_shard_path("iso_p", "100000001")
        u_iso_raw = u_iso_path.read_text(encoding="utf-8")
        assert "的私聊手账 (用户:100000001)" in u_iso_raw and "驻群手账" not in u_iso_raw
        u_iso_doc = jstorage.parse_shard_markdown(u_iso_raw, jstorage._user_shard_id("100000001"))
        u_iso_doc.yesterday = ["- 昨日私聊整理了体检报告"]
        u_iso_doc.monthly = ["- 本月私聊持续跟进健身计划"]
        jstorage._save_shard_sync("iso_p", u_iso_doc)
        jstorage._SHARD_CACHE.clear()
        dm_view = jm.read_life_journal_view("iso_p", group_id=None, user_id="100000001")
        assert "私聊专属健康清单" in dm_view and "昨日私聊整理了体检报告" in dm_view and "跟进健身计划" in dm_view
        assert "私聊专属健康清单" not in jm.read_life_journal_view("iso_p", group_id=None, user_id="100000002")
        assert "私聊专属健康清单" not in jm.read_life_journal_view("iso_p", group_id="group_a", user_id="100000001")

        g_iso = jstorage._group_shard_path("iso_p", "group_a")
        g_iso.parent.mkdir(parents=True, exist_ok=True)
        g_iso.write_text(
            "# iso_p 驻群手账 (群:group_a)\n\n## 今日行动流水 (2026-11-21)\n"
            "- [10:00 | 查资料] 帮A查了天气 (替:100000001)\n"
            "- [10:05 | 查资料] 帮B查了私密账本 (替:100000002)\n",
            encoding="utf-8",
        )
        dm_j = jm.read_life_journal_view("iso_p", group_id=None, user_id="100000001", allowed_group_ids=[])
        assert "帮A查了天气" in dm_j and "私密账本" not in dm_j
        assert "帮A查了天气" not in jm.read_life_journal_view("iso_p", group_id="group_b", user_id="100000001")

        self_iso = jstorage._self_shard_path("iso_p")
        self_iso.write_text(
            "# iso_p 生活手账 (全局自我核)\n\n## 今日行动流水 (2026-11-21)\n"
            "- [11:00 | 自主探索] 自己看了一会书\n"
            "- [11:10 | 查资料] 帮用户A整理私密清单 (替:100000001)\n"
            "- [12:00 | 查资料] 群内通用事 (群:group_a)\n",
            encoding="utf-8",
        )
        jstorage._SHARD_CACHE.clear()
        assert "私密清单" not in jm.read_life_journal_view("iso_p", group_id="group_a", user_id="100000001")
        assert "群内通用事" in jm.read_life_journal_view("iso_p", group_id="group_a", user_id="100000001")
        assert "群内通用事" not in jm.read_life_journal_view("iso_p", group_id="group_b", user_id="100000001")
        assert "私密清单" in jm.read_life_journal_view("iso_p", group_id=None, user_id="100000001")
        assert "群内通用事" not in jm.read_life_journal_view("iso_p", group_id=None, user_id="100000001")
        assert "私密清单" not in jm.read_life_journal_view("iso_p", group_id=None, user_id="100000002")

        # 旧 self.md 零删除归档迁移 + 幂等 + 水位线后新人格不动（独立手账根目录，不与其他分片混扫）
        with monkeypatch.context() as mp:
            setup_journal_env(tmp_path / "legacy_migration", mp)
            legacy_self = jstorage._self_shard_path("legacy_iso_p")
            legacy_self.parent.mkdir(parents=True, exist_ok=True)
            legacy_self.write_text(
                "# legacy_iso_p 生活手账 (全局自我核)\n\n## 今日行动流水 (2026-11-21)\n"
                "- [11:00 | 自主探索] 自己看了一会书\n"
                "- [11:10 | 查资料] 帮用户A整理私密清单 (替:100000001)\n",
                encoding="utf-8",
            )
            mp.setattr(mig_v1, "get_isolation_watermark_ts", lambda: 0.0)
            assert await jm.archive_and_reset_legacy_self_shards() == ["legacy_iso_p"]
            assert (legacy_self.parent / "self.legacy_v1.bak").exists()
            legacy_files = list((legacy_self.parent / "archives").glob("legacy_v1_*.jsonl"))
            assert legacy_files
            month_key = legacy_files[0].stem.removeprefix("legacy_v1_")
            assert (
                await jd.read_journal_archive_sync(legacy_self.parent / "archives", jstorage.SELF_SHARD_ID, month_key)
                == ""
            )
            assert await jm.archive_and_reset_legacy_self_shards() == []

            mp.setattr(mig_v1, "get_isolation_watermark_ts", lambda: 1_000_000.0)
            fresh_self = jstorage._self_shard_path("brand_new_p")
            fresh_self.parent.mkdir(parents=True, exist_ok=True)
            fresh_self.write_text("# brand_new_p\n\n## 持久自我认知与通用经验\n- 全新纯净个人经验\n", encoding="utf-8")
            assert await jm.archive_and_reset_legacy_self_shards() == []
            assert "全新纯净个人经验" in fresh_self.read_text(encoding="utf-8")

    try:
        asyncio.run(scenario())
    finally:
        clear_journal_caches()


def test_journal_dream_tiers_notes_gc_and_ground_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """做梦三级阀门、双 ID 归档句柄、路径穿越防御、笔记 Wiki 整编、60 天休眠唤醒与真值对账。"""
    from gsuid_core.ai_core.journal import dream as jd, manager as jm, storage as jstorage
    from gsuid_core.ai_core.database.models import AIAutonomyActivity
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.planning.handle_resolver import resolve_handle

    _setup_isolated_persona_env(tmp_path / "personas_cfg", monkeypatch)
    _seed_persona(tmp_path / "personas_cfg", "alpha", "specific", ["group_1", "group_hot", "villa_1:room_9"])
    _, notes_root = setup_journal_env(tmp_path, monkeypatch, patch_autonomy_notes=True)

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="dream_tiers.db") as (_, maker):
            now = time.time()
            await jm.record_journal_entry(
                jstorage.JournalEntry(now, "2026-11-18", "09:00", "查资料", "三大指数回落复盘。", group_id="group_1"),
                target_personas=["alpha"],
            )
            await jm.record_journal_entry(
                jstorage.JournalEntry(
                    now + 1,
                    "2026-11-18",
                    "09:05",
                    "自主探索",
                    "给你带了一句私聊提醒",
                    group_id="",
                    requested_by="100000001",
                ),
                target_personas=["alpha"],
            )
            await jm.record_journal_entry(
                jstorage.JournalEntry(
                    now + 2, "2026-11-18", "12:00", "查资料", "核查了双ID房间的测试配置。", group_id="villa_1:room_9"
                ),
                target_personas=["alpha"],
            )
            for i in range(55):
                await jm.record_journal_entry(
                    jstorage.JournalEntry(
                        now + 10 + i,
                        "2026-11-18",
                        f"11:{i % 60:02d}",
                        "查资料",
                        f"回应大群第 {i} 次资料核验并总结结论。",
                        group_id="group_hot",
                    ),
                    target_personas=["alpha"],
                )

            heavy_llm_shards: list[str] = []

            async def _track_llm(
                persona_name: str,
                doc: jstorage.ShardDocument,
                *,
                ground_truth: jd.GroundTruthDigest,
                prev_month_rollover: Sequence[str] = (),
            ) -> jd.DreamLLMOutcome:
                del persona_name, ground_truth, prev_month_rollover
                heavy_llm_shards.append(doc.shard_id)
                if doc.shard_id == "group_hot":
                    jstorage.put_journal_state_sync(
                        "beta",
                        {
                            "last_compact_ts": 0.0,
                            "last_dream_date": "",
                            "dirty_shards": ["group_concurrent"],
                            "light_carry": {},
                            "last_gc_month": "",
                            "schema_version": 2,
                        },
                    )
                return {
                    "persistent": ["高频核查保持结论先行。"],
                    "monthly": ["本月持续跟进大群高频测试专题。"],
                    "yesterday": [
                        "昨日从早到晚密集完成了数十次资料比对、图片文字核验与多轮统计口径复核，确保各项数据准确无误。"
                    ],
                }

            monkeypatch.setattr(jd, "synthesize_shard_with_llm", _track_llm)
            assert await jd.compact_journal("alpha", force=False) is True
            assert "group_hot" in heavy_llm_shards
            assert "group_1" not in heavy_llm_shards and "villa_1:room_9" not in heavy_llm_shards
            beta_state = jstorage.read_journal_state_sync("beta")
            assert beta_state is not None and "group_concurrent" in beta_state["dirty_shards"]

            gh_after = jstorage.parse_shard_markdown(
                jm.get_journal_path("alpha", "group_hot").read_text(encoding="utf-8"), "group_hot"
            )
            sleep_day = gh_after.yesterday_label
            assert f"journal:alpha:group_hot:{sleep_day}" in gh_after.yesterday[-1]

            resolved_dual = await resolve_handle(f"journal:alpha:villa_1:room_9:{sleep_day}")
            assert resolved_dual is not None and "双ID房间" in (resolved_dual.payload_inline or "")
            assert await resolve_handle("journal:..:self:2026-11-18") is None
            assert await resolve_handle("journal:alpha:group_1:../../etc") is None

            resolved_arch = await resolve_handle(f"journal:alpha:group_1:{sleep_day}")
            assert resolved_arch is not None and "三大指数回落" in (resolved_arch.payload_inline or "")

            # 个人笔记 Wiki 整编 + 超 60 天休眠分片迁入与自动唤醒
            alpha_notes = notes_root / "alpha"
            alpha_notes.mkdir(parents=True, exist_ok=True)
            scope_hdr = "<!-- gscore-scope: self:1:alpha | owner: 100000001 -->\n"
            for idx, suffix in enumerate(("小抄", "小笔记", "调查")):
                (alpha_notes / f"体态拉伸_{suffix}.txt").write_text(
                    f"{scope_hdr}第 {idx + 1} 次记录：每日拉伸 15 分钟有助于放松背部姿态。", encoding="utf-8"
                )
            for distinct_topic in ("睡眠规律", "饮水习惯", "桌面收纳"):
                (alpha_notes / f"关于_{distinct_topic}_笔记.txt").write_text(
                    f"{scope_hdr}独立主题记录：{distinct_topic}。", encoding="utf-8"
                )
            old_snap = alpha_notes / "2026-10-01_日常速记.txt"
            old_snap.write_text("单日临时快照数据。", encoding="utf-8")
            old_ts = now - 15 * 86400
            os.utime(old_snap, (old_ts, old_ts))

            merged_n, archived_n = await jd.consolidate_persona_notes(
                "alpha", notes_dir=alpha_notes, now_ts=now, allow_llm=False
            )
            assert merged_n == 1 and archived_n == 4
            assert (alpha_notes / "topic_体态拉伸.md").exists() and not (alpha_notes / "topic_关于.md").exists()
            assert not old_snap.exists()
            assert await resolve_handle("note:alpha:topic_体态拉伸") is not None
            assert await resolve_handle("note:alpha:体态拉伸_小抄") is not None

            g1_file = jm.get_journal_path("alpha", "group_1")
            u1_file = jstorage._user_shard_path("alpha", "100000001")
            ancient_ts = now - 70 * 86400
            os.utime(g1_file, (ancient_ts, ancient_ts))
            os.utime(u1_file, (ancient_ts, ancient_ts))
            jstorage.forget_persona_cache_sync("alpha")
            dormant_cnt, _ = await jd.run_storage_gc_sync(jstorage.persona_journal_dir("alpha"), now)
            assert dormant_cnt >= 2 and not g1_file.exists() and not u1_file.exists()
            assert jstorage._dormant_user_path("alpha", "100000001").exists()
            woken_text, _ = jm.read_journal_for_context("alpha", current_group_id="group_1")
            assert "三大指数回落" in woken_text and g1_file.exists()
            woken_dm, _ = jm.read_journal_for_context("alpha", current_group_id=None, current_user_id="100000001")
            assert "给你带了一句私聊提醒" in woken_dm and u1_file.exists()

            resolved_dm_arch = await resolve_handle(f"journal:alpha:user:100000001:{sleep_day}")
            assert resolved_dm_arch is not None
            assert resolved_dm_arch.scope_key == "user:100000001" and resolved_dm_arch.owner_user_id == "100000001"
            dm_rule_slots = jd.rule_rollover_shard(
                jstorage.ShardDocument(shard_id="user:100000001", timeline=["- [10:00 | 自主探索] 提醒休息"]),
                persona_name="alpha",
                sleep_date=sleep_day,
                current_month=sleep_day[:7],
                ground_truth=jd.GroundTruthDigest(),
            )
            assert all("群聊" not in item for item in dm_rule_slots["persistent"])

            # 实库真值对账（completed 入已完结，>24h 僵尸审批不进 in_flight，私聊按人格隔离）
            now_dt = datetime.fromtimestamp(now)
            async with maker() as s:
                s.add_all(
                    [
                        AIAgentTask(
                            id="t_done",
                            goal="完成分析",
                            display_name="年度报表核查",
                            status="completed",
                            group_id="group_1",
                            updated_at=now_dt - timedelta(hours=2),
                        ),
                        AIAgentTask(
                            id="t_zombie",
                            goal="旧审批",
                            display_name="过期十天审批单",
                            status="waiting_approval",
                            group_id="group_1",
                            updated_at=now_dt - timedelta(hours=30),
                        ),
                        AIAgentTask(
                            id="t_dm_alpha",
                            goal="私聊跟进",
                            display_name="本人格私聊事项",
                            status="completed",
                            persona_name="alpha",
                            owner_user_id="100000001",
                            group_id=None,
                            updated_at=now_dt - timedelta(hours=1),
                        ),
                        AIAgentTask(
                            id="t_dm_beta",
                            goal="私聊跟进",
                            display_name="他人格私聊事项",
                            status="completed",
                            persona_name="beta",
                            owner_user_id="100000001",
                            group_id=None,
                            updated_at=now_dt - timedelta(hours=1),
                        ),
                        AIAutonomyActivity(
                            persona_name="alpha",
                            bot_id="onebot",
                            bot_self_id="1",
                            drive_kind="self_explore",
                            summary="自主整理了读书摘要",
                            created_at=now - 600,
                        ),
                        AIAutonomyActivity(
                            persona_name="alpha",
                            bot_id="onebot",
                            bot_self_id="1",
                            drive_kind="care_user",
                            target_user_id="100000001",
                            target_session_key="private:100000001",
                            summary="私下问候作息进展",
                            created_at=now - 300,
                        ),
                        AIAutonomyActivity(
                            persona_name="alpha",
                            bot_id="onebot",
                            bot_self_id="1",
                            drive_kind="care_user",
                            target_user_id="100000001",
                            target_session_key="group:group_1",
                            summary="群里提过的公共事项",
                            created_at=now - 200,
                        ),
                    ]
                )
                await s.commit()

            gt_g1 = await jd.collect_ground_truth("alpha", "group_1", now)
            assert "年度报表核查" in gt_g1.completed and "过期十天审批单" not in gt_g1.in_flight
            gt_self = await jd.collect_ground_truth("alpha", "__self__", now)
            assert any("读书摘要" in x for x in gt_self.autonomy_facts)
            gt_dm = await jd.collect_ground_truth("alpha", "user:100000001", now)
            assert any("私下问候作息进展" in x for x in gt_dm.autonomy_facts)
            assert all("群里提过的公共事项" not in x for x in gt_dm.autonomy_facts)
            assert "本人格私聊事项" in gt_dm.completed and "他人格私聊事项" not in gt_dm.completed

    try:
        asyncio.run(scenario())
    finally:
        clear_journal_caches()


def test_journal_settle_subagent_proactive_and_webconsole_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子代理落盘回流、主 Agent 结算边界、主动出口入账与 WebConsole 结构化读写。"""
    from gsuid_core.ai_core import session_logger as sl_mod
    from gsuid_core.ai_core.journal import dream as jd, manager as jm, storage as jstorage
    from gsuid_core.ai_core.planning import tool_output_helper as toh
    from gsuid_core.ai_core.proactive import emitter as emitter_mod
    from gsuid_core.ai_core.control.lines import delivery_wake_line
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.proactive.dispatcher import UnifiedProactiveDispatcher
    from gsuid_core.ai_core.buildin_tools.message_sender import send_message_by_ai

    _setup_isolated_persona_env(tmp_path / "personas_cfg", monkeypatch)
    _seed_persona(tmp_path / "personas_cfg", "alpha", "specific", ["group_1"])
    journal_dir, _ = setup_journal_env(tmp_path, monkeypatch, patch_autonomy_notes=True)
    monkeypatch.setattr(sl_mod, "AI_SESSION_LOGS_PATH", tmp_path / "session_logs")
    monkeypatch.setattr(sl_mod, "AI_SUBAGENT_LOGS_PATH", tmp_path / "session_logs" / "subagents")

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="journal_settle.db"):
            monkeypatch.setattr(toh, "ARTIFACT_ROOT", tmp_path / "artifacts")
            ev_g1 = Event(bot_id="onebot", bot_self_id="1", user_id="100000001", user_type="group", group_id="group_1")
            ev_dm = Event(bot_id="onebot", bot_self_id="1", user_id="100000001", user_type="direct", group_id=None)
            g1_shard_path = jm.get_journal_path("alpha", "group_1")
            u1_file = jstorage._user_shard_path("alpha", "100000001")

            # 1) persist_subagent_result：群任务归属 alpha、私聊写 users/{uid}.md、缺 uid 拒绝落 self.md
            sub_card = await toh.persist_subagent_result(
                profile="research_agent",
                content="完整的子代理调研结论内容，包含多维度测试指标核查与交叉对比结论，总字符数超过四十个字符门槛以便触发手账自动回流落盘流程。",
                task=AIAgentTask(
                    id="t_sub",
                    goal="调研",
                    display_name="调研子任务",
                    persona_name=None,
                    group_id="group_1",
                    scope_key="group:group_1",
                    owner_user_id="100000001",
                ),
                res_handle="res_test1",
            )
            assert sub_card is not None and sub_card.id.startswith("sa_")
            dm_sub_card = await toh.persist_subagent_result(
                profile="research_agent",
                content="私聊专属调研结论汇总，涵盖核心数据交叉对比、阶段进展评估与后续跟进建议，总字符数超过四十个字符门槛以便触发私聊手账自动回流。",
                task=AIAgentTask(
                    id="t_sub_dm",
                    goal="私聊调研",
                    display_name="私聊调研子任务",
                    persona_name="alpha",
                    group_id=None,
                    scope_key="user_global:100000001",
                    owner_user_id="100000001",
                ),
                res_handle="res_test_dm",
            )
            assert dm_sub_card is not None and dm_sub_card.id.startswith("sa_")
            await toh.persist_subagent_result(
                profile="research_agent",
                content="这条缺少属主用户编号的私聊子任务结果绝对不能回退写入公共自我手账文件。",
                task=AIAgentTask(
                    id="t_sub_dm_nouid",
                    goal="缺失属主",
                    display_name="无属主私聊任务",
                    persona_name="alpha",
                    group_id=None,
                    scope_key="user_global:",
                    owner_user_id="",
                ),
                res_handle="res_bad_dm",
            )
            await _drain_background_tasks()
            assert any(sub_card.id in ln for ln in _read_timeline_now(g1_shard_path))
            assert not (journal_dir / "personas" / "default" / "groups" / "group_1.md").exists()
            assert any(dm_sub_card.id in ln and "私聊专属调研结论" in ln for ln in _read_timeline_now(u1_file))
            assert not (journal_dir / "personas" / "alpha" / "self.md").exists()

            # 2) 主 Agent settle 回流边界：Aside/无 ev/闸门拦截不入账；看板结单与私聊答疑入对应分片
            ok_model = TestModel(
                call_tools=[],
                custom_output_text="年度报表核查结果已经全部整理完毕，各项指标均符合预期标准。",
            )
            await _run_offline_agent(
                monkeypatch,
                ok_model,
                "命令旁白长句子测试，即便超过六十个字符的点评内容也不应该被写入驻群生活手账以免大群命令洪峰灌水污染手账记录。",
                create_by="Aside",
                ev=ev_g1,
            )
            await _drain_background_tasks()
            assert not any("年度报表核查结果" in ln for ln in _read_timeline_now(g1_shard_path))

            await _run_offline_agent(
                monkeypatch,
                ok_model,
                "这是一句超过六十个字符的无事件内部调用测试输入，用于验证无事件时不会误入公共自我手账文件。",
                ev=None,
            )
            wake_msg = delivery_wake_line("【子任务交付】任务#1「年度报表核查」已完成。", "", "", allow_render=False)
            await _run_offline_agent(monkeypatch, ok_model, wake_msg, ev=ev_g1, is_framework_injection=True)
            await _drain_background_tasks()
            assert any("交付完结" in ln and "年度报表核查结果" in ln for ln in _read_timeline_now(g1_shard_path))
            assert not (journal_dir / "personas" / "alpha" / "self.md").exists()

            dm_long_model = TestModel(
                call_tools=[],
                custom_output_text=(
                    "这是私聊里超过六十个字符的详细答疑说明，梳理了今天与明天的个人日程安排、"
                    "优先事项清单与关键注意事项，另外留好了晚间复盘时间，按节奏慢慢推进即可。"
                ),
            )
            await _run_offline_agent(
                monkeypatch,
                dm_long_model,
                "帮我理一下这两天私聊安排",
                session_id="ws1:onebot:1:private:100000001",
                ev=ev_dm,
            )
            await _drain_background_tasks()
            assert any("私聊答疑" in ln and "个人日程安排" in ln for ln in _read_timeline_now(u1_file))
            assert not any("个人日程安排" in ln for ln in _read_timeline_now(g1_shard_path))

            # send_message_by_ai 发送两段气泡接短尾语，手账按序保留已发正文与尾语
            sent_bubbles: list[str] = []

            async def _stub_bot_send(self: Bot, message: object, *_a: object, **_kw: object) -> None:
                del self
                sent_bubbles.append(str(message))

            async def _stream_send_then_tail(
                messages: list[ModelMessage], info: AgentInfo
            ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
                del info
                if len(messages) == 1:
                    yield {
                        0: DeltaToolCall(
                            name="send_message_by_ai", json_args='{"text": "先看结论汇总Z"}', tool_call_id="c1"
                        ),
                        1: DeltaToolCall(
                            name="send_message_by_ai",
                            json_args='{"text": "再看分项比例表A，各项比例分开列在表里。"}',
                            tool_call_id="c2",
                        ),
                    }
                else:
                    yield "发好啦~"

            with monkeypatch.context() as mp:
                mp.setattr(Bot, "send", _stub_bot_send)
                await _run_offline_agent(
                    mp,
                    FunctionModel(stream_function=_stream_send_then_tail),
                    "请帮我生成一张比例汇总图",
                    bot=Bot(_Bot("onebot"), ev_g1),
                    ev=ev_g1,
                    tools=[Tool(send_message_by_ai)],
                    return_mode="return",
                )
            await _drain_background_tasks()
            assert len(sent_bubbles) == 2
            assert any(
                "先看结论汇总Z；再看分项比例表A" in ln and "发好啦" in ln for ln in _read_timeline_now(g1_shard_path)
            )

            leak_model = TestModel(
                call_tools=[],
                custom_output_text="<control>内部系统提醒泄露文本超过六十个字符也不得入账</control>",
            )
            for _mode in ("return", "by_bot"):
                await _run_offline_agent(
                    monkeypatch,
                    leak_model,
                    "测试泄漏拦截",
                    bot=Bot(_Bot("onebot"), ev_g1) if _mode == "by_bot" else None,
                    ev=ev_g1,
                    return_mode=_mode,
                )
            await _drain_background_tasks()
            assert not any("内部系统提醒泄露" in ln for ln in _read_timeline_now(g1_shard_path))

            # 3) 主动出口异步入账 + WebConsole 结构化视图与单分片整理
            isolated_dispatcher = UnifiedProactiveDispatcher()
            monkeypatch.setattr(emitter_mod, "get_dispatcher", lambda: isolated_dispatcher)

            async def _noop(*_a: object, **_kw: object) -> None:
                return None

            monkeypatch.setattr(emitter_mod, "send_chat_result", _noop)
            monkeypatch.setattr(emitter_mod, "_sync_to_main_session", _noop)

            async def _emit_sched(ev: Event, msg: str, reason: str) -> bool:
                return await emitter_mod.emit_proactive_message(
                    event=ev,
                    message=msg,
                    source="scheduled_task",
                    trigger_reason=reason,
                    bot=Bot(_Bot("onebot"), ev),
                    persona_name="alpha",
                    suppress_when_heartbeat_recent=False,
                )

            await _emit_sched(ev_g1, "这次的定时任务没能执行成功，稍后再让我试试吧\n⏰ 定时任务 task_fail", "task_fail")
            assert await _emit_sched(ev_g1, "大家早上好，今天天气很清爽。\n⏰ 定时任务: 晨间问候", "task_morning")
            assert await _emit_sched(ev_dm, "私聊晚间提醒：记得按时休息呀。\n⏰ 定时任务: 晚间提醒", "task_evening_dm")
            await _drain_background_tasks()
            tl_after = _read_timeline_now(g1_shard_path)
            assert not any("没能执行成功" in ln for ln in tl_after)
            assert any("定时提醒" in ln and "大家早上好" in ln and "定时任务" not in ln for ln in tl_after)
            assert any("定时提醒" in ln and "私聊晚间提醒" in ln for ln in _read_timeline_now(u1_file))
            assert not (journal_dir / "personas" / "alpha" / "self.md").exists()

            # 先经单分片规则整理写出归档，再校验 WebConsole 结构化视图与归档安全过滤
            async def _no_llm(*_a: object, **_kw: object) -> jd.DreamLLMOutcome:
                return None

            monkeypatch.setattr(jd, "synthesize_shard_with_llm", _no_llm)
            await jm.save_journal_content_async("alpha", g1_shard_path.read_text(encoding="utf-8"), shard_id="group_1")
            assert jstorage.parse_shard_markdown(g1_shard_path.read_text(encoding="utf-8"), "group_1").monthly == []
            assert await jd.compact_journal("alpha", force=True, target_shard="group_1", max_llm_shards=1) is True
            assert len(_read_timeline_now(g1_shard_path)) == 0

            arch_dir = jstorage._archives_dir("alpha")
            (arch_dir / "legacy_v1_2026-10.jsonl").write_text('{"date":"2026-10-01"}\n', encoding="utf-8")
            (arch_dir / "noise.jsonl").write_text("{}", encoding="utf-8")
            structured_view = jm.get_journal_structured("alpha")
            assert any(gs["group_id"] == "group_1" for gs in structured_view["group_shards"])
            assert any(
                u["user_id"] == "100000001" and u["shard_id"] == "user:100000001"
                for u in structured_view["user_shards"]
            )
            assert structured_view["archive_months"] and "noise" not in structured_view["archive_months"]
            assert not structured_view["archive_months"][0].startswith("legacy_v1_")
            arch_recs, arch_months, arch_sel = jm.get_journal_archive_records("alpha", shard_id="group_1")
            assert arch_months == structured_view["archive_months"]
            assert arch_sel and any(r["shard"] == "group_1" and r["entries"] for r in arch_recs)
            bad_recs, _, bad_sel = jm.get_journal_archive_records("alpha", month="../evil")
            assert bad_recs == [] and bad_sel == ""
            compat_md = jm.get_journal_content("alpha")
            assert "### 驻群分片 [群:group_1]" in compat_md

    try:
        asyncio.run(scenario())
    finally:
        clear_journal_caches()


def test_persona_channel_scopes_migration_and_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """旧配置原位迁移、双通道独立互斥、群/私聊绑定、HTTP 覆盖、抢占与自主私聊范围约束。"""
    from msgspec import json as msgjson

    from gsuid_core.ai_core.persona import config as persona_cfg_mod
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.autonomy.select import render_digest, assemble_care_targets
    from gsuid_core.ai_core.journal.storage import get_active_personas
    from gsuid_core.ai_core.http_agent.runtime import set_http_persona_override, clear_http_persona_override
    from gsuid_core.ai_core.persona.config_rules import read_channel_scopes, persona_covers_session

    _setup_isolated_persona_env(tmp_path, monkeypatch)

    legacy_cases = (
        ("mig_global", "global", ["g_keep"], "all", "all"),
        ("mig_gg", "global_group", [], "all", "off"),
        ("mig_gp", "global_private", [], "off", "all"),
        ("mig_spec", "specific", ["group_a", "group_b"], "specific", "off"),
        ("mig_off", "disabled", ["g_old"], "off", "off"),
    )
    for pname, old_scope, old_groups, exp_g, exp_p in legacy_cases:
        pdir = tmp_path / pname
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "persona.md").write_text(f"# {pname}\n", encoding="utf-8")
        (pdir / "config.json").write_bytes(
            msgjson.encode(
                {
                    "scope": GsStrConfig("启用范围", "", old_scope),
                    "target_groups": GsListStrConfig("目标群聊", "", old_groups),
                }
            )
        )
        cfg = persona_config_manager.get_config(pname)
        scopes = read_channel_scopes(cfg)
        assert scopes.group_scope == exp_g and scopes.private_scope == exp_p
        assert cfg.get_config("target_groups").data == old_groups and cfg.get_config("target_users").data == []

    for pname, *_ in legacy_cases:
        persona_config_manager.delete_persona_config(pname)
        (tmp_path / pname / "persona.md").unlink()
        (tmp_path / pname).rmdir()
    persona_config_manager.clear_cache()

    _seed_persona(tmp_path, "dm_and_part_group", "specific", ["group_1"], group_scope="specific", private_scope="all")
    _seed_persona(tmp_path, "all_group_p", "global_group")
    _seed_persona(tmp_path, "beta", "specific", [], group_scope="specific", private_scope="specific")
    _seed_persona(tmp_path, "off", "disabled")

    assert persona_config_manager.set_channel_scopes("beta", private_scope="all")[0] is False
    assert persona_config_manager.set_channel_scopes("beta", group_scope="all")[0] is False

    group_1_sid = "ws1:onebot:10001:group:group_1"
    group_2_sid = "ws1:onebot:10001:group:group_2"
    dm_sid = "ws1:onebot:10001:private:100000001"
    http_sid = "HTTP_AGENT:onebot:s1:group:group_1"

    assert persona_config_manager.get_persona_for_session(group_1_sid) == "dm_and_part_group"
    assert persona_config_manager.get_persona_for_session(group_2_sid) == "all_group_p"
    assert persona_config_manager.get_persona_for_session(dm_sid) == "dm_and_part_group"

    dm_cfg = persona_config_manager.get_config("dm_and_part_group")
    assert persona_covers_session(dm_cfg, group_id="group_1") is True
    assert persona_covers_session(dm_cfg, group_id="group_2") is False
    assert persona_covers_session(dm_cfg, user_id="100000001") is True

    assert persona_config_manager.assign_target_to_persona("beta", "group_1", is_private=False)[0] is True
    persona_config_manager.clear_cache()
    assert "group_1" not in persona_config_manager.get_config("dm_and_part_group").get_config("target_groups").data
    assert "group_1" in persona_config_manager.get_config("beta").get_config("target_groups").data
    assert persona_config_manager.get_persona_for_session(group_1_sid) == "beta"

    assert persona_config_manager.assign_target_to_persona("all_group_p", "group_1", is_private=False)[0] is True
    assert "group_1" not in persona_config_manager.get_config("beta").get_config("target_groups").data
    assert persona_config_manager.get_persona_for_session(group_1_sid) == "all_group_p"

    assert persona_config_manager.assign_target_to_persona("beta", "100000001", is_private=True)[0] is True
    assert "100000001" in persona_config_manager.get_config("beta").get_config("target_users").data
    assert persona_config_manager.get_persona_for_session(dm_sid) == "beta"
    assert persona_config_manager.assign_target_to_persona("all_group_p", "100000001", is_private=True)[0] is False
    assert persona_config_manager.assign_target_to_persona("off", "group_1", is_private=False)[0] is False

    set_http_persona_override(http_sid, "dm_and_part_group")
    try:
        assert persona_config_manager.get_persona_for_session(http_sid) == "dm_and_part_group"
        assert persona_config_manager.get_persona_for_session(group_1_sid) == "all_group_p"
        assert persona_config_manager.get_persona_for_session(dm_sid) == "beta"
    finally:
        clear_http_persona_override(http_sid)

    # 旧客户端回传 scope='specific' 不抹除 private_scope='all'；原子抢占只关冲突通道
    assert persona_config_manager.set_scope("dm_and_part_group", "specific")[0] is True
    idem_scopes = read_channel_scopes(persona_config_manager.get_config("dm_and_part_group"))
    assert idem_scopes.group_scope == "specific" and idem_scopes.private_scope == "all"

    assert persona_config_manager.set_channel_scopes("beta", private_scope="all", takeover_conflicts=True)[0] is True
    after_prev = read_channel_scopes(persona_config_manager.get_config("dm_and_part_group"))
    assert after_prev.group_scope == "specific" and after_prev.private_scope == "off"
    assert read_channel_scopes(persona_config_manager.get_config("beta")).private_scope == "all"

    active_journal_personas = get_active_personas()
    assert "beta" in active_journal_personas and "all_group_p" in active_journal_personas
    assert "off" not in active_journal_personas

    # 旧配置迁移写盘失败时必须经公共入口抛错，严禁静默重置为 off
    fail_mig_dir = tmp_path / "mig_io_err"
    fail_mig_dir.mkdir(parents=True, exist_ok=True)
    (fail_mig_dir / "persona.md").write_text("# mig_io_err\n", encoding="utf-8")
    (fail_mig_dir / "config.json").write_bytes(msgjson.encode({"scope": GsStrConfig("启用范围", "", "global")}))

    def _boom_atomic_save(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk locked")

    with monkeypatch.context() as mp:
        mp.setattr(persona_cfg_mod, "atomic_save", _boom_atomic_save)
        mp.setattr(persona_cfg_mod.time, "sleep", lambda _s: None)
        with pytest.raises(OSError, match="disk locked"):
            persona_config_manager.get_config("mig_io_err")

    care = assemble_care_targets(
        ["100000001", "100000002"],
        {"100000001": 90, "100000002": 90},
        {"100000002"},
        [],
        {},
        {"100000001": "测试用户A", "100000002": "测试用户B"},
        now=1000.0,
        allowed_groups=set(),
        allow_private=True,
        allowed_private_users={"100000001"},
    )
    by_uid = {item.user_id: item for item in care}
    assert by_uid["100000001"].private_scope_allowed is True and by_uid["100000001"].allow_private_push is True
    assert by_uid["100000002"].private_scope_allowed is False and by_uid["100000002"].allow_private_push is False
    assert "私聊未开放" in render_digest(_make_digest("beta", care_targets=care), now=1000.0)


def test_persona_bundle_import_export_copy_and_config_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整包导入/导出脱敏、复制清空目标、与 WebConsole 两阶段字段校验。"""
    from msgspec import json as msgjson

    from gsuid_core.ai_core.persona import config as persona_cfg_mod
    from gsuid_core.ai_core.persona.bundle import (
        BUNDLE_FORMAT,
        BUNDLE_VERSION,
        import_bundle_dir,
        export_persona_bundle,
    )
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.webconsole.persona_api import update_persona_config
    from gsuid_core.ai_core.meme.pack_model import PackStore
    from gsuid_core.ai_core.persona.resource import copy_persona
    from gsuid_core.ai_core.persona.config_rules import read_channel_scopes

    _setup_isolated_persona_env(tmp_path, monkeypatch)
    _patch_persona_path(tmp_path, monkeypatch)
    _seed_persona(tmp_path, "dm_and_part_group", "specific", ["group_1"], group_scope="specific", private_scope="off")
    _seed_persona(tmp_path, "all_group_p", "global_group")
    _seed_persona(tmp_path, "beta", "specific", [], group_scope="specific", private_scope="all", users=["100000001"])

    async def _empty_binds(_persona_name: str) -> list[str]:
        return []

    async def _noop_mount_default(_persona_name: str) -> None:
        return None

    monkeypatch.setattr(PackStore, "pack_ids_for_persona", staticmethod(_empty_binds))
    monkeypatch.setattr(PackStore, "mount_default_pack", staticmethod(_noop_mount_default))

    bundle_dir = tmp_path / "_incoming_bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "manifest.json").write_text(
        json.dumps({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "persona_name": "dm_and_part_group"}),
        encoding="utf-8",
    )
    (bundle_dir / "persona.md").write_text("# updated bundle\n", encoding="utf-8")
    (bundle_dir / "config.json").write_bytes(
        msgjson.encode(
            {
                "scope": GsStrConfig("启用范围", "", "specific"),
                "target_groups": GsListStrConfig("目标群聊", "", ["ext_group"]),
                "target_users": GsListStrConfig("目标私聊用户", "", ["ext_user"]),
            }
        )
    )

    def _read_disk_cfg(pname: str) -> dict[str, object]:
        raw: object = json.loads((tmp_path / pname / "config.json").read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        return raw

    async def scenario() -> None:
        prev_beta_groups = list(persona_config_manager.get_config("beta").get_config("target_groups").data)
        bad_payloads: tuple[dict[str, object], ...] = (
            {
                "group_scope": "all",
                "takeover_conflicts": True,
                "target_groups": ["no_persist"],
                "inspect_interval": 999,
            },
            {"group_scope": "all", "takeover_conflicts": True, "social_initiative": 101},
            {"curiosity_focus": "invalid_focus"},
        )
        for bad_payload in bad_payloads:
            resp = await update_persona_config("beta", bad_payload, {})
            assert resp["status"] == 1
            assert read_channel_scopes(persona_config_manager.get_config("all_group_p")).group_scope == "all"
            assert persona_config_manager.get_config("beta").get_config("target_groups").data == prev_beta_groups

        before_bundle = persona_config_manager.snapshot_channel_deployment("dm_and_part_group")
        res_bundle = await import_bundle_dir(bundle_dir)
        assert res_bundle.created is False
        assert persona_config_manager.snapshot_channel_deployment("dm_and_part_group") == before_bundle
        updated_disk = _read_disk_cfg("dm_and_part_group")
        assert "group_scope" in updated_disk and "private_scope" in updated_disk

        (bundle_dir / "manifest.json").write_text(
            json.dumps({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "persona_name": "fresh_bundle_p"}),
            encoding="utf-8",
        )
        res_fresh = await import_bundle_dir(bundle_dir)
        assert res_fresh.created is True
        fresh_snap = persona_config_manager.snapshot_channel_deployment("fresh_bundle_p")
        assert fresh_snap.group_scope == "off" and fresh_snap.private_scope == "off"
        assert fresh_snap.target_groups == () and fresh_snap.target_users == ()
        fresh_disk = _read_disk_cfg("fresh_bundle_p")
        assert "group_scope" in fresh_disk and "private_scope" in fresh_disk

        exported_bytes = await export_persona_bundle("beta")
        with zipfile.ZipFile(io.BytesIO(exported_bytes), "r") as zf:
            exported_cfg = json.loads(zf.read("config.json").decode("utf-8"))
        assert exported_cfg["group_scope"]["data"] == "off" and exported_cfg["private_scope"]["data"] == "off"
        assert exported_cfg["scope"]["data"] == "disabled"
        assert exported_cfg["target_groups"]["data"] == [] and exported_cfg["target_users"]["data"] == []

        copied_name = await copy_persona("beta")
        copied_snap = persona_config_manager.snapshot_channel_deployment(copied_name)
        assert copied_snap.group_scope == "off" and copied_snap.private_scope == "off"
        assert copied_snap.target_groups == () and copied_snap.target_users == ()

        null_chan_resp = await update_persona_config(
            "fresh_bundle_p",
            {"group_scope": None, "private_scope": None, "scope": "specific"},
            {},
        )
        assert null_chan_resp["status"] == 0
        assert read_channel_scopes(persona_config_manager.get_config("fresh_bundle_p")).group_scope == "specific"

    asyncio.run(scenario())

    # 冲突抢占若写入目标人格失败，必须回滚已释放的旧全局兜底
    orig_write_scopes = persona_cfg_mod._write_channel_scopes
    beta_cfg = persona_config_manager.get_config("beta")

    def _fail_target_write(cfg: StringConfig, gs: str, ps: str) -> bool:
        if cfg is beta_cfg:
            return False
        return orig_write_scopes(cfg, gs, ps)

    with monkeypatch.context() as mp:
        mp.setattr(persona_cfg_mod, "_write_channel_scopes", _fail_target_write)
        assert persona_config_manager.set_channel_scopes("beta", group_scope="all", takeover_conflicts=True)[0] is False
    assert read_channel_scopes(persona_config_manager.get_config("all_group_p")).group_scope == "all"


def test_corepersona_command_split_sv_and_dm_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.session_registry import get_ai_session_registry
    from gsuid_core.ai_core.proactive.dm_lease import (
        grant_dm_lease,
        renew_dm_lease,
        get_active_dm_lease,
        maybe_switch_dm_lease,
    )
    from gsuid_core.buildin_plugins.core_command.core_ai_control import (
        switch_persona,
        sv_core_ai_control,
        sv_core_persona_control,
    )

    _setup_isolated_persona_env(tmp_path, monkeypatch)
    _seed_persona(tmp_path, "alpha", "global")
    _seed_persona(tmp_path, "beta", "specific", [], group_scope="specific", private_scope="specific")

    assert sv_core_persona_control.pm == 0 and sv_core_ai_control.pm == 0

    sent: list[str] = []

    async def _fake_send(self: Bot, message: object, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        sent.append(str(message))

    monkeypatch.setattr(Bot, "send", _fake_send)

    registry = get_ai_session_registry()
    group_sid = "ws1:onebot:10001:group:group_1"
    other_group_sid = "ws2:onebot:10002:group:group_1"
    dm_sid = "ws1:onebot:10001:private:100000001"
    dummy_model = OpenAIChatModel("gpt-test", provider=OpenAIProvider(api_key="test"))
    for sid in (group_sid, other_group_sid, dm_sid):
        registry.set_ai_session(sid, GsCoreAIAgent(openai_chat_model=dummy_model, persona_name="alpha", session_id=sid))
    grant_dm_lease(user_id="100000001", persona_name="alpha", bot_id="onebot")

    async def scenario() -> None:
        try:
            group_ev = _make_event(group_id="group_1", text="beta")
            await switch_persona(Bot(_Bot("ws1"), group_ev), group_ev)
            assert "beta" in sent[-1]
            assert persona_config_manager.get_persona_for_session(group_ev.session_id) == "beta"
            assert not registry.has_ai_session(group_sid) and not registry.has_ai_session(other_group_sid)
            assert registry.has_ai_session(dm_sid) and get_active_dm_lease("100000001", "onebot") is not None

            dm_ev = _make_event(text="beta")
            await switch_persona(Bot(_Bot("ws1"), dm_ev), dm_ev)
            assert get_active_dm_lease("100000001", "onebot") is None
            assert persona_config_manager.get_persona_for_session(dm_ev.session_id) == "beta"
            assert not registry.has_ai_session(dm_sid)

            _seed_persona(
                tmp_path, "group_only_p", "specific", ["group_1"], group_scope="specific", private_scope="off"
            )
            grp_list_ev = _make_event(group_id="group_1")
            await switch_persona(Bot(_Bot("ws1"), grp_list_ev), grp_list_ev)
            grp_avail = [ln for ln in sent[-1].splitlines() if ln.startswith("可切换人格：")][0]
            assert "group_only_p" in grp_avail and "当前群聊未启用人格" not in sent[-1]

            dm_list_ev = _make_event()
            await switch_persona(Bot(_Bot("ws1"), dm_list_ev), dm_list_ev)
            dm_avail = [ln for ln in sent[-1].splitlines() if ln.startswith("可切换人格：")][0]
            dm_dis = [ln for ln in sent[-1].splitlines() if "当前私聊未启用人格" in ln][0]
            assert "group_only_p" not in dm_avail and "group_only_p" in dm_dis

            assert maybe_switch_dm_lease("100000001", "onebot", "group_only_p 在吗", "beta") is None
            assert maybe_switch_dm_lease("100000002", "onebot", "beta 在吗", "alpha") is None
            assert maybe_switch_dm_lease("100000001", "onebot", "alpha 在吗", "beta") == "alpha"
            assert persona_config_manager.set_channel_scopes("alpha", private_scope="specific")[0] is True
            assert persona_config_manager.set_target_users("alpha", ["100000099"])[0] is True
            assert persona_config_manager.get_persona_for_session(dm_ev.session_id) == "beta"
            assert renew_dm_lease("100000001", "onebot") is False
            assert get_active_dm_lease("100000001", "onebot") is None
            assert persona_config_manager.set_channel_scopes("alpha", private_scope="off")[0] is True
            assert persona_config_manager.get_persona_for_session(dm_ev.session_id) == "beta"
        finally:
            for sid in (group_sid, other_group_sid, dm_sid):
                registry.remove_ai_session(sid)

    asyncio.run(scenario())


def test_appearance_card_v3_and_self_visual_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """外观指纹卡：非自拍提问不下发 MISS 指纹卡、受信任立绘精确命中仍保留，且旧 v2 卡自动升级 v3。"""
    from PIL import Image

    from gsuid_core.ai_core.persona import appearance as app_mod

    pdir = tmp_path / "personas" / "alpha"
    pdir.mkdir(parents=True)
    monkeypatch.setattr(app_mod, "PERSONA_PATH", tmp_path / "personas")
    app_mod._HASH_INDEX.clear()

    portrait_img = Image.new("RGB", (32, 32), color=(40, 120, 90))
    for x in range(16):
        for y in range(32):
            portrait_img.putpixel((x, y), (220, 180, 140))
    portrait_img.save(pdir / "image.png", format="PNG")
    (pdir / "persona.md").write_text("Name: 测试角色\n特征：深蓝风衣配银色徽章", encoding="utf-8")
    (pdir / "appearance.txt").write_text("# appearance-card v2\n摘要：旧版待刷新摘要", encoding="utf-8")

    captured_prompts: list[str] = []

    async def fake_understand(_path: str, prompt: str | None = None, **_kw: object) -> str:
        captured_prompts.append(prompt or "")
        return "摘要：深蓝风衣配银色徽章的角色\n常服：深蓝风衣"

    monkeypatch.setattr("gsuid_core.ai_core.image_understand.understand.understand_image", fake_understand)

    async def _refresh_twice() -> tuple[str, str]:
        return await app_mod.refresh_appearance_card("alpha"), await app_mod.refresh_appearance_card("alpha")

    line, line_again = asyncio.run(_refresh_twice())
    assert line == "深蓝风衣配银色徽章的角色" and line_again == line
    assert len(captured_prompts) == 1 and "深蓝风衣配银色徽章" in captured_prompts[0]

    other_buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(200, 20, 20)).save(other_buf, format="PNG")
    other_bytes = other_buf.getvalue()
    assert app_mod.format_look_identity_note("alpha", other_bytes, allow_card_fallback=False) == ""
    assert app_mod.MISS_SELF_PREFIX in app_mod.format_look_identity_note("alpha", other_bytes, allow_card_fallback=True)

    self_buf = io.BytesIO()
    portrait_img.save(self_buf, format="PNG")
    assert (
        app_mod.format_look_identity_note("alpha", self_buf.getvalue(), allow_card_fallback=False)
        == app_mod.HIT_SELF_NOTE
    )


def test_autonomy_send_context_isolation_and_rate_guards(
    tmp_path: Path,
    journal_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Autonomy 发信上下文组装、手账分片单路隔离、近期防重短话题与 silence/error/配额收口。"""
    from gsuid_core.ai_core.journal import manager as j_mgr, storage as j_store
    from gsuid_core.ai_core.persona import persona as persona_cls_mod
    from gsuid_core.ai_core.autonomy import tools as auto_tools, observer as auto_obs
    from gsuid_core.ai_core.proactive import speak as pro_speak, emitter as pro_emitter
    from gsuid_core.ai_core.autonomy.models import AutonomyStepTrace, CareTargetSnapshot
    from gsuid_core.ai_core.autonomy.select import render_digest
    from gsuid_core.ai_core.database.models import AIAutonomyActivity
    from gsuid_core.ai_core.autonomy.delivery import extract_facts, output_is_failed, output_is_skipped
    from gsuid_core.ai_core.proactive.dispatcher import UnifiedProactiveDispatcher

    del journal_tmp
    _setup_isolated_persona_env(tmp_path / "personas_cfg", monkeypatch)
    _seed_persona(tmp_path / "personas_cfg", "alpha", "specific", ["group_speak_1", "group_speak_2"])
    (tmp_path / "personas_cfg" / "alpha" / "persona.md").write_text(
        "Name: alpha\nStyle (风格): 轻快短句", encoding="utf-8"
    )
    monkeypatch.setattr(persona_cls_mod, "PERSONA_PATH", tmp_path / "personas_cfg")

    # 共享系统提示与动态称呼边界
    speak_sys = pro_speak.build_proactive_speak_system_prompt("alpha", "Name: alpha", include_meme_protocol=True)
    assert (
        "角色扮演" in speak_sys and "Name: alpha" in speak_sys and "框架通用底线" in speak_sys and "<meme:" in speak_sys
    )
    assert "<meme:" not in pro_speak.build_proactive_speak_system_prompt(
        "alpha", "Name: alpha", include_meme_protocol=False
    )

    for target_name, must_have, must_not in (
        ("测试用户A", ("严禁称呼「主人」",), ()),
        ("", ("只有回应主人本人发的消息时才称「主人」",), ("对方不是主人",)),
    ):
        prompt = pro_speak.build_autonomy_speak_user_prompt(
            draft="群里话题草稿",
            is_group=True,
            recent_context="测试用户A: 今天真热闹",
            master_title="主人",
            target_is_master=False,
            target_name=target_name,
            allow_meme=True,
            persona_name="alpha",
        )
        for token in must_have:
            assert token in prompt
        for token in must_not:
            assert token not in prompt

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="autonomy_send.db"):
            now = time.time()
            dm_snap = CareTargetSnapshot(
                user_id="100000001",
                user_name="测试用户A",
                score=90,
                zone_value="close",
                is_master=True,
                bound_uids={},
                recent_lines=["群 group_1 刚刚 [目标] 测试用户A: 群里没聊完的话题"],
                group_id="group_1",
                bot_id="onebot",
                bot_self_id="10001",
                ws_bot_id="ws1",
                session_key="group:group_1",
                last_human_ts=now - 60.0,
            )
            emitted_calls: list[tuple[str, list[str]]] = []
            compose_reply: list[pro_speak.ComposeOutcome] = []
            compose_contexts: list[tuple[bool, str, str]] = []

            async def _fake_emit(
                ev: Event,
                message: str,
                *,
                generator_log_files: list[str] | None = None,
                **_kw: object,
            ) -> bool:
                del ev, _kw
                emitted_calls.append((message, list(generator_log_files or [])))
                return True

            async def _fake_compose(
                *,
                event: Event,
                recent_context: str,
                context_header: str = "",
                **_kw: object,
            ) -> pro_speak.ComposeOutcome:
                del _kw
                compose_contexts.append((bool(event.group_id), recent_context, context_header))
                return compose_reply.pop(0)

            @contextmanager
            def _bound_send(guard_obj: auto_tools.GuardState) -> Iterator[None]:
                disp = UnifiedProactiveDispatcher()
                with monkeypatch.context() as mp:
                    mp.setattr(auto_tools, "get_dispatcher", lambda: disp)
                    mp.setattr(pro_emitter, "emit_proactive_message", _fake_emit)
                    mp.setattr(pro_speak, "compose_autonomy_message", _fake_compose)
                    tok = auto_tools.bind_guard(guard_obj)
                    try:
                        yield
                    finally:
                        auto_tools.reset_guard(tok)

            auto_ctx = make_run_ctx()
            dm_guard = _make_guard(now=now, whitelist=frozenset({"100000001"}), allowed_users={"100000001": dm_snap})
            dm_ev = _make_event()
            compose_reply.extend(
                [
                    pro_speak.ComposeOutcome(status="ok", text="私下问候一句"),
                    pro_speak.ComposeOutcome(status="ok", text="私下接着聊一句"),
                ]
            )
            with _bound_send(dm_guard), isolated_history(dm_ev) as hm:
                await auto_tools.send_message_by_ai(
                    auto_ctx, text="私下跟进一句", target_id="100000001", target_type="user"
                )
                _, dm_ctx_text, dm_ctx_header = compose_contexts[-1]
                assert "群 group_1" in dm_ctx_text and dm_ctx_header == "[对方近期互动记录（含群聊与私聊）]"
                dm_prompt = pro_speak.build_autonomy_speak_user_prompt(
                    draft="私下跟进一句",
                    is_group=False,
                    recent_context=dm_ctx_text,
                    context_header=dm_ctx_header,
                    master_title="主人",
                    target_is_master=True,
                    target_name="测试用户A",
                    persona_name="alpha",
                )
                assert "[对方近期互动记录（含群聊与私聊）]" in dm_prompt and "[最近的私聊上下文]" not in dm_prompt

                hm.add_message(dm_ev, "user", "前两天私聊说过的话", user_name="测试用户A")
                dm_guard.sent_count = 0
                dm_guard.speak_attempts = 0
                await auto_tools.send_message_by_ai(
                    auto_ctx, text="私下跟进一句", target_id="100000001", target_type="user"
                )
                _, dm_both_text, dm_both_header = compose_contexts[-1]
                assert dm_both_header == "" and "[最近的私聊上下文]" in dm_both_text
                assert "[对方近期在群里的发言]" in dm_both_text and "群里没聊完的话题" in dm_both_text
                dm_both_prompt = pro_speak.build_autonomy_speak_user_prompt(
                    draft="私下跟进一句",
                    is_group=False,
                    recent_context=dm_both_text,
                    context_header=dm_both_header,
                    master_title="主人",
                    target_is_master=True,
                    target_name="测试用户A",
                    persona_name="alpha",
                )
                assert dm_both_prompt.count("[最近的私聊上下文]") == 1

            # 近期发信防重摘要走短话题，不回灌长串数值
            await AIAutonomyActivity.insert_activity(
                persona_name="alpha",
                bot_id="onebot",
                bot_self_id="10001",
                drive_kind="group_topic",
                target_user_id="",
                target_session_key="group:group_1",
                plugins_used_json='["web_search_tool", "send_message_by_ai"]',
                steps_json="[]",
                fact_pack_json="{}",
                summary="原始技术指标99.9%与长串百科数值",
                open_trails_json='["测试话题甲"]',
                delivery_mode="immediate_push",
                step_count=2,
                duration_ms=100,
                session_id="autonomy:alpha:test",
                created_at=now - 120.0,
            )
            obs_digest = await auto_obs.build_observation("alpha", now=now)
            assert any("测试话题甲" in item for item in obs_digest.recent_proactive_records)
            assert all("99.9%" not in item for item in obs_digest.recent_proactive_records)

            # silence 按目标锁、error 不计已发、MAX_SPEAK_ATTEMPTS 限流
            emitted_calls.clear()
            compose_reply.extend(
                [
                    pro_speak.ComposeOutcome(status="silence", log_files=["autonomy_output_silent.json"]),
                    pro_speak.ComposeOutcome(status="error", log_files=["autonomy_output_err.json"]),
                    pro_speak.ComposeOutcome(
                        status="ok", text="今天这话题挺好玩呀", log_files=["autonomy_output_ok.json"]
                    ),
                ]
            )
            g_obs = _make_group_obs("group_speak_1", now=now, recent_lines=["测试用户A: 测试项目提问"])
            g_obs2 = _make_group_obs("group_speak_2", now=now, recent_lines=["测试用户B: 另一群话题"])
            guard = _make_guard(now=now, allowed_groups={"group_speak_1": g_obs, "group_speak_2": g_obs2})
            with _bound_send(guard):
                silent_res = await auto_tools.send_message_by_ai(
                    auto_ctx, text="刚才看到群里在聊测试数据 88.8%", target_id="group_speak_1", target_type="group"
                )
                assert (
                    silent_res.startswith("已跳过发送")
                    and not output_is_failed(silent_res)
                    and output_is_skipped(silent_res)
                )
                skipped_facts = extract_facts(
                    [
                        AutonomyStepTrace(
                            step_index=0,
                            tool_name="send_message_by_ai",
                            args_summary="{}",
                            output_text=silent_res,
                            is_error=False,
                        )
                    ]
                )
                assert not skipped_facts.has_substantive_outcome and guard.sent_count == 0 and not emitted_calls

                same_target_res = await auto_tools.send_message_by_ai(
                    auto_ctx, text="换个说法再试试", target_id="group_speak_1", target_type="group"
                )
                assert same_target_res.startswith("发送被拦截") and output_is_failed(same_target_res)
                assert len(compose_reply) == 2

                err_res = await auto_tools.send_message_by_ai(
                    auto_ctx, text="刚才看到群里在聊测试数据 88.8%", target_id="group_speak_2", target_type="group"
                )
                assert err_res.startswith("发送失败") and output_is_failed(err_res) and guard.sent_count == 0

                capped_res = await auto_tools.send_message_by_ai(
                    auto_ctx, text="刚才看到群里在聊测试数据 88.8%", target_id="group_speak_2", target_type="group"
                )
                assert capped_res.startswith("发送被拦截") and output_is_failed(capped_res) and len(compose_reply) == 1

                guard.speak_attempts = 0
                sent_res = await auto_tools.send_message_by_ai(
                    auto_ctx, text="刚才看到群里在聊测试数据 88.8%", target_id="group_speak_2", target_type="group"
                )
                assert sent_res.startswith("已成功向") and guard.sent_count == 1
                assert emitted_calls == [("今天这话题挺好玩呀", ["autonomy_output_ok.json"])]

            # 规划底片不承载分片手账原文；发声轮按目标单路隔离注入
            j_store._save_shard_sync(
                "alpha",
                j_store.ShardDocument(
                    shard_id="user:100000001",
                    persistent=["- 私聊喜欢直截了当的结论"],
                    monthly=["- 本月持续跟进周末复盘计划"],
                    yesterday=["- 昨日私聊约定周末整理书单（归档: journal:alpha:user:100000001:2026-11-24）。"],
                ),
            )
            j_store._save_shard_sync(
                "alpha", j_store.ShardDocument(shard_id="user:100000002", yesterday=["- 测试用户B的私密健康记录"])
            )
            j_store._save_shard_sync(
                "alpha",
                j_store.ShardDocument(
                    shard_id="group_speak_1",
                    persistent=["- 本群常聊架构设计与工具链"],
                    yesterday=["- 昨日群里完成模块拆分讨论"],
                ),
            )
            dm_brief = j_mgr.read_shard_brief("alpha", user_id="100000001", max_chars=80, include_persistent=False)
            assert "周末整理书单" in dm_brief and "journal:" not in dm_brief
            assert "模块拆分讨论" in j_mgr.read_shard_brief(
                "alpha", group_id="group_speak_1", max_chars=80, include_persistent=False
            )
            rendered_digest = render_digest(_make_digest(care_targets=[dm_snap], group_digests=[g_obs]), now=now)
            assert (
                "周末整理书单" not in rendered_digest
                and "模块拆分讨论" not in rendered_digest
                and "手账线索" not in rendered_digest
            )

            ab_guard = _make_guard(
                now=now,
                whitelist=frozenset({"100000001"}),
                allowed_groups={"group_speak_1": g_obs},
                allowed_users={"100000001": dm_snap},
            )
            compose_contexts.clear()
            compose_reply.extend(
                [
                    pro_speak.ComposeOutcome(status="ok", text="群里模块拆分推进得挺顺"),
                    pro_speak.ComposeOutcome(status="ok", text="周末书单准备好了没"),
                ]
            )
            with _bound_send(ab_guard):
                await auto_tools.send_message_by_ai(
                    auto_ctx, text="跟进模块拆分", target_id="group_speak_1", target_type="group"
                )
                ab_guard.sent_count = 0
                ab_guard.speak_attempts = 0
                await auto_tools.send_message_by_ai(
                    auto_ctx, text="问问周末书单", target_id="100000001", target_type="user"
                )
            grp_is_g, grp_speak_ctx, _ = compose_contexts[0]
            dm_is_g, dm_speak_ctx, _ = compose_contexts[1]
            assert grp_is_g is True and "[本群长期默契与近期手账]" in grp_speak_ctx and "模块拆分讨论" in grp_speak_ctx
            assert "周末整理书单" not in grp_speak_ctx and "私密健康记录" not in grp_speak_ctx
            assert (
                dm_is_g is False
                and "[你与对方的私聊默契与近期手账]" in dm_speak_ctx
                and "私聊喜欢直截了当" in dm_speak_ctx
            )
            assert "私密健康记录" not in dm_speak_ctx and "模块拆分讨论" not in dm_speak_ctx

    asyncio.run(scenario())


def test_proactive_speak_agent_and_emitter_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直跑真实 compose_autonomy_message / run_proactive_speak_agent（离线模型与真实超时）与出口护栏。"""
    from gsuid_core.ai_core import session_logger as sl_mod
    from gsuid_core.ai_core.persona import persona as persona_cls_mod
    from gsuid_core.ai_core.proactive import speak as pro_speak, emitter as pro_emitter
    from gsuid_core.ai_core.agent_errors import NO_RESULT_TEXT
    from gsuid_core.ai_core.proactive.dispatcher import UnifiedProactiveDispatcher

    _setup_isolated_persona_env(tmp_path / "personas_cfg", monkeypatch)
    _seed_persona(tmp_path / "personas_cfg", "alpha", "specific", ["group_1"])
    (tmp_path / "personas_cfg" / "alpha" / "persona.md").write_text(
        "Name: alpha\nStyle (风格): 轻快短句", encoding="utf-8"
    )
    monkeypatch.setattr(persona_cls_mod, "PERSONA_PATH", tmp_path / "personas_cfg")
    monkeypatch.setattr(sl_mod, "AI_SESSION_LOGS_PATH", tmp_path / "session_logs")
    monkeypatch.setattr(sl_mod, "AI_SUBAGENT_LOGS_PATH", tmp_path / "session_logs" / "subagents")

    assert pro_speak.strip_message_quotes("“外层中文引号”") == "外层中文引号"
    assert pro_speak.strip_message_quotes("“甲”和“乙”") == "“甲”和“乙”"

    disp_meme = UnifiedProactiveDispatcher()
    disp_meme.register_send("group_1", "heartbeat", "早安呀\n<meme:happy>", persona_name="alpha")
    assert not disp_meme.would_repeat_heartbeat("group_1", "去吃饭吗\n<meme:happy>", persona_name="alpha")

    def _bind_model(model: Model) -> None:
        monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _l, _k=None: model)
        monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_by_full_name", lambda _n: model)

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="proactive_speak_agent.db"):
            grp_ev = _make_event(group_id="group_1", user_id="")
            seen_logs: list[str] = []

            for raw_reply, exp_status, exp_text in (
                ('"这句带引号也该干净发出去"', "ok", "这句带引号也该干净发出去"),
                ("<SILENCE>", "silence", ""),
                (NO_RESULT_TEXT, "error", ""),
                ("执行出错: Request timed out.", "error", ""),
            ):
                _bind_model(TestModel(call_tools=[], custom_output_text=raw_reply))
                res = await pro_speak.compose_autonomy_message(
                    persona_name="alpha",
                    event=grp_ev,
                    draft="草稿",
                    recent_context="测试用户A: 聊聊",
                    target_is_master=False,
                )
                assert res.status == exp_status and res.text == exp_text and len(res.log_files) == 1
                seen_logs.extend(res.log_files)

            async def _slow_stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
                await asyncio.sleep(0.2)
                yield "too late"

            _bind_model(FunctionModel(stream_function=_slow_stream))
            res_to = await pro_speak.run_proactive_speak_agent(
                persona_name="alpha",
                event=grp_ev,
                system_prompt="sys",
                user_prompt="usr",
                create_by="Heartbeat_Output",
                session_id="hb_timeout_test",
                timeout_seconds=0.02,
            )
            assert res_to.status == "error" and len(res_to.log_files) == 1
            seen_logs.extend(res_to.log_files)

            _bind_model(TestModel(call_tools=[], custom_output_text="<SILENCE>"))
            res_sil = await pro_speak.run_proactive_speak_agent(
                persona_name="alpha",
                event=grp_ev,
                system_prompt="sys",
                user_prompt="usr",
                create_by="Heartbeat_Output",
                session_id="hb_silence_test",
            )
            assert res_sil.status == "silence" and len(res_sil.log_files) == 1
            seen_logs.extend(res_sil.log_files)
            assert len(set(seen_logs)) == 6

            # emit_proactive_message 出口护栏：拦截 <SILENCE>/空白，放行正常消息
            sent_proactive: list[str] = []

            async def _record_send_chat(_bot: object, msg: str, **_kw: object) -> None:
                sent_proactive.append(msg)

            async def _noop_sync_main(**_kw: object) -> None:
                return None

            emit_disp = UnifiedProactiveDispatcher()
            grp_bot = Bot(_Bot("ws1"), grp_ev)
            with monkeypatch.context() as mp:
                mp.setattr(pro_emitter, "get_dispatcher", lambda: emit_disp)
                mp.setattr(pro_emitter, "send_chat_result", _record_send_chat)
                mp.setattr(pro_emitter, "_sync_to_main_session", _noop_sync_main)
                emit_cases: tuple[tuple[str, pro_emitter.ProactiveSource, bool], ...] = (
                    ("<SILENCE>", "heartbeat", False),
                    ("   ", "autonomy", False),
                    ("正常主动发声", "autonomy", True),
                )
                for msg_text, src_kind, exp_sent in emit_cases:
                    ok_emit = await pro_emitter.emit_proactive_message(
                        grp_ev,
                        msg_text,
                        source=src_kind,
                        trigger_reason="测试出口护栏",
                        bot=grp_bot,
                        persona_name="alpha",
                        suppress_when_heartbeat_recent=False,
                    )
                    assert ok_emit is exp_sent
                assert sent_proactive == ["正常主动发声"]

    asyncio.run(scenario())


def test_proactive_policy_unified_gates_and_persona_rate_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """统一主动门控：禁言拦截搭话但放行定时/看板，且 autonomy 发信按人格维度执行频次上限。"""
    from gsuid_core.ai_core.autonomy import tools as auto_tools
    from gsuid_core.ai_core.proactive.policy import (
        ProactiveGateInput,
        evaluate_proactive_gate,
        check_target_rate_and_gap,
    )
    from gsuid_core.ai_core.proactive.dispatcher import UnifiedProactiveDispatcher

    gate_cases: tuple[tuple[ProactiveGateInput, bool, str], ...] = (
        (ProactiveGateInput("heartbeat", "alpha", "group_1", False, scope_banned=True), False, "scope_banned"),
        (ProactiveGateInput("autonomy_send", "alpha", "group_1", False, scope_banned=True), False, "scope_banned"),
        (ProactiveGateInput("scheduled_task", "alpha", "group_1", False, scope_banned=True), True, ""),
        (ProactiveGateInput("kanban", "alpha", "group_1", False, scope_banned=True), True, ""),
        (
            ProactiveGateInput("heartbeat", "alpha", "100000001", True, dm_reciprocity_blocked=True),
            False,
            "dm_reciprocity_backoff",
        ),
        (ProactiveGateInput("heartbeat", "alpha", "group_1", False, dm_reciprocity_blocked=True), True, ""),
    )
    for gate_in, exp_allowed, exp_code in gate_cases:
        res = evaluate_proactive_gate(gate_in)
        assert res.allowed is exp_allowed
        if exp_code:
            assert res.code == exp_code

    fake_now = [1_700_000_000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])
    disp = UnifiedProactiveDispatcher()
    disp.register_send("group_1", "autonomy", "测试问候一", persona_name="alpha")
    disp.register_send("group_1", "autonomy", "测试问候二", persona_name="alpha")
    fake_now[0] += 600.0
    _, rate_a = check_target_rate_and_gap(disp, "group_1", persona_name="alpha", max_per_hour=2, max_per_day=8)
    _, rate_b = check_target_rate_and_gap(disp, "group_1", persona_name="beta", max_per_hour=2, max_per_day=8)
    assert rate_a is True and rate_b is False

    now = fake_now[0]
    guard = _make_guard(now=now, allowed_groups={"group_1": _make_group_obs("group_1", now=now)}, hour_cap=2, day_cap=8)
    tok = auto_tools.bind_guard(guard)
    try:
        with monkeypatch.context() as mp:
            mp.setattr(auto_tools, "get_dispatcher", lambda: disp)
            msg = asyncio.run(auto_tools.send_message_by_ai(make_run_ctx(), "顺口打个招呼", target_id="group_1"))
            assert "配额已达上限" in msg
    finally:
        auto_tools.reset_guard(tok)


def test_persona_reset_guard_and_archive_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.persona_reset import guard, archive

    guard.freeze(["alpha"], shared=False, scope_keys=["self:10001"])
    try:
        assert guard.is_persona_frozen("alpha") is True and guard.is_persona_frozen("beta") is False
        assert guard.is_scope_frozen("self:10001:alpha") is True and guard.is_scope_frozen("self:10001:beta") is False
        assert guard.is_scope_frozen("self:10001") is True and guard.is_scope_frozen("group:group_1") is False

        guard.freeze(["alpha"], shared=True)
        assert guard.is_scope_frozen("group:group_1") is True and guard.is_scope_frozen("user_global:100000001") is True
        guard.unfreeze(["alpha"], shared=True)
        assert guard.is_persona_frozen("alpha") is True and guard.is_scope_frozen("group:group_1") is False
    finally:
        guard.unfreeze(["alpha"], shared=False, scope_keys=["self:10001"])
    assert not guard.is_persona_frozen("alpha") and not guard.is_scope_frozen("self:10001")

    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../escape.txt", "boom")
    with pytest.raises(ValueError, match="unsafe zip entry"):
        archive.extract_sync(evil_zip)

    bad_zip = tmp_path / "corrupt.zip"
    bad_zip.write_bytes(b"not-a-zip")
    assert archive.read_manifest_sync(bad_zip) is None

    monkeypatch.setattr(archive, "BACKUP_ROOT", tmp_path / "backups")
    out_dir = archive.persona_backup_dir("alpha")
    out_dir.mkdir(parents=True)
    for idx in range(6):
        (out_dir / f"20200101_00000{idx}.zip").write_bytes(b"x")
    newest = asyncio.run(archive.BackupWriter("alpha").finalize())
    kept = sorted(p.name for p in out_dir.glob("*.zip"))
    assert len(kept) == archive.KEEP_BACKUPS and kept[-1] == newest.name and kept[0] == "20200101_000002.zip"


def test_persona_reset_backup_restore_and_vector_resume_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端覆盖：预览 → 重置（含备份）→ 跨人格隔离 → 还原 → 向量断点续跑 → HTTP 与故障恢复。"""
    import httpx
    from sqlmodel import select
    from sqlalchemy import delete as sql_delete

    from gsuid_core.webconsole import persona_api as _persona_api  # noqa: F401
    from gsuid_core.ai_core.rag import base as rag_base
    from gsuid_core.ai_core.memory import startup as mem_startup
    from gsuid_core.ai_core.journal import storage as jstorage
    from gsuid_core.ai_core.persona import mood as mood_mod
    from gsuid_core.webconsole.app_app import app as web_app
    from gsuid_core.webconsole.web_api import require_admin
    from gsuid_core.ai_core.state_store import state_get_value, state_set_value
    from gsuid_core.ai_core.memory.vector import ops as vec_ops
    from gsuid_core.ai_core.persona_reset import jobs, guard, archive, service
    from gsuid_core.ai_core.scheduled_task import executor as sched_exec
    from gsuid_core.ai_core.cognition.nodes import AICogEdge, AICogNode, CogEdgeKind, AICogAttachment
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.database.models import UserFavorability, AIAutonomyActivity
    from gsuid_core.ai_core.memory.database import fts as fts_mod, models as mem_models
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.ai_core.persona.homeostasis import persona_scope
    from gsuid_core.ai_core.persona_reset.types import ALL_ITEMS
    from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask
    from gsuid_core.ai_core.memory.database.models import (
        AIMemEdge,
        AIMemEntity,
        AIMemThread,
        AIMemEpisode,
        mem_episode_entity_mentions,
    )
    from gsuid_core.ai_core.memory.database.preference import AIMemPreference

    _setup_isolated_persona_env(tmp_path / "personas", monkeypatch)
    _seed_persona(tmp_path / "personas", "alpha", "specific", ["group_1"], group_scope="specific", private_scope="all")
    _seed_persona(tmp_path / "personas", "beta", "specific", ["group_2"], group_scope="specific", private_scope="off")

    _, notes_root = setup_journal_env(tmp_path, monkeypatch, patch_autonomy_notes=True)
    backup_root = tmp_path / "persona_backups"
    monkeypatch.setattr(archive, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(archive, "JOBS_DIR", backup_root / "_jobs")
    jobs._jobs.clear()
    jobs._active_job_id = ""
    mood_mod._mood_states.clear()

    class _FakeQdrant:
        def __init__(self) -> None:
            self.deleted_collections: list[str] = []

        async def collection_exists(self, collection_name: str) -> bool:
            del collection_name
            return True

        async def delete(self, collection_name: str, points_selector: object) -> None:
            del points_selector
            self.deleted_collections.append(collection_name)

    class _FakeEmbedder:
        dimension: int = 4

    fake_qdrant = _FakeQdrant()
    monkeypatch.setattr(rag_base, "client", fake_qdrant)
    monkeypatch.setattr(rag_base, "embedding_provider", _FakeEmbedder())
    monkeypatch.setattr(fts_mod, "_FTS_READY", None)

    upserted: dict[str, list[str]] = {"episode": [], "entity": [], "edge": []}
    fail_once = {"entity": True}

    async def _fake_upsert_ep(rows: list[dict[str, object]]) -> None:
        upserted["episode"].extend(str(r["episode_id"]) for r in rows)

    async def _fake_upsert_ent(rows: list[dict[str, object]]) -> None:
        if fail_once["entity"]:
            fail_once["entity"] = False
            raise RuntimeError("simulated embed outage")
        upserted["entity"].extend(str(r["entity_id"]) for r in rows)

    async def _fake_upsert_edge(rows: list[dict[str, object]]) -> None:
        upserted["edge"].extend(str(r["edge_id"]) for r in rows)

    embed_calls = {"n": 0}

    async def _fake_embed_dense(texts: list[str]) -> list[list[float]]:
        embed_calls["n"] += 1
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    service._tps_cache.clear()
    monkeypatch.setattr(vec_ops, "upsert_episode_vectors_batch", _fake_upsert_ep)
    monkeypatch.setattr(vec_ops, "upsert_entity_vectors_batch", _fake_upsert_ent)
    monkeypatch.setattr(vec_ops, "upsert_edge_vectors_batch", _fake_upsert_edge)
    monkeypatch.setattr(vec_ops, "embed_texts_dense", _fake_embed_dense)

    registered_jobs: list[str] = []
    monkeypatch.setattr(
        sched_exec,
        "register_restored_task",
        lambda task, overdue_idx: registered_jobs.append(task.task_id),
    )

    async def _wait_job(job_id: str) -> service.JobView:
        for _ in range(200):
            view = await service.job_view(job_id)
            if view is not None and view["status"] != "running":
                return view
            await asyncio.sleep(0.02)
        raise AssertionError(f"job {job_id} timed out")

    async def scenario(maker: async_sessionmaker[AsyncSession]) -> None:
        now_dt = datetime.now(timezone.utc)
        async with maker() as s:
            await fts_mod.ensure_fts_table(s)
            ent_a = AIMemEntity(id="ent-a1", scope_key="self:10001:alpha", name="角色自述", qdrant_id="ent-a1")
            ent_b = AIMemEntity(id="ent-b1", scope_key="self:10001:beta", name="他人格实体", qdrant_id="ent-b1")
            ent_g1 = AIMemEntity(id="ent-g1", scope_key="group:group_1", name="测试用户A", qdrant_id="ent-g1")
            ent_g2 = AIMemEntity(id="ent-g2", scope_key="group:group_1", name="测试项目", qdrant_id="ent-g2")
            ent_u1 = AIMemEntity(id="ent-u1", scope_key="user_global:100000001", name="测试用户A", qdrant_id="ent-u1")
            th_g = AIMemThread(id="th-g1", scope_key="group:group_1", title="测试项目讨论")
            ep_a = AIMemEpisode(
                id="ep-a1",
                scope_key="self:10001:alpha",
                content="alpha 自我回忆",
                speaker_ids=["alpha"],
                qdrant_id="ep-a1",
            )
            ep_b = AIMemEpisode(
                id="ep-b1",
                scope_key="self:10001:beta",
                content="beta 自我回忆",
                speaker_ids=["beta"],
                qdrant_id="ep-b1",
            )
            ep_g = AIMemEpisode(
                id="ep-g1",
                scope_key="group:group_1",
                content="群里讨论测试项目进展",
                speaker_ids=["100000001"],
                qdrant_id="ep-g1",
                thread_id="th-g1",
            )
            edge_g = AIMemEdge(
                id="edge-g1",
                scope_key="group:group_1",
                fact="测试用户A负责测试项目",
                source_entity_id="ent-g1",
                target_entity_id="ent-g2",
                source_episode_id="ep-g1",
                thread_id="th-g1",
                qdrant_id="edge-g1",
            )
            pref_u = AIMemPreference(
                id="pref-u1", scope_key="user_global:100000001", user_id="100000001", preference_rule="回答简洁"
            )
            pref_a = AIMemPreference(
                id="pref-a1", scope_key="self:10001:alpha", user_id="100000001", preference_rule="alpha自偏好"
            )
            pref_b = AIMemPreference(
                id="pref-b1", scope_key="self:10001:beta", user_id="100000001", preference_rule="beta自偏好"
            )
            s.add_all([ent_a, ent_b, ent_g1, ent_g2, ent_u1, th_g, ep_a, ep_b, ep_g, edge_g, pref_u, pref_a, pref_b])
            await s.flush()
            await s.execute(mem_episode_entity_mentions.insert().values(episode_id="ep-g1", entity_id="ent-g1"))
            await fts_mod.index_episodes(s, [ep_a, ep_b, ep_g])

            hub = AICogNode(kind=CogKind.ENTITY.value, ref="world:test_hub", scope_key="", title="公共枢纽")
            env_node = AICogNode(
                kind=CogKind.ENTITY.value,
                ref="env:ent-g2",
                scope_key="group:group_1",
                title="测试项目",
                canon="world:test_hub",
            )
            self_note = AICogNode(
                kind=CogKind.SELF_NOTE.value, ref="self:10001:note:1", scope_key="self:10001:alpha", title="自我反思"
            )
            s.add_all([hub, env_node, self_note])
            await s.flush()
            assert isinstance(hub.id, int) and isinstance(env_node.id, int) and isinstance(self_note.id, int)
            s.add(AICogEdge(src_id=env_node.id, dst_id=hub.id, edge_kind=CogEdgeKind.RELATED.value))
            s.add(AICogAttachment(node_id=self_note.id, slot="备注", title="附注", ref="att:1", handle="h:1"))

            s.add(UserFavorability(user_id="100000001", bot_id="onebot", persona_name="alpha", favorability=42))
            s.add(UserFavorability(user_id="100000001", bot_id="onebot", persona_name="beta", favorability=88))
            s.add(AIAutonomyActivity(persona_name="alpha", summary="alpha 自主记录", created_at=time.time()))
            s.add(AIAutonomyActivity(persona_name="beta", summary="beta 自主记录", created_at=time.time()))
            s.add(
                AIScheduledTask(
                    bot_id="onebot",
                    task_id="task-alpha-1",
                    user_id="100000001",
                    persona_name="alpha",
                    task_prompt="测试提醒",
                    status="pending",
                    trigger_time=now_dt + timedelta(hours=1),
                )
            )
            await s.commit()

        j_alpha = jstorage.persona_journal_dir("alpha") / "self.md"
        j_alpha.parent.mkdir(parents=True, exist_ok=True)
        j_alpha.write_text("# alpha 手账\n", encoding="utf-8")
        j_beta = jstorage.persona_journal_dir("beta") / "self.md"
        j_beta.parent.mkdir(parents=True, exist_ok=True)
        j_beta.write_text("# beta 手账\n", encoding="utf-8")
        (notes_root / "alpha").mkdir(parents=True, exist_ok=True)
        (notes_root / "alpha" / "n1.txt").write_text("alpha 笔记", encoding="utf-8")
        jstorage.put_journal_state_sync(
            "alpha",
            {
                "last_compact_ts": 123.0,
                "last_dream_date": "2026-11-24",
                "dirty_shards": [],
                "light_carry": {},
                "last_gc_month": "",
                "schema_version": 2,
            },
        )
        await state_set_value(persona_scope("alpha"), "homeostasis", {"energy": 0.5})
        await state_set_value(persona_scope("beta"), "homeostasis", {"energy": 0.9})
        await state_set_value("__gscore_group_profile__", "group:group_1", {"tags": ["测试"]})
        await state_set_value(
            "self:10001:alpha",
            "self_model",
            {"preferences_learned": ["用户(100000001)：叫我测试称呼"], "commitments": ["记住测试约定"]},
        )
        await state_set_value(
            "self:10001:beta",
            "self_model",
            {"preferences_learned": ["用户(100000001)：beta专属称呼"]},
        )
        await state_set_value("group:group_1", "self_model", {"commitments": ["群里一起测试"]})
        await mood_mod.update_mood("alpha", "group_1", "praise")

        ev_g1 = _make_event(group_id="group_1")
        with isolated_history(ev_g1) as hm:
            hm.add_message(ev_g1, "user", "测试消息")

            prev = await service.preview_reset("alpha")
            assert prev["other_personas"] == ["beta"]
            by_key = {item["key"]: item for item in prev["items"]}
            assert by_key["self_memory"]["default_checked"] is True and by_key["self_memory"]["count"] == 5
            assert by_key["global_group"]["default_checked"] is False and by_key["global_group"]["count"] == 7
            assert by_key["global_user"]["default_checked"] is False and by_key["global_user"]["count"] == 1
            assert by_key["user_preferences"]["default_checked"] is False and by_key["user_preferences"]["count"] == 1
            assert by_key["scheduled_tasks"]["default_checked"] is False and by_key["scheduled_tasks"]["count"] == 1
            assert by_key["session_history"]["backup_supported"] is False and by_key["session_history"]["count"] >= 1

            assert (await service.start_reset("alpha", ["journal"], "wrong", False, False))["error"]
            assert (await service.start_reset("alpha", ["global_group"], "alpha", False, False))["error"]
            assert (await service.start_reset("alpha", ["user_preferences"], "alpha", False, False))["error"]

            # 单独清空与还原用户偏好：不动全局用户图谱、人格自偏好和 self_model，也不触发向量重建
            pref_only = await service.start_reset("alpha", ["user_preferences"], "alpha", True, True)
            assert not pref_only["error"] and pref_only["job_id"]
            pref_done = await _wait_job(pref_only["job_id"])
            assert pref_done["status"] == "done" and pref_done["backup_file"].endswith(".zip")
            async with maker() as s:
                assert sorted(p.id for p in (await s.execute(select(AIMemPreference))).scalars().all()) == [
                    "pref-a1",
                    "pref-b1",
                ]
                assert "ent-u1" in [e.id for e in (await s.execute(select(AIMemEntity))).scalars().all()]
            assert await state_get_value("self:10001:alpha", "self_model") is not None
            pref_rprev = await service.preview_restore("alpha", pref_done["backup_file"])
            assert not isinstance(pref_rprev, str) and pref_rprev["vector_texts"] == 0
            pref_rstart = await service.start_restore("alpha", pref_done["backup_file"], ["user_preferences"], "alpha")
            assert not pref_rstart["error"] and pref_rstart["job_id"]
            pref_rdone = await _wait_job(pref_rstart["job_id"])
            assert pref_rdone["status"] == "done" and all(st["key"] != "vectors" for st in pref_rdone["steps"])
            async with maker() as s:
                assert sorted(p.id for p in (await s.execute(select(AIMemPreference))).scalars().all()) == [
                    "pref-a1",
                    "pref-b1",
                    "pref-u1",
                ]
            assert await service.delete_backup("alpha", pref_done["backup_file"]) is True

            # 冻结期禁止写入 self_model
            from gsuid_core.ai_core.self_cognition import add_self_note

            guard.freeze(["alpha"], shared=False)
            try:
                assert await add_self_note("10001", "alpha", "冻结期不该写入", "preferences_learned") is False
            finally:
                guard.unfreeze(["alpha"], shared=False)

            # 摄入 flush 超时 → 任务失败、手账未删、人格解冻
            class _StuckWorker:
                async def wait_frozen_flush_idle(self, timeout: float) -> bool:
                    del timeout
                    return False

                def drop_frozen_buffers(self) -> int:
                    return 0

            with monkeypatch.context() as mp:
                mp.setattr(mem_startup, "get_ingestion_worker", lambda: _StuckWorker())
                stuck = await service.start_reset("alpha", ["journal"], "alpha", False, False)
                assert not stuck["error"] and stuck["job_id"]
                assert (await _wait_job(stuck["job_id"]))["status"] == "failed"
                assert j_alpha.exists() and guard.is_persona_frozen("alpha") is False

            # 冻结人格的一次性定时任务顺延补跑不直接点火
            overdue_calls: list[tuple[str, str, int]] = []
            monkeypatch.setattr(
                sched_exec,
                "_schedule_overdue_job",
                lambda task_id, kind, overdue_idx: overdue_calls.append((task_id, kind, overdue_idx)),
            )
            monkeypatch.setattr(ai_config.get_config("enable"), "data", True)
            guard.freeze(["alpha"], shared=False)
            try:
                await sched_exec.execute_scheduled_task("task-alpha-1")
            finally:
                guard.unfreeze(["alpha"], shared=False)
            assert overdue_calls == [("task-alpha-1", "一次性任务", 0)]

            # 全量重置（含备份）+ 跨人格备份拒绝
            started = await service.start_reset("alpha", list(ALL_ITEMS), "alpha", True, True)
            assert not started["error"] and started["job_id"]
            done_view = await _wait_job(started["job_id"])
            assert done_view["status"] == "done" and done_view["backup_file"].endswith(".zip")
            backup_file = done_view["backup_file"]

            beta_backup_dir = archive.persona_backup_dir("beta")
            beta_backup_dir.mkdir(parents=True, exist_ok=True)
            copied = beta_backup_dir / backup_file
            shutil.copy2(archive.persona_backup_dir("alpha") / backup_file, copied)
            try:
                assert await service.preview_restore("beta", backup_file) == "备份不属于该人格"
            finally:
                copied.unlink(missing_ok=True)

            assert not j_alpha.exists() and j_beta.exists() and not (notes_root / "alpha").exists()
            assert jstorage.read_journal_state_sync("alpha") is None
            assert await state_get_value(persona_scope("alpha"), "homeostasis") is None
            assert await state_get_value(persona_scope("beta"), "homeostasis") == {"energy": 0.9}
            assert await state_get_value("__gscore_group_profile__", "group:group_1") is None
            assert await state_get_value("self:10001:alpha", "self_model") is None
            assert await state_get_value("group:group_1", "self_model") is None
            assert await state_get_value("self:10001:beta", "self_model") == {
                "preferences_learned": ["用户(100000001)：beta专属称呼"]
            }
            assert mood_mod.count_mood_states("alpha") == 0 and fake_qdrant.deleted_collections

            async with maker() as s:
                assert [e.id for e in (await s.execute(select(AIMemEpisode))).scalars().all()] == ["ep-b1"]
                assert [p.id for p in (await s.execute(select(AIMemPreference))).scalars().all()] == ["pref-b1"]
                assert [
                    (f.persona_name, f.favorability)
                    for f in (await s.execute(select(UserFavorability))).scalars().all()
                ] == [("beta", 88)]
                assert [a.persona_name for a in (await s.execute(select(AIAutonomyActivity))).scalars().all()] == [
                    "beta"
                ]
                assert [c.ref for c in (await s.execute(select(AICogNode))).scalars().all()] == ["world:test_hub"]
                task_row = (await s.execute(select(AIScheduledTask))).scalars().first()
                assert task_row is not None and task_row.status == "cancelled"

            # 还原预览（吞吐缓存去重嵌入）+ 覆盖还原 + 向量中断断点续跑
            rprev = await service.preview_restore("alpha", backup_file)
            assert not isinstance(rprev, str)
            assert rprev["overwrite_counts"]["scheduled_tasks"] == 0 and rprev["vector_texts"] == 11
            assert not isinstance(await service.preview_restore("alpha", backup_file), str) and embed_calls["n"] == 1

            await UserFavorability.restore_rows(
                [UserFavorability(user_id="100000002", bot_id="onebot", persona_name="alpha", favorability=5)]
            )
            rstart = await service.start_restore("alpha", backup_file, list(rprev["items"]), "alpha")
            assert not rstart["error"] and rstart["job_id"]
            interrupted = await _wait_job(rstart["job_id"])
            assert interrupted["status"] == "resumable"
            assert [st for st in interrupted["steps"] if st["key"] == "vectors"][0]["status"] == "failed"

            assert j_alpha.read_text(encoding="utf-8") == "# alpha 手账\n"
            assert (notes_root / "alpha" / "n1.txt").read_text(encoding="utf-8") == "alpha 笔记"
            assert jstorage.read_journal_state_sync("alpha") is not None
            assert await state_get_value(persona_scope("alpha"), "homeostasis") == {"energy": 0.5}
            assert await state_get_value("__gscore_group_profile__", "group:group_1") == {"tags": ["测试"]}
            assert await state_get_value("self:10001:alpha", "self_model") == {
                "preferences_learned": ["用户(100000001)：叫我测试称呼"],
                "commitments": ["记住测试约定"],
            }
            assert await state_get_value("group:group_1", "self_model") == {"commitments": ["群里一起测试"]}
            assert "task-alpha-1" in registered_jobs

            async with maker() as s:
                assert sorted(e.id for e in (await s.execute(select(AIMemEpisode))).scalars().all()) == [
                    "ep-a1",
                    "ep-b1",
                    "ep-g1",
                ]
                assert sorted(p.id for p in (await s.execute(select(AIMemPreference))).scalars().all()) == [
                    "pref-a1",
                    "pref-b1",
                    "pref-u1",
                ]
                assert sorted(
                    (f.persona_name, f.user_id, f.favorability)
                    for f in (await s.execute(select(UserFavorability))).scalars().all()
                ) == [("alpha", "100000001", 42), ("beta", "100000001", 88)]
                assert sorted(c.ref for c in (await s.execute(select(AICogNode))).scalars().all()) == [
                    "env:ent-g2",
                    "self:10001:note:1",
                    "world:test_hub",
                ]
                assert len((await s.execute(select(AICogEdge))).scalars().all()) == 1
                assert len((await s.execute(select(AICogAttachment))).scalars().all()) == 1
                task_row = (await s.execute(select(AIScheduledTask))).scalars().first()
                assert task_row is not None and task_row.status == "pending"

            jobs._jobs.clear()
            jobs._active_job_id = ""
            active = await service.active_job_view("alpha")
            assert active is not None and active["status"] == "resumable" and active["job_id"] == rstart["job_id"]

            resumed = await service.resume_job(rstart["job_id"])
            assert not resumed["error"]
            final_view = await _wait_job(resumed["job_id"])
            assert final_view["status"] == "done" and await service.active_job_view("alpha") is None
            assert sorted(upserted["episode"]) == ["ep-a1", "ep-g1"]
            assert sorted(upserted["entity"]) == ["ent-a1", "ent-g1", "ent-g2", "ent-u1"]
            assert sorted(upserted["edge"]) == ["edge-g1"]

            # FastAPI HTTP 契约端到端验证
            web_app.dependency_overrides[require_admin] = lambda: make_session_record("admin")
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=web_app), base_url="http://testserver"
                ) as http:
                    resp_prev = await http.get("/api/persona/alpha/reset/preview")
                    assert resp_prev.status_code == 200 and resp_prev.json()["status"] == 0
                    assert resp_prev.json()["data"]["persona"] == "alpha"

                    resp_job = await http.get(f"/api/persona-reset/jobs/{final_view['job_id']}")
                    assert resp_job.status_code == 200 and resp_job.json()["data"]["status"] == "done"

                    resp_list = await http.get("/api/persona/alpha/backups")
                    listed = resp_list.json()["data"]
                    assert resp_list.status_code == 200 and len(listed) == 1 and listed[0]["file"] == backup_file

                    resp_dl = await http.get(f"/api/persona/alpha/backups/{backup_file}/download")
                    assert resp_dl.status_code == 200 and resp_dl.content[:2] == b"PK"

                    resp_del = await http.delete(f"/api/persona/alpha/backups/{backup_file}")
                    assert resp_del.status_code == 200 and resp_del.json()["status"] == 0
                    assert await service.list_backups("alpha") == []
            finally:
                web_app.dependency_overrides.pop(require_admin, None)

            # prepare 阶段 sweep 故障不锁死任务；新重置自动清理磁盘旧断点游标
            def _boom_sweep() -> int:
                raise RuntimeError("simulated sweep error before freeze")

            with monkeypatch.context() as mp:
                mp.setattr(archive, "sweep_stale_sync", _boom_sweep)
                fail_start = await service.start_reset("alpha", ["self_memory"], "alpha", False, False)
                assert not fail_start["error"] and fail_start["job_id"]
                fail_view = await _wait_job(fail_start["job_id"])
                assert fail_view["status"] == "failed" and jobs.active_job() is None

            archive.JOBS_DIR.mkdir(parents=True, exist_ok=True)
            stale_cursor = archive.JOBS_DIR / "stale_alpha_job.json"
            stale_cursor.write_text(
                json.dumps(
                    {
                        "job_id": "stale_alpha_job",
                        "persona": "alpha",
                        "file": "dummy.zip",
                        "scope_keys": ["self:10001:alpha"],
                        "kind_index": 1,
                        "chunk_index": 0,
                        "last_id": "",
                        "done": 1,
                        "total": 3,
                    }
                ),
                encoding="utf-8",
            )
            fresh_start = await service.start_reset("alpha", ["self_memory"], "alpha", False, False)
            assert not fresh_start["error"] and fresh_start["job_id"]
            assert (await _wait_job(fresh_start["job_id"]))["status"] == "done" and not stale_cursor.exists()

            # clear_ops 按前缀清理也覆盖仅有偏好行的 scope
            from gsuid_core.ai_core.memory.database.clear_ops import clear_memories_for_scope_async

            async with maker() as s:
                s.add(
                    AIMemPreference(
                        id="pref-only-1",
                        scope_key="user_global:prefonly_1",
                        user_id="prefonly_1",
                        preference_rule="仅偏好规则",
                    )
                )
                await s.commit()
            cleared_pref_only = await clear_memories_for_scope_async(scope_pattern="user_global:prefonly_")
            assert cleared_pref_only["status"] == 0 and cleared_pref_only["data"]["deleted_preferences"] == 1

            # 旧格式备份兼容：偏好嵌在 global_user（无 user_preferences 项），覆盖预估与清删还原均准确
            import gzip

            async with maker() as s:
                s.add(
                    AIMemPreference(
                        id="pref-extra-1",
                        scope_key="user_global:100000099",
                        user_id="100000099",
                        preference_rule="仅偏好待清",
                    )
                )
                await s.commit()

            legacy_name = "20260101_000000_legacy.zip"
            legacy_zip = archive.persona_backup_dir("alpha") / legacy_name
            legacy_zip.parent.mkdir(parents=True, exist_ok=True)
            legacy_pref_row = {
                "id": "pref-legacy-1",
                "scope_key": "user_global:100000001",
                "user_id": "100000001",
                "target_context": "general",
                "preference_rule": "旧备份偏好规则",
                "polarity": "do",
                "is_correction": False,
                "is_active": True,
                "source_episode_id": None,
                "mention_count": 1,
                "created_at": now_dt.isoformat(),
                "updated_at": now_dt.isoformat(),
                "last_applied_at": None,
            }
            legacy_manifest = {
                "version": archive.MANIFEST_VERSION,
                "persona": "alpha",
                "created_at": int(time.time()),
                "items": ["global_user"],
                "counts": {"global_user/aimempreference": 1},
                "scope_keys": {"global_user": ["user_global:100000001"]},
                "tasks": [],
                "embedding_model": "",
            }
            with zipfile.ZipFile(legacy_zip, "w") as zf:
                zf.writestr("manifest.json", json.dumps(legacy_manifest, ensure_ascii=False))
                zf.writestr(
                    "items/global_user/aimempreference.jsonl.gz",
                    gzip.compress((json.dumps(legacy_pref_row, ensure_ascii=False) + "\n").encode("utf-8")),
                )
            try:
                lprev = await service.preview_restore("alpha", legacy_name)
                assert not isinstance(lprev, str)
                # 当前 user_global:* 有 1 个实体（ent-u1）+ 2 条偏好（pref-u1, pref-extra-1）= 3
                assert lprev["overwrite_counts"]["global_user"] == 3
                lstart = await service.start_restore("alpha", legacy_name, ["global_user"], "alpha")
                assert not lstart["error"] and lstart["job_id"]
                assert (await _wait_job(lstart["job_id"]))["status"] == "done"
                async with maker() as s:
                    assert sorted(p.id for p in (await s.execute(select(AIMemPreference))).scalars().all()) == [
                        "pref-b1",
                        "pref-legacy-1",
                    ]
            finally:
                legacy_zip.unlink(missing_ok=True)

            # 负向防线：篡改备份的 self_models.json 越权 scope（其他人格 / 系统键 / 跨组）一律拒绝
            tampered_name = "20260101_000001_tampered.zip"
            tampered_zip = archive.persona_backup_dir("alpha") / tampered_name
            tampered_manifest = {
                "version": archive.MANIFEST_VERSION,
                "persona": "alpha",
                "created_at": int(time.time()),
                "items": ["self_memory"],
                "counts": {"self_memory/self_models": 4},
                "scope_keys": {"self_memory": ["self:10001:alpha"]},
                "tasks": [],
                "embedding_model": "",
            }
            tampered_models = {
                "self:10001:alpha": {"preferences_learned": ["合法恢复"]},
                "self:10001:beta": {"preferences_learned": ["越权篡改beta"]},
                "system:migrations": {"hacked": True},
                "group:group_1": {"commitments": ["越权篡改群"]},
            }
            with zipfile.ZipFile(tampered_zip, "w") as zf:
                zf.writestr("manifest.json", json.dumps(tampered_manifest, ensure_ascii=False))
                zf.writestr(
                    "items/self_memory/self_models.json",
                    json.dumps(tampered_models, ensure_ascii=False),
                )
            try:
                tstart = await service.start_restore("alpha", tampered_name, ["self_memory"], "alpha")
                assert not tstart["error"] and tstart["job_id"]
                assert (await _wait_job(tstart["job_id"]))["status"] == "done"
                assert await state_get_value("self:10001:alpha", "self_model") == {"preferences_learned": ["合法恢复"]}
                assert await state_get_value("self:10001:beta", "self_model") == {
                    "preferences_learned": ["用户(100000001)：beta专属称呼"]
                }
                assert await state_get_value("system:migrations", "self_model") is None
                assert await state_get_value("group:group_1", "self_model") == {"commitments": ["群里一起测试"]}
            finally:
                tampered_zip.unlink(missing_ok=True)

            async with maker() as s:
                await s.execute(sql_delete(AIMemEdge))
                await s.commit()

    async def _run() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="reset_e2e.db") as (_, maker):
            monkeypatch.setattr(mem_models, "async_maker", maker)
            await scenario(maker)

    try:
        asyncio.run(_run())
    finally:
        clear_journal_caches()
