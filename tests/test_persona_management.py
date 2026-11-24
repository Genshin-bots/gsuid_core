"""人格管理契约：来源标记、整包定位、门禁、多独立人格状态与关系隔离。"""

from __future__ import annotations

import asyncio
from typing import Dict
from pathlib import Path

import pytest
from sqlmodel import SQLModel
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.utils.database import base_models
from gsuid_core.ai_core.persona import store as persona_store
from gsuid_core.ai_core.persona.bundle import BundleError
from gsuid_core.utils.plugins_config.models import GsStrConfig, GsListStrConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig


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


def test_source_marker_bundle_root_and_default_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.webconsole.persona_api import _managed_block
    from gsuid_core.ai_core.migrations.persona_scope_v1 import pick_default_persona

    _patch_persona_path(tmp_path, monkeypatch)
    p = tmp_path / "sayu"
    p.mkdir(parents=True)
    (p / "persona.md").write_text("# sayu\n", encoding="utf-8")

    assert persona_store.read_source("sayu") is None and not persona_store.is_git_managed("sayu")
    assert _managed_block("sayu") is None

    persona_store.write_source("sayu", _make_source())
    src = persona_store.read_source("sayu")
    assert src is not None and src["repo_name"] == "persona_a" and persona_store.is_git_managed("sayu")
    blocked = _managed_block("sayu")
    assert blocked is not None and blocked["status"] == 1

    persona_store.drop_source("sayu")
    assert not persona_store.is_git_managed("sayu")
    (p / persona_store.SOURCE_MARKER).write_text("{bad", encoding="utf-8")
    assert persona_store.read_source("sayu") is None

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

    configs: Dict[str, StringConfig] = {
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


def test_persona_isolation_for_relationship_cognition_and_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gsuid_core.ai_core.relationship import fetch_relationship
    from gsuid_core.ai_core.self_cognition import add_self_note, build_self_cognition_context
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.command_aside.recall import save_advice, lookup_advice

    target = tmp_path / "aside.json"
    monkeypatch.setattr("gsuid_core.ai_core.command_aside.recall._path", lambda: target)
    save_advice(plugin="p", trigger_id="t1", receipt="命令 foo 执行成功", advice="用中文解释结果", persona_name="A")
    assert lookup_advice("p", "命令 foo 执行成功", "t1", "A") == "用中文解释结果"
    assert lookup_advice("p", "命令 foo 执行成功", "t1", "B") == ""

    async def _noop_distill(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("gsuid_core.ai_core.cognition.distill.distill_self_note", _noop_distill)

    async def scenario() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'persona_iso.db').as_posix()}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False, "timeout": 5.0},
        )
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(base_models, "async_maker", maker)
        monkeypatch.setattr(base_models, "engine", engine)
        monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
        monkeypatch.setattr("gsuid_core.ai_core.state_store.store._table_ensured", True)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            async with maker() as session:
                session.add(UserFavorability(user_id="u1", bot_id="b1", persona_name="A", favorability=85))
                await session.commit()

            view_a = await fetch_relationship("u1", "b1", "A")
            assert view_a.scored and view_a.score == 85
            assert not (await fetch_relationship("u1", "b1", "B")).scored

            assert await add_self_note("b1", "A", "我对用户承诺过每天汇报", "self_notes")
            assert "每天汇报" in await build_self_cognition_context("b1", "A", include_relationship=False)
            assert "每天汇报" not in await build_self_cognition_context("b1", "B", include_relationship=False)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_journal_compaction_and_active_hours_gating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from typing import Sequence

    from gsuid_core.ai_core.journal import dream as jd, manager as jm, storage as jstorage

    journal_dir = tmp_path / "journals"
    monkeypatch.setattr(jstorage, "JOURNAL_DIR", journal_dir)
    jstorage._SHARD_CACHE.clear()
    jstorage._SHARD_UPDATE_TS.clear()

    async def _no_gt(persona_name: str, shard_id: str, now_ts: float) -> jd.GroundTruthDigest:
        del persona_name, shard_id, now_ts
        return jd.GroundTruthDigest(completed=("测试项目报告",))

    monkeypatch.setattr(jd, "collect_ground_truth", _no_gt)
    monkeypatch.setattr(
        "gsuid_core.ai_core.autonomy.tools.artifact_dir",
        lambda pname, root=None: tmp_path / "notes" / pname,
    )

    async def scenario() -> None:
        llm_called_shards: list[str] = []

        async def _stub_summarize(
            persona_name: str,
            doc: jstorage.ShardDocument,
            gt: jd.GroundTruthDigest,
            prev_month_rollover: Sequence[str] = (),
        ) -> jd.DreamLLMOutcome:
            del persona_name, prev_month_rollover
            llm_called_shards.append(doc.shard_id)
            done_tag = gt.completed[0] if gt.completed else "日常"
            return {
                "persistent": ["保持核实后再作答的习惯。"],
                "monthly": [f"[已完结] {done_tag} 已闭环交付。"],
                "yesterday": ["做梦整理：已跟进各项事务。"],
            }

        monkeypatch.setattr(jd, "_summarize_journal_with_llm", _stub_summarize)

        assert await jd.compact_journal("tester") is False

        # 旧版单文件写盘 → 自动无损拆迁至 self.md 与 groups/group_1001.md
        legacy_file = journal_dir / "tester.md"
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

        compacted = await jd.compact_journal("tester", force=True)
        assert compacted is True
        assert not legacy_file.exists()

        self_path = jm.get_journal_path("tester")
        grp_path = jm.get_journal_path("tester", group_id="group_1001")
        assert self_path.exists() and grp_path.exists()

        standing, timeline = jstorage._read_sections(jm.get_journal_content("tester"))
        assert len(timeline) == 0
        assert "做梦整理：已跟进各项事务" in standing
        assert "[已完结] 测试项目报告" in grp_path.read_text(encoding="utf-8")
        assert jstorage.get_last_compact_ts("tester") > 0.0

        # 验证 429 临时故障保护：非强制夜间巡检遇 transient_error 绝不清空流水
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

        async def _stub_transient(
            persona_name: str,
            doc: jstorage.ShardDocument,
            gt: jd.GroundTruthDigest,
            prev_month_rollover: Sequence[str] = (),
        ) -> jd.DreamLLMOutcome:
            del persona_name, doc, gt, prev_month_rollover
            return "transient_error"

        monkeypatch.setattr(jd, "_summarize_journal_with_llm", _stub_transient)
        assert await jd.compact_journal("tester", force=False) is False
        _, kept_lines = jstorage._read_sections(grp_path.read_text(encoding="utf-8"))
        assert len(kept_lines) == 1

        # LLM 未启用返回 None 时走确定性规则跨日兜底，同时写入月度归档
        async def _stub_none(
            persona_name: str,
            doc: jstorage.ShardDocument,
            gt: jd.GroundTruthDigest,
            prev_month_rollover: Sequence[str] = (),
        ) -> jd.DreamLLMOutcome:
            del persona_name, doc, gt, prev_month_rollover
            return None

        monkeypatch.setattr(jd, "_summarize_journal_with_llm", _stub_none)
        assert await jd.compact_journal("tester", force=True) is True
        standing_fb, tl_fb = jstorage._read_sections(grp_path.read_text(encoding="utf-8"))
        assert len(tl_fb) == 0
        assert "核对了第一份数据" in standing_fb and "journal:tester:group_1001:" in standing_fb
        assert await jd.compact_journal("tester") is False

        # 测试作息窗口门禁与自然睡眠日去重（消除 24h 时间戳漂移）
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

        import json

        def _write_test_compact_ts(ts_val: float) -> None:
            st_file = journal_dir / ".journal_state.json"
            st_data = json.loads(st_file.read_text(encoding="utf-8")) if st_file.exists() else {}
            entry = st_data["tester"] if "tester" in st_data and isinstance(st_data["tester"], dict) else {}
            entry["last_compact_ts"] = ts_val
            entry["last_dream_date"] = jd.logical_sleep_date("Asia/Shanghai", ts_val)
            st_data["tester"] = entry
            st_file.write_text(json.dumps(st_data, ensure_ascii=False), encoding="utf-8")

        monkeypatch.setattr(jd, "is_within_active_hours", lambda *args, **kwargs: False)
        _write_test_compact_ts(time.time() - 3600)
        await jd.check_and_compact_all_journals()
        assert len(jstorage.parse_shard_markdown(self_path.read_text(encoding="utf-8")).timeline) == 1

        _write_test_compact_ts(time.time() - 90000)
        await jd.check_and_compact_all_journals()
        assert len(jstorage.parse_shard_markdown(self_path.read_text(encoding="utf-8")).timeline) == 0

    asyncio.run(scenario())


def test_journal_sharding_sanitization_and_dream_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import time
    from typing import Sequence

    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.models.test import TestModel

    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.journal import dream as jd, manager as jm, storage as jstorage, sanitizer as js
    from gsuid_core.ai_core.autonomy.models import FactPack, DeliveryPin, DeliveryDecision, AutonomyStepTrace
    from gsuid_core.ai_core.autonomy.delivery import parse_footer
    from gsuid_core.ai_core.autonomy.scheduler import _record_autonomy_journal
    from gsuid_core.ai_core.planning.handle_resolver import resolve_handle
    from gsuid_core.ai_core.planning.tool_output_tools import _handle_access_allowed

    _setup_isolated_persona_env(tmp_path / "personas_cfg", monkeypatch)
    _seed_persona(
        tmp_path / "personas_cfg",
        "alpha",
        "specific",
        ["group_1", "group_hot", "group_quiet", "villa_1:room_9"],
    )

    journal_dir = tmp_path / "journals"
    notes_root = tmp_path / "autonomy_notes"
    monkeypatch.setattr(jstorage, "JOURNAL_DIR", journal_dir)
    monkeypatch.setattr(
        "gsuid_core.ai_core.autonomy.tools.artifact_dir",
        lambda pname, root=None: notes_root / pname,
    )
    jstorage._SHARD_CACHE.clear()
    jstorage._SHARD_UPDATE_TS.clear()
    jstorage._RECENT_ENTRY_TS.clear()

    # 1. 句界与 ASCII 词元边界保护（复现截图 148%，R 与 Markd 腰斩 + 多行吃字 + 报错拦截）
    raw_rsi = (
        "查了一下群友提到的测试新材，抓到的数据是：页面（11-13 更新）显示昨收 43.44，"
        "当日区间 42.92~46.82，近一年涨幅约 148%，RSI(14) 41.21，市值约 69.5 亿。"
    )
    cut_rsi = js.sanitize_journal_summary(raw_rsi, max_chars=80)
    assert not cut_rsi.endswith("R") and not cut_rsi.endswith("R…")

    raw_md = "官方机器人平台翻了翻，频道和群聊都能跑，消息能收也能发，支持文本、Markdown、图片、语音和文件。"
    cut_md = js.sanitize_journal_summary(raw_md, max_chars=46)
    assert "Markd…" not in cut_md and not cut_md.endswith("Markd")

    multiline = "<reply #12>\n第一段结论核实完毕\n第二段补充细节 <@100000001>"
    flat = js.sanitize_journal_summary(multiline, max_chars=110, nicks={"100000001": "测试用户A"})
    assert "\n" not in flat and "@测试用户A" in flat
    assert js.is_rejected_journal_body("执行出错: 模型套餐用量已达上限") is True
    assert js.is_rejected_journal_body("这次的定时任务没能执行成功，稍后再让我试试吧") is True
    assert js.is_rejected_journal_body("DRIVE: group_topic\nTARGET: group_1") is True
    assert js.clean_journal_tag("web_search_tool") == "查资料"

    async def scenario() -> None:
        from datetime import datetime, timedelta

        from gsuid_core.utils.database import base_models as db_base
        from gsuid_core.ai_core.planning import tool_output_helper as toh
        from gsuid_core.ai_core.database.models import AIAutonomyActivity
        from gsuid_core.ai_core.planning.models import AIAgentTask

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(db_base, "async_maker", maker)

        now = time.time()

        # 2. 子任务归属路由（未传或旧已删人格按群回落至 alpha，未启用群绝不广播给无关人格）
        assert jm.resolve_persona_for_group_or_session(persona_name=None, group_id="group_1") == "alpha"
        assert jm.resolve_persona_for_group_or_session(persona_name="deleted_ghost", group_id="group_1") == "alpha"
        await jm.record_journal_entry(
            jstorage.JournalEntry(
                now, "2026-11-18", "09:00", "查资料", "未绑定群的流水不应污染其它人格", group_id="group_unassigned"
            )
        )
        assert not (journal_dir / "personas" / "alpha" / "groups" / "group_unassigned.md").exists()

        # 3. Autonomy parse_footer 后写入干净 FACT+TAKE+长笔记句柄（兼容 500 字截断 JSON），且隔离私聊关怀与 429 报错
        footer_only_out = (
            "DRIVE: group_topic\nTARGET: group_1\nTOPIC: 隔夜指标盘点\nOUTCOME: found\n"
            "FACT: 三大指数回落，波动率抬升\nTAKE: 数字看多了眼花，先记在本子上。"
        )
        parsed = parse_footer(footer_only_out)
        truncated_long_args = ('{"summary": "隔夜盘点小记", "body": "' + "详细核查记录文本" * 80)[:500]
        step = AutonomyStepTrace(
            step_index=0,
            tool_name="artifact_put",
            args_summary=truncated_long_args,
            output_text="已写入笔记《隔夜盘点小记》（句柄: note:alpha:隔夜盘点小记）",
        )
        pin = DeliveryPin(
            user_id="",
            group_id="group_1",
            is_master=False,
            zone_value="familiar",
            bot_id="onebot",
            bot_self_id="10001",
            ws_bot_id="ws1",
            session_key="group:group_1",
            last_human_ts=now - 60,
        )
        await _record_autonomy_journal(
            persona_name="alpha",
            raw_text=footer_only_out,
            steps=[step],
            spoken_text=parsed.spoken_text,
            topic=parsed.topic,
            fact=parsed.fact,
            take=parsed.take,
            decision=DeliveryDecision("group_topic", "staged_merge", pin, parsed.summary),
            facts=FactPack(["artifact_put"], ["ok"], [], True),
            pin=pin,
            mode="staged_merge",
            tool_sent=False,
            last_sent_group_id="",
        )
        # 私聊主动关怀（care_user）写入独立 users/{uid}.md，绝不写入公共 self.md；空 uid 须 fail-closed 丢弃
        dm_pin = DeliveryPin(
            user_id="100000001",
            group_id="",
            is_master=False,
            zone_value="intimate",
            bot_id="onebot",
            bot_self_id="10001",
            ws_bot_id="ws1",
            session_key="direct:100000001",
            last_human_ts=now - 60,
        )
        await _record_autonomy_journal(
            persona_name="alpha",
            raw_text="给你带了一句私聊提醒",
            steps=[],
            spoken_text="给你带了一句私聊提醒",
            topic="私聊问候",
            fact="",
            take="",
            decision=DeliveryDecision("care_user", "immediate_push", dm_pin, "给你带了一句私聊提醒"),
            facts=FactPack(["send_message_by_ai"], ["ok"], [], True),
            pin=dm_pin,
            mode="immediate_push",
            tool_sent=True,
            last_sent_group_id="",
            last_sent_user_id="100000001",
        )
        await _record_autonomy_journal(
            persona_name="alpha",
            raw_text="缺少目标用户的私聊关怀不应污染公共手账",
            steps=[],
            spoken_text="缺少目标用户的私聊关怀不应污染公共手账",
            topic="空目标",
            fact="",
            take="",
            decision=DeliveryDecision("care_user", "silent_only", None, "空目标测试"),
            facts=FactPack(["web_search_tool"], ["ok"], [], True),
            pin=None,
            mode="silent_only",
            tool_sent=False,
            last_sent_group_id="",
            last_sent_user_id="",
        )
        assert not (journal_dir / "personas" / "alpha" / "self.md").exists()
        u1_auto_path = jstorage._user_shard_path("alpha", "100000001")
        assert u1_auto_path.exists()
        u1_auto_raw = u1_auto_path.read_text(encoding="utf-8")
        assert "自主探索" in u1_auto_raw and "给你带了一句私聊提醒" in u1_auto_raw
        await _record_autonomy_journal(
            persona_name="alpha",
            raw_text="执行出错: 模型套餐用量已达上限",
            steps=[],
            spoken_text="执行出错: 模型套餐用量已达上限",
            topic="",
            fact="",
            take="",
            decision=DeliveryDecision("rest", "silent_only", None, ""),
            facts=FactPack([], [], [], False),
            pin=None,
            mode="silent_only",
            tool_sent=False,
            last_sent_group_id="",
        )
        g1_text, _ = jm.read_journal_for_context("alpha", current_group_id="group_1")
        assert "DRIVE:" not in g1_text
        assert "三大指数回落" in g1_text and "note:alpha:隔夜盘点小记" in g1_text
        assert "用量已达上限" not in g1_text

        # 4. 跨行静默巡检折叠（中间夹杂普通行动时仍能原位合并为 ×2，不泄漏 <<NO_BROADCAST>>）
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
        assert len(gq_doc.timeline) == 2
        assert "<<NO_BROADCAST>>" not in "\n".join(gq_doc.timeline)
        assert "(×2)" in gq_doc.timeline[-1] and "09:30~10:30" in gq_doc.timeline[-1]

        # 4b. 跨日残留流水（前晚 20:55 留在今日 09:43 之前）不得被套上今日日期后当成「刚刚经历」反复注入
        from datetime import datetime as _dt

        today_str = _dt.fromtimestamp(now).strftime("%Y-%m-%d")
        cur_hhmm = _dt.fromtimestamp(now).strftime("%H:%M")
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
        jstorage._SHARD_CACHE.pop(stale_path, None)
        jstorage._SHARD_UPDATE_TS.pop(("alpha", "group_rollover"), None)
        jstorage._RECENT_ENTRY_TS.pop(("alpha", "group_rollover"), None)
        delta_roll, _ = jm.read_journal_for_context("alpha", current_group_id="group_rollover", since_ts=now - 30.0)
        assert "昨夜遗留的旧播报内容" not in delta_roll
        anchor_roll, _ = jm.read_journal_for_context("alpha", current_group_id="group_rollover")
        assert "昨夜遗留的旧播报内容" not in anchor_roll and "今日当前已完成记录" in anchor_roll

        # 5. 超大群突发洪峰环形定长（单日写入 75 条仍严格 <= 50 行，锚点严格 <= 880 字，且零污染其它群）
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
        assert len(hot_anchor) <= jstorage.ANCHOR_TOTAL_MAX_CHARS
        assert "早先概览" in hot_anchor
        assert "高频资料核验" not in quiet_anchor and "高频资料核验" not in dm_anchor

        # 6. 做梦三级阀门 + 双 ID 平台群号 (:) + 长摘要归档句柄防截断 + 并发 dirty_shards 防丢失 + 路径穿越拦截
        await jm.record_journal_entry(
            jstorage.JournalEntry(
                now + 90, "2026-11-18", "12:00", "查资料", "核查了双ID房间的测试配置。", group_id="villa_1:room_9"
            ),
            target_personas=["alpha"],
        )
        heavy_llm_shards: list[str] = []

        async def _track_llm(
            persona_name: str,
            doc: jstorage.ShardDocument,
            gt: jd.GroundTruthDigest,
            prev_month_rollover: Sequence[str] = (),
        ) -> jd.DreamLLMOutcome:
            del persona_name, gt, prev_month_rollover
            heavy_llm_shards.append(doc.shard_id)
            if doc.shard_id == "group_hot":
                # 模拟做梦耗时期间另一人格 beta 并发写入 dirty_shards，验证不会被覆盖抹掉
                jstorage._mark_shard_dirty_sync("beta", "group_concurrent")
            return {
                "persistent": ["高频核查保持结论先行。"],
                "monthly": ["本月持续跟进大群高频测试专题。"],
                "yesterday": [
                    "昨日从早到晚密集完成了数十次资料比对、图片文字核验与多轮统计口径复核，确保各项数据准确无误。"
                ],
            }

        monkeypatch.setattr(jd, "_summarize_journal_with_llm", _track_llm)
        # group_1 / villa_1:room_9 仅 1 条（轻度分片）→ 0 LLM；group_hot 50 条（重度分片）→ 触发 LLM
        assert await jd.compact_journal("alpha", force=False) is True
        assert "group_hot" in heavy_llm_shards
        assert "group_1" not in heavy_llm_shards
        assert "villa_1:room_9" not in heavy_llm_shards
        assert "group_concurrent" in jstorage._load_full_state()["beta"]["dirty_shards"]

        gh_after = jstorage.parse_shard_markdown(
            jm.get_journal_path("alpha", "group_hot").read_text(encoding="utf-8"), "group_hot"
        )
        sleep_day = gh_after.yesterday_label
        # 长昨日摘要追加归档句柄后绝不腰斩句柄尾部
        assert f"journal:alpha:group_hot:{sleep_day}" in gh_after.yesterday[-1]

        # 双 ID 群号 (villa_1:room_9) 归档句柄完整可解，且恶意路径穿越一律拒绝返回 None
        dual_hid = f"journal:alpha:villa_1:room_9:{sleep_day}"
        resolved_dual = await resolve_handle(dual_hid)
        assert resolved_dual is not None and "双ID房间" in (resolved_dual.payload_inline or "")
        assert await resolve_handle("journal:..:self:2026-11-18") is None
        assert await resolve_handle("journal:alpha:group_1:../../etc") is None

        g1_after = jstorage.parse_shard_markdown(
            jm.get_journal_path("alpha", "group_1").read_text(encoding="utf-8"), "group_1"
        )
        assert len(g1_after.yesterday) >= 1 and len(g1_after.timeline) == 0
        arch_hid = f"journal:alpha:group_1:{sleep_day}"
        resolved_arch = await resolve_handle(arch_hid)
        assert resolved_arch is not None and "三大指数回落" in (resolved_arch.payload_inline or "")

        def _make_ctx(ev: Event) -> RunContext[ToolContext]:
            return RunContext(
                deps=ToolContext(bot=None, ev=ev),
                model=TestModel(),
                usage=RunUsage(),
                prompt="check",
            )

        ev_g1 = Event(bot_id="onebot", bot_self_id="1", user_id="100000001", user_type="group", group_id="group_1")
        ev_g2 = Event(
            bot_id="onebot", bot_self_id="1", user_id="group_quiet", user_type="group", group_id="group_quiet"
        )
        ev_dm = Event(bot_id="onebot", bot_self_id="1", user_id="100000001", user_type="direct", group_id=None)
        assert await _handle_access_allowed(resolved_arch, _make_ctx(ev_g1)) is True
        assert await _handle_access_allowed(resolved_arch, _make_ctx(ev_g2)) is False
        assert await _handle_access_allowed(resolved_arch, _make_ctx(ev_dm)) is False

        # 7. Phase 3 个人笔记 Wiki 整编（零垂直词表 + 防止『关于_XX_笔记』误聚）+ 365 天休眠群归档与唤醒
        alpha_notes = notes_root / "alpha"
        alpha_notes.mkdir(parents=True, exist_ok=True)
        scope_hdr = "<!-- gscore-scope: self:1:alpha | owner: 100000001 -->\n"
        for idx, suffix in enumerate(("小抄", "小笔记", "调查")):
            (alpha_notes / f"体态拉伸_{suffix}.txt").write_text(
                f"{scope_hdr}第 {idx + 1} 次记录：每日拉伸 15 分钟有助于放松背部姿态。",
                encoding="utf-8",
            )
        for distinct_topic in ("睡眠规律", "饮水习惯", "桌面收纳"):
            (alpha_notes / f"关于_{distinct_topic}_笔记.txt").write_text(
                f"{scope_hdr}独立主题记录：{distinct_topic}。",
                encoding="utf-8",
            )
        old_snap = alpha_notes / "2026-10-01_日常速记.txt"
        old_snap.write_text("单日临时快照数据。", encoding="utf-8")
        old_ts = now - 15 * 86400
        os.utime(old_snap, (old_ts, old_ts))

        merged_n, archived_n = await jd.consolidate_persona_notes(
            "alpha", notes_dir=alpha_notes, now_ts=now, allow_llm=False
        )
        assert merged_n == 1 and archived_n == 4
        assert (alpha_notes / "topic_体态拉伸.md").exists()
        assert not (alpha_notes / "topic_关于.md").exists()
        assert not old_snap.exists()
        # 合并后的新专题句柄与已归档的旧碎片句柄均可通过 read_handle 无损解析
        assert await resolve_handle("note:alpha:topic_体态拉伸") is not None
        assert await resolve_handle("note:alpha:体态拉伸_小抄") is not None

        # 超 60 天未活跃群与私聊分片自动迁入 dormant_*，再次读取时无缝唤醒
        g1_file = jm.get_journal_path("alpha", "group_1")
        u1_file = jstorage._user_shard_path("alpha", "100000001")
        ancient_ts = now - 70 * 86400
        os.utime(g1_file, (ancient_ts, ancient_ts))
        os.utime(u1_file, (ancient_ts, ancient_ts))
        jstorage._SHARD_CACHE.clear()
        dormant_cnt, _ = await jd.run_storage_gc_sync(jstorage._persona_root("alpha"), now)
        assert dormant_cnt >= 2 and not g1_file.exists() and not u1_file.exists()
        assert jstorage._dormant_user_path("alpha", "100000001").exists()
        woken_text, _ = jm.read_journal_for_context("alpha", current_group_id="group_1")
        assert "三大指数回落" in woken_text and g1_file.exists()
        woken_dm, _ = jm.read_journal_for_context("alpha", current_group_id=None, current_user_id="100000001")
        assert "给你带了一句私聊提醒" in woken_dm and u1_file.exists()

        # 私聊归档句柄解析回填 owner_user_id，且私聊规则跨日不混入「群聊」口吻
        dm_arch_hid = f"journal:alpha:user:100000001:{sleep_day}"
        resolved_dm_arch = await resolve_handle(dm_arch_hid)
        assert resolved_dm_arch is not None
        assert resolved_dm_arch.scope_key == "user:100000001"
        assert resolved_dm_arch.owner_user_id == "100000001"
        dm_rule_slots = jd.rule_rollover_shard(
            jstorage.ShardDocument(shard_id="user:100000001", timeline=["- [10:00 | 自主探索] 提醒休息"]),
            persona_name="alpha",
            sleep_date=sleep_day,
            current_month=sleep_day[:7],
            ground_truth=jd.GroundTruthDigest(),
        )
        assert all("群聊" not in item for item in dm_rule_slots["persistent"])

        # 8. 实库验证 Phase 1 真值对账（completed 入已完结，>24h 僵尸审批项不进 in_flight，私聊关联 care_user）
        now_dt = datetime.fromtimestamp(now)
        async with maker() as s:
            s.add(
                AIAgentTask(
                    id="t_done",
                    goal="完成分析",
                    display_name="年度报表核查",
                    status="completed",
                    group_id="group_1",
                    updated_at=now_dt - timedelta(hours=2),
                )
            )
            s.add(
                AIAgentTask(
                    id="t_zombie",
                    goal="旧审批",
                    display_name="过期十天审批单",
                    status="waiting_approval",
                    group_id="group_1",
                    updated_at=now_dt - timedelta(hours=30),
                )
            )
            s.add(
                AIAgentTask(
                    id="t_dm_alpha",
                    goal="私聊跟进",
                    display_name="本人格私聊事项",
                    status="completed",
                    persona_name="alpha",
                    owner_user_id="100000001",
                    group_id=None,
                    updated_at=now_dt - timedelta(hours=1),
                )
            )
            s.add(
                AIAgentTask(
                    id="t_dm_beta",
                    goal="私聊跟进",
                    display_name="他人格私聊事项",
                    status="completed",
                    persona_name="beta",
                    owner_user_id="100000001",
                    group_id=None,
                    updated_at=now_dt - timedelta(hours=1),
                )
            )
            s.add(
                AIAutonomyActivity(
                    persona_name="alpha",
                    bot_id="onebot",
                    bot_self_id="1",
                    drive_kind="self_explore",
                    summary="自主整理了读书摘要",
                    created_at=now - 600,
                )
            )
            s.add(
                AIAutonomyActivity(
                    persona_name="alpha",
                    bot_id="onebot",
                    bot_self_id="1",
                    drive_kind="care_user",
                    target_user_id="100000001",
                    target_session_key="private:100000001",
                    summary="私下问候作息进展",
                    created_at=now - 300,
                )
            )
            s.add(
                AIAutonomyActivity(
                    persona_name="alpha",
                    bot_id="onebot",
                    bot_self_id="1",
                    drive_kind="care_user",
                    target_user_id="100000001",
                    target_session_key="group:group_1",
                    summary="群里提过的公共事项",
                    created_at=now - 200,
                )
            )
            await s.commit()

        gt_g1 = await jd.collect_ground_truth("alpha", "group_1", now)
        assert "年度报表核查" in gt_g1.completed
        assert "过期十天审批单" not in gt_g1.in_flight
        gt_self = await jd.collect_ground_truth("alpha", "__self__", now)
        assert any("读书摘要" in x for x in gt_self.autonomy_facts)
        gt_dm = await jd.collect_ground_truth("alpha", "user:100000001", now)
        assert any("私下问候作息进展" in x for x in gt_dm.autonomy_facts)
        assert all("群里提过的公共事项" not in x for x in gt_dm.autonomy_facts)
        assert "本人格私聊事项" in gt_dm.completed
        assert "他人格私聊事项" not in gt_dm.completed

        # 9. persist_subagent_result 在 persona_name=None 时按 group_id 路由至 alpha，不落 default
        from gsuid_core.ai_core.planning.tool_output_store import PersistedHandleCard

        async def _fake_write_rec(**kwargs: object) -> PersistedHandleCard:
            del kwargs
            return PersistedHandleCard(
                id="sa_test0001",
                kind="tool_output",
                mime="text/plain",
                summary="子代理测试结果摘要",
                size_bytes=200,
                read_tool="read_handle",
            )

        monkeypatch.setattr(toh, "_write_record", _fake_write_rec)
        long_sub_body = (
            "完整的子代理调研结论内容，包含多维度测试指标核查与交叉对比结论，"
            "总字符数超过四十个字符门槛以便触发手账自动回流落盘流程。"
        )
        sub_task = AIAgentTask(
            id="t_sub",
            goal="调研",
            display_name="调研子任务",
            persona_name=None,
            group_id="group_1",
            scope_key="group:group_1",
            owner_user_id="100000001",
        )

        def _read_raw_text(p: Path) -> str:
            return p.read_text(encoding="utf-8")

        def _read_timeline_now(p: Path) -> list[str]:
            return jstorage.parse_shard_markdown(_read_raw_text(p)).timeline if p.exists() else []

        async def _wait_timeline_has(shard_path: Path, needle: str, timeout: float = 1.5) -> list[str]:
            deadline = time.monotonic() + timeout
            lines: list[str] = []
            while time.monotonic() < deadline:
                lines = _read_timeline_now(shard_path)
                if any(needle in ln for ln in lines):
                    return lines
                await asyncio.sleep(0.01)
            return lines

        g1_shard_path = jm.get_journal_path("alpha", "group_1")
        await toh.persist_subagent_result(
            profile="research_agent",
            content=long_sub_body,
            task=sub_task,
            res_handle="res_test1",
        )
        tl_after_sub = await _wait_timeline_has(g1_shard_path, "sa_test0001")
        assert any("sa_test0001" in ln for ln in tl_after_sub)
        assert not (journal_dir / "personas" / "default" / "groups" / "group_1.md").exists()

        # 私聊子代理产出路由至 users/{uid}.md，缺 uid 时 fail-closed 不落 self.md
        dm_sub_task = AIAgentTask(
            id="t_sub_dm",
            goal="私聊调研",
            display_name="私聊调研子任务",
            persona_name="alpha",
            group_id=None,
            scope_key="user_global:100000001",
            owner_user_id="100000001",
        )
        await toh.persist_subagent_result(
            profile="research_agent",
            content=(
                "私聊专属调研结论汇总，涵盖核心数据交叉对比、阶段进展评估与后续跟进建议，"
                "总字符数超过四十个字符门槛以便触发私聊手账自动回流。"
            ),
            task=dm_sub_task,
            res_handle="res_test_dm",
        )
        tl_dm_sub = await _wait_timeline_has(u1_file, "sa_test0001")
        assert any("sa_test0001" in ln and "私聊专属调研结论" in ln for ln in tl_dm_sub)
        bad_dm_task = AIAgentTask(
            id="t_sub_dm_nouid",
            goal="缺失属主",
            display_name="无属主私聊任务",
            persona_name="alpha",
            group_id=None,
            scope_key="user_global:",
            owner_user_id="",
        )
        await toh.persist_subagent_result(
            profile="research_agent",
            content="这条缺少属主用户编号的私聊子任务结果绝对不能回退写入公共自我手账文件。",
            task=bad_dm_task,
            res_handle="res_bad_dm",
        )
        await asyncio.sleep(0.03)
        assert not (journal_dir / "personas" / "alpha" / "self.md").exists()

        # 10. 主 Agent settle 回流边界：看板回灌结单入账，Aside 旁白与无 ev 调用隔离不入账
        from gsuid_core.ai_core import session_logger as sl_mod
        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.control.lines import delivery_wake_line

        monkeypatch.setattr(sl_mod, "AI_SESSION_LOGS_PATH", tmp_path / "session_logs")
        monkeypatch.setattr(sl_mod, "AI_SUBAGENT_LOGS_PATH", tmp_path / "session_logs" / "subagents")
        offline_test_model = TestModel(
            call_tools=[],
            custom_output_text="年度报表核查结果已经全部整理完毕，各项指标均符合预期标准。",
        )
        monkeypatch.setattr(
            "gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _level, _kind=None: offline_test_model
        )
        monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_by_full_name", lambda _name: offline_test_model)

        aside_agent = create_agent(
            system_prompt="测试角色",
            persona_name="alpha",
            create_by="Aside",
            session_id="ws1:onebot:1:group:group_1",
        )
        try:
            await aside_agent.run(
                "命令旁白长句子测试，即便超过六十个字符的点评内容也不应该被写入驻群生活手账以免大群命令洪峰灌水污染手账记录。",
                ev=ev_g1,
                tools=[],
                return_mode="return",
                budget_gate=False,
            )
        finally:
            aside_agent._session_logger.close()
        await asyncio.sleep(0.03)
        g1_after_aside = jstorage.parse_shard_markdown(g1_shard_path.read_text(encoding="utf-8"))
        assert not any("年度报表核查结果" in ln for ln in g1_after_aside.timeline)

        chat_agent = create_agent(
            system_prompt="测试角色",
            persona_name="alpha",
            create_by="Chat",
            session_id="ws1:onebot:1:group:group_1",
        )
        try:
            # 无 ev 的内部调用严禁误落入公共 self.md
            await chat_agent.run(
                "这是一句超过六十个字符的无事件内部调用测试输入，用于验证无事件时不会误入公共自我手账文件。",
                ev=None,
                tools=[],
                return_mode="return",
                budget_gate=False,
            )
            wake_msg = delivery_wake_line("【子任务交付】任务#1「年度报表核查」已完成。", "", "", allow_render=False)
            await chat_agent.run(
                wake_msg,
                ev=ev_g1,
                tools=[],
                return_mode="return",
                budget_gate=False,
                is_framework_injection=True,
            )
        finally:
            chat_agent._session_logger.close()
        tl_after_wake = await _wait_timeline_has(g1_shard_path, "交付完结")
        assert any("交付完结" in ln and "年度报表核查结果" in ln for ln in tl_after_wake)
        assert not (journal_dir / "personas" / "alpha" / "self.md").exists()

        # 日常私聊结算（>=60 字）写入 users/{uid}.md 并标「私聊答疑」，绝不污染群手账或 self.md
        dm_long_model = TestModel(
            call_tools=[],
            custom_output_text=(
                "这是私聊里超过六十个字符的详细答疑说明，梳理了今天与明天的个人日程安排、"
                "优先事项清单与关键注意事项，另外留好了晚间复盘时间，按节奏慢慢推进即可。"
            ),
        )
        monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _l, _k=None: dm_long_model)
        monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_by_full_name", lambda _n: dm_long_model)
        dm_chat_agent = create_agent(
            system_prompt="测试角色",
            persona_name="alpha",
            create_by="Chat",
            session_id="ws1:onebot:1:private:100000001",
            dynamic_tools=False,
        )
        try:
            await dm_chat_agent.run(
                "帮我理一下这两天私聊安排",
                ev=ev_dm,
                tools=[],
                return_mode="return",
                budget_gate=False,
            )
        finally:
            dm_chat_agent._session_logger.close()
        tl_dm_chat = await _wait_timeline_has(u1_file, "私聊答疑")
        assert any("私聊答疑" in ln and "个人日程安排" in ln for ln in tl_dm_chat)
        assert not (journal_dir / "personas" / "alpha" / "self.md").exists()
        assert not any("个人日程安排" in ln for ln in _read_timeline_now(g1_shard_path))

        # 验证 send_message_by_ai 发送正文后无论是返回 <SILENCE> 还是短收尾语（如“发好啦”），均保序保留真实已发正文
        from pydantic_ai.tools import Tool

        from gsuid_core.bot import Bot, _Bot

        tail_after_send_model = TestModel(call_tools=["_mock_send_by_ai"], custom_output_text="发好啦~")
        monkeypatch.setattr(
            "gsuid_core.ai_core.gs_agent.get_model_for_task",
            lambda _level, _kind=None: tail_after_send_model,
        )
        monkeypatch.setattr(
            "gsuid_core.ai_core.gs_agent.get_model_by_full_name",
            lambda _name: tail_after_send_model,
        )
        img_agent = create_agent(
            system_prompt="测试角色",
            persona_name="alpha",
            create_by="Chat",
            session_id="ws1:onebot:1:group:group_1",
            dynamic_tools=False,
        )

        async def _mock_send_by_ai() -> str:
            for bubble in ("先看结论汇总Z", "再看分项比例表A，各项比例分开列在表里。"):
                img_agent._run_sent_texts.add(bubble)
                img_agent._run_sent_order.append(bubble)
            return "已成功发送"

        try:
            await img_agent.run(
                "请帮我生成一张比例汇总图",
                ev=ev_g1,
                tools=[Tool(_mock_send_by_ai)],
                return_mode="return",
                budget_gate=False,
            )
        finally:
            img_agent._session_logger.close()
        tl_after_img = await _wait_timeline_has(g1_shard_path, "先看结论汇总Z")
        assert any("先看结论汇总Z；再看分项比例表A" in ln and "发好啦" in ln for ln in tl_after_img)

        # 验证 return 与 by_bot 双模式下被闸门拦截/未出站的泄漏文本均绝不混入手账
        leak_model = TestModel(
            call_tools=[], custom_output_text="<control>内部系统提醒泄露文本超过六十个字符也不得入账</control>"
        )
        monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _level, _kind=None: leak_model)
        monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_by_full_name", lambda _name: leak_model)
        for _mode in ("return", "by_bot"):
            leak_agent = create_agent(
                system_prompt="测试角色",
                persona_name="alpha",
                create_by="Chat",
                session_id="ws1:onebot:1:group:group_1",
            )
            try:
                await leak_agent.run(
                    "测试泄漏拦截",
                    bot=Bot(_Bot("onebot"), ev_g1) if _mode == "by_bot" else None,
                    ev=ev_g1,
                    tools=[],
                    return_mode=_mode,
                    budget_gate=False,
                )
            finally:
                leak_agent._session_logger.close()
        await asyncio.sleep(0.03)
        g1_after_leak = jstorage.parse_shard_markdown(_read_raw_text(g1_shard_path))
        assert not any("内部系统提醒泄露" in ln for ln in g1_after_leak.timeline)

        # 11. 主动出口（scheduled_task / heartbeat）异步入账、拦截失败兜底文案 + WebConsole 占位符过滤与单分片整理
        from gsuid_core.ai_core.proactive import emitter as emitter_mod
        from gsuid_core.ai_core.proactive.dispatcher import UnifiedProactiveDispatcher

        isolated_dispatcher = UnifiedProactiveDispatcher()
        monkeypatch.setattr(emitter_mod, "get_dispatcher", lambda: isolated_dispatcher)

        async def _noop_send_chat(*_a: object, **_kw: object) -> None:
            return None

        async def _noop_sync_main(**_kw: object) -> None:
            return None

        monkeypatch.setattr(emitter_mod, "send_chat_result", _noop_send_chat)
        monkeypatch.setattr(emitter_mod, "_sync_to_main_session", _noop_sync_main)
        await emitter_mod.emit_proactive_message(
            event=ev_g1,
            message="这次的定时任务没能执行成功，稍后再让我试试吧\n⏰ 定时任务 task_fail",
            source="scheduled_task",
            trigger_reason="task_fail",
            bot=Bot(_Bot("onebot"), ev_g1),
            persona_name="alpha",
            suppress_when_heartbeat_recent=False,
        )
        sent_ok = await emitter_mod.emit_proactive_message(
            event=ev_g1,
            message="大家早上好，今天天气很清爽。\n⏰ 定时任务: 晨间问候",
            source="scheduled_task",
            trigger_reason="task_morning",
            bot=Bot(_Bot("onebot"), ev_g1),
            persona_name="alpha",
            suppress_when_heartbeat_recent=False,
        )
        assert sent_ok is True
        tl_after_proactive = await _wait_timeline_has(g1_shard_path, "定时提醒")
        assert not any("没能执行成功" in ln for ln in tl_after_proactive)
        assert any("定时提醒" in ln and "大家早上好" in ln and "定时任务" not in ln for ln in tl_after_proactive)

        sent_dm_ok = await emitter_mod.emit_proactive_message(
            event=ev_dm,
            message="私聊晚间提醒：记得按时休息呀。\n⏰ 定时任务: 晚间提醒",
            source="scheduled_task",
            trigger_reason="task_evening_dm",
            bot=Bot(_Bot("onebot"), ev_dm),
            persona_name="alpha",
            suppress_when_heartbeat_recent=False,
        )
        assert sent_dm_ok is True
        tl_dm_proactive = await _wait_timeline_has(u1_file, "定时提醒")
        assert any("定时提醒" in ln and "私聊晚间提醒" in ln for ln in tl_dm_proactive)
        assert not (journal_dir / "personas" / "alpha" / "self.md").exists()

        await jm.record_journal_entry(
            jstorage.JournalEntry(
                timestamp=now,
                date_str="2026-11-22",
                time_str="08:00",
                tag="查资料",
                summary="私聊核查个人体检日程安排",
                group_id="",
                requested_by="100000001",
            ),
            target_personas=["alpha"],
        )
        arch_dir = jstorage._archives_dir("alpha")
        (arch_dir / "legacy_v1_2026-10.jsonl").write_text('{"date":"2026-10-01"}\n', encoding="utf-8")
        (arch_dir / "noise.jsonl").write_text("{}", encoding="utf-8")
        structured_view = jm.get_journal_structured("alpha")
        assert any(gs["group_id"] == "group_1" and gs["timeline_count"] >= 1 for gs in structured_view["group_shards"])
        assert any(
            u["user_id"] == "100000001" and u["shard_id"] == "user:100000001" for u in structured_view["user_shards"]
        )
        assert len(structured_view["archive_months"]) >= 1
        assert "noise" not in structured_view["archive_months"]
        assert not structured_view["archive_months"][0].startswith("legacy_v1_")
        arch_recs, arch_months, arch_sel = jm.get_journal_archive_records("alpha", shard_id="group_1")
        assert arch_months == structured_view["archive_months"]
        assert arch_months and arch_sel and any(r["shard"] == "group_1" and r["entries"] for r in arch_recs)
        bad_recs, _, bad_sel = jm.get_journal_archive_records("alpha", month="../evil")
        assert bad_recs == [] and bad_sel == ""
        compat_md = jm.get_journal_content("alpha")
        compat_standing, compat_tl = jstorage._read_sections(compat_md)
        assert compat_standing and any("(群:group_1)" in ln for ln in compat_tl)

        # WebConsole 保存往返不会将占位文案固化进槽位，且单分片手动整理仅压缩指定群分片
        await jm.save_journal_content_async("alpha", _read_raw_text(g1_shard_path), shard_id="group_1")
        assert jstorage.parse_shard_markdown(_read_raw_text(g1_shard_path), "group_1").monthly == []
        assert await jd.compact_journal("alpha", force=True, target_shard="group_1", max_llm_shards=1) is True
        assert len(jstorage.parse_shard_markdown(_read_raw_text(g1_shard_path)).timeline) == 0

        await engine.dispose()

    asyncio.run(scenario())


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


def test_assign_target_to_persona_and_session_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from msgspec import json as msgjson

    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.http_agent.runtime import set_http_persona_override, clear_http_persona_override
    from gsuid_core.ai_core.persona.config_rules import read_channel_scopes, persona_covers_session

    _setup_isolated_persona_env(tmp_path, monkeypatch)

    # 1. 旧配置方案 A 加载前原位迁移（用真实 msgspec GSC 线格式写盘，覆盖 5 种旧 scope）
    legacy_cases = [
        ("mig_global", "global", ["g_keep"], "all", "all"),
        ("mig_gg", "global_group", [], "all", "off"),
        ("mig_gp", "global_private", [], "off", "all"),
        ("mig_spec", "specific", ["group_a", "group_b"], "specific", "off"),
        ("mig_off", "disabled", ["g_old"], "off", "off"),
    ]
    for pname, old_scope, old_groups, exp_g, exp_p in legacy_cases:
        pdir = tmp_path / pname
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "persona.md").write_text(f"# {pname}\n", encoding="utf-8")
        legacy_payload = msgjson.encode(
            {
                "scope": GsStrConfig("启用范围", "", old_scope),
                "target_groups": GsListStrConfig("目标群聊", "", old_groups),
            }
        )
        (pdir / "config.json").write_bytes(legacy_payload)
        cfg = persona_config_manager.get_config(pname)
        scopes = read_channel_scopes(cfg)
        assert scopes.group_scope == exp_g
        assert scopes.private_scope == exp_p
        assert cfg.get_config("target_groups").data == old_groups
        assert cfg.get_config("target_users").data == []

    # 清理迁移用例目录后测通道独立互斥与混合路由
    for pname, *_ in legacy_cases:
        persona_config_manager.delete_persona_config(pname)
        (tmp_path / pname / "persona.md").unlink()
        (tmp_path / pname).rmdir()
    persona_config_manager.clear_cache()

    # 2. 双通道独立互斥：全局私聊 + 指定群聊 与 全局群聊 可共存
    _seed_persona(tmp_path, "dm_and_part_group", "specific", ["group_1"], group_scope="specific", private_scope="all")
    _seed_persona(tmp_path, "all_group_p", "global_group")
    _seed_persona(tmp_path, "beta", "specific", [], group_scope="specific", private_scope="specific")
    _seed_persona(tmp_path, "off", "disabled")

    # 同通道全局互斥：再把 beta 设为全部私聊应被拒，但设为指定不影响
    ok_dup_dm, _ = persona_config_manager.set_channel_scopes("beta", private_scope="all")
    assert ok_dup_dm is False
    ok_dup_grp, _ = persona_config_manager.set_channel_scopes("beta", group_scope="all")
    assert ok_dup_grp is False

    group_1_sid = "ws1:onebot:10001:group:group_1"
    group_2_sid = "ws1:onebot:10001:group:group_2"
    dm_sid = "ws1:onebot:10001:private:100000001"
    http_sid = "HTTP_AGENT:onebot:s1:group:group_1"

    # group_1 命中指定群，group_2 回退全群兜底，私聊命中全私聊兜底
    assert persona_config_manager.get_persona_for_session(group_1_sid) == "dm_and_part_group"
    assert persona_config_manager.get_persona_for_session(group_2_sid) == "all_group_p"
    assert persona_config_manager.get_persona_for_session(dm_sid) == "dm_and_part_group"

    dm_cfg = persona_config_manager.get_config("dm_and_part_group")
    assert persona_covers_session(dm_cfg, group_id="group_1") is True
    assert persona_covers_session(dm_cfg, group_id="group_2") is False
    assert persona_covers_session(dm_cfg, user_id="100000001") is True

    # 群内切换目标到 beta
    ok, _ = persona_config_manager.assign_target_to_persona("beta", "group_1", is_private=False)
    assert ok is True
    persona_config_manager.clear_cache()
    assert "group_1" not in persona_config_manager.get_config("dm_and_part_group").get_config("target_groups").data
    assert "group_1" in persona_config_manager.get_config("beta").get_config("target_groups").data
    assert persona_config_manager.get_persona_for_session(group_1_sid) == "beta"

    # 切回全群兜底人格会移出 beta 的特定绑定
    ok, _ = persona_config_manager.assign_target_to_persona("all_group_p", "group_1", is_private=False)
    assert ok is True
    assert "group_1" not in persona_config_manager.get_config("beta").get_config("target_groups").data
    assert persona_config_manager.get_persona_for_session(group_1_sid) == "all_group_p"

    # 私聊切换绑定到 beta（走 target_users，且覆盖全局私聊）
    ok_dm_bind, _ = persona_config_manager.assign_target_to_persona("beta", "100000001", is_private=True)
    assert ok_dm_bind is True
    assert "100000001" in persona_config_manager.get_config("beta").get_config("target_users").data
    assert persona_config_manager.get_persona_for_session(dm_sid) == "beta"

    # 仅群聊人格拒绝私聊绑定；停用人格拒绝任何绑定
    ok_grp_only_dm, _ = persona_config_manager.assign_target_to_persona("all_group_p", "100000001", is_private=True)
    assert ok_grp_only_dm is False
    ok_off, _ = persona_config_manager.assign_target_to_persona("off", "group_1", is_private=False)
    assert ok_off is False

    # HTTP_AGENT 专属覆盖仅作用于 HTTP_AGENT 会话，不干扰普通会话
    set_http_persona_override(http_sid, "dm_and_part_group")
    try:
        assert persona_config_manager.get_persona_for_session(http_sid) == "dm_and_part_group"
        assert persona_config_manager.get_persona_for_session(group_1_sid) == "all_group_p"
        assert persona_config_manager.get_persona_for_session(dm_sid) == "beta"
    finally:
        clear_http_persona_override(http_sid)

    # 旧客户端回传有损派生 scope='specific' 不得抹除 private_scope='all'
    ok_idem, _ = persona_config_manager.set_scope("dm_and_part_group", "specific")
    assert ok_idem is True
    idem_scopes = read_channel_scopes(persona_config_manager.get_config("dm_and_part_group"))
    assert idem_scopes.group_scope == "specific"
    assert idem_scopes.private_scope == "all"

    # 原子抢占：仅关闭冲突人格的冲突通道，保留其另一通道
    ok_takeover, _ = persona_config_manager.set_channel_scopes(
        "beta",
        private_scope="all",
        takeover_conflicts=True,
    )
    assert ok_takeover is True
    after_prev = read_channel_scopes(persona_config_manager.get_config("dm_and_part_group"))
    assert after_prev.group_scope == "specific"
    assert after_prev.private_scope == "off"
    assert read_channel_scopes(persona_config_manager.get_config("beta")).private_scope == "all"

    # WebConsole 整单两阶段校验：其它字段不合法时既不得提前释放冲突人格，也不得半写入 target_groups
    from gsuid_core.webconsole.persona_api import update_persona_config

    prev_beta_groups = list(persona_config_manager.get_config("beta").get_config("target_groups").data)
    bad_resp = asyncio.run(
        update_persona_config(
            "beta",
            {
                "group_scope": "all",
                "takeover_conflicts": True,
                "target_groups": ["should_not_persist"],
                "inspect_interval": 999,
            },
            {},
        )
    )
    assert bad_resp["status"] == 1
    assert read_channel_scopes(persona_config_manager.get_config("all_group_p")).group_scope == "all"
    assert persona_config_manager.get_config("beta").get_config("target_groups").data == prev_beta_groups

    # journal 活跃人格解析走双通道真值源
    from gsuid_core.ai_core.journal.storage import get_active_personas

    active_journal_personas = get_active_personas()
    assert "beta" in active_journal_personas
    assert "all_group_p" in active_journal_personas
    assert "off" not in active_journal_personas

    # 整包覆盖导入已有非 git 人格：保留本地双通道范围与目标，且不触发旧配置伪迁移告警
    import io
    import json
    import zipfile

    from gsuid_core.ai_core.persona import config as persona_cfg_mod, persona as persona_cls_mod
    from gsuid_core.ai_core.persona.bundle import (
        BUNDLE_FORMAT,
        BUNDLE_VERSION,
        import_bundle_dir,
        export_persona_bundle,
    )
    from gsuid_core.ai_core.meme.pack_model import PackStore

    monkeypatch.setattr(persona_cls_mod, "PERSONA_PATH", tmp_path)

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
    migrated_calls: list[Path] = []
    orig_migrate = persona_cfg_mod._migrate_legacy_scope_file

    def _spy_migrate(path: Path) -> bool:
        did = orig_migrate(path)
        if did:
            migrated_calls.append(path)
        return did

    with monkeypatch.context() as mp:
        mp.setattr(persona_cfg_mod, "_migrate_legacy_scope_file", _spy_migrate)
        before_bundle = persona_config_manager.snapshot_channel_deployment("dm_and_part_group")
        res_bundle = asyncio.run(import_bundle_dir(bundle_dir))
        assert res_bundle.created is False
        after_bundle = persona_config_manager.snapshot_channel_deployment("dm_and_part_group")
        assert after_bundle == before_bundle

        # 新导入整包：强制关闭双通道并清空外部作者自带的 target_groups / target_users
        (bundle_dir / "manifest.json").write_text(
            json.dumps({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "persona_name": "fresh_bundle_p"}),
            encoding="utf-8",
        )
        res_fresh = asyncio.run(import_bundle_dir(bundle_dir))
        assert res_fresh.created is True
        fresh_snap = persona_config_manager.snapshot_channel_deployment("fresh_bundle_p")
        assert fresh_snap.group_scope == "off"
        assert fresh_snap.private_scope == "off"
        assert fresh_snap.target_groups == ()
        assert fresh_snap.target_users == ()
        assert migrated_calls == []

    # 整包导出脱敏：导出的 config.json 不得携带本地部署范围或真实群/私聊用户 ID
    exported_bytes = asyncio.run(export_persona_bundle("beta"))
    with zipfile.ZipFile(io.BytesIO(exported_bytes), "r") as zf:
        exported_cfg = json.loads(zf.read("config.json").decode("utf-8"))
    assert exported_cfg["group_scope"]["data"] == "off"
    assert exported_cfg["private_scope"]["data"] == "off"
    assert exported_cfg["scope"]["data"] == "disabled"
    assert exported_cfg["target_groups"]["data"] == []
    assert exported_cfg["target_users"]["data"] == []

    # 复制人格强制关闭双通道并清空目标列表
    from gsuid_core.ai_core.persona.resource import copy_persona

    monkeypatch.setattr("gsuid_core.ai_core.persona.resource.PERSONA_PATH", tmp_path, raising=False)
    monkeypatch.setattr("gsuid_core.ai_core.resource.PERSONA_PATH", tmp_path)
    copied_name = asyncio.run(copy_persona("beta"))
    copied_snap = persona_config_manager.snapshot_channel_deployment(copied_name)
    assert copied_snap.group_scope == "off"
    assert copied_snap.private_scope == "off"
    assert copied_snap.target_groups == ()
    assert copied_snap.target_users == ()

    # 驱动参数越界与非法枚举前置拦截，不得触发半写入或抢占
    bad_trait_resp = asyncio.run(
        update_persona_config(
            "beta",
            {
                "group_scope": "all",
                "takeover_conflicts": True,
                "social_initiative": 101,
            },
            {},
        )
    )
    assert bad_trait_resp["status"] == 1
    assert read_channel_scopes(persona_config_manager.get_config("all_group_p")).group_scope == "all"
    bad_focus_resp = asyncio.run(
        update_persona_config(
            "beta",
            {"curiosity_focus": "invalid_focus"},
            {},
        )
    )
    assert bad_focus_resp["status"] == 1

    # 冲突抢占若写入目标人格失败，必须回滚已释放的旧全局兜底
    orig_write_scopes = persona_cfg_mod._write_channel_scopes
    beta_cfg = persona_config_manager.get_config("beta")

    def _fail_target_write(cfg: StringConfig, gs: str, ps: str) -> bool:
        if cfg is beta_cfg:
            return False
        return orig_write_scopes(cfg, gs, ps)

    with monkeypatch.context() as mp:
        mp.setattr(persona_cfg_mod, "_write_channel_scopes", _fail_target_write)
        ok_rb, _ = persona_config_manager.set_channel_scopes("beta", group_scope="all", takeover_conflicts=True)
        assert ok_rb is False
    assert read_channel_scopes(persona_config_manager.get_config("all_group_p")).group_scope == "all"

    for child in bundle_dir.iterdir():
        child.unlink()
    bundle_dir.rmdir()

    # group_scope=None 时不得吞掉显式传入的兼容 scope 字段
    null_chan_resp = asyncio.run(
        update_persona_config(
            "fresh_bundle_p",
            {"group_scope": None, "private_scope": None, "scope": "specific"},
            {},
        )
    )
    assert null_chan_resp["status"] == 0
    assert read_channel_scopes(persona_config_manager.get_config("fresh_bundle_p")).group_scope == "specific"

    # 旧配置迁移写盘失败时必须抛错，严禁吞掉 OSError 后让 reconcile_config 静默重置为 off
    fail_mig_dir = tmp_path / "mig_io_err"
    fail_mig_dir.mkdir(parents=True, exist_ok=True)
    (fail_mig_dir / "persona.md").write_text("# mig_io_err\n", encoding="utf-8")
    fail_cfg_path = fail_mig_dir / "config.json"
    fail_cfg_path.write_bytes(msgjson.encode({"scope": GsStrConfig("启用范围", "", "global")}))

    def _boom_atomic_save(*_args: object, **_kwargs: object) -> object:
        raise OSError("disk locked")

    with monkeypatch.context() as mp:
        mp.setattr(persona_cfg_mod, "atomic_save", _boom_atomic_save)
        mp.setattr(persona_cfg_mod.time, "sleep", lambda _s: None)
        with pytest.raises(OSError, match="disk locked"):
            persona_cfg_mod._migrate_legacy_scope_file(fail_cfg_path)

    # 自主行动私聊范围约束：private_scope=specific 仅对 target_users 放行私聊推送
    from gsuid_core.ai_core.autonomy.models import ObservationDigest
    from gsuid_core.ai_core.autonomy.select import render_digest, assemble_care_targets

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
    assert by_uid["100000001"].private_scope_allowed is True
    assert by_uid["100000001"].allow_private_push is True
    assert by_uid["100000002"].private_scope_allowed is False
    assert by_uid["100000002"].allow_private_push is False
    digest_text = render_digest(
        ObservationDigest(
            persona_name="beta",
            bot_id="onebot",
            bot_self_id="10001",
            ws_bot_id="ws1",
            mood_score=0.5,
            curiosity_focus="balanced",
            cooled_topics=[],
            active_arc=None,
            arc_max_hops=3,
            care_targets=care,
            group_digests=[],
            available_plugins_summary=[],
            recently_used_tools=[],
            in_flight_tasks=[],
            recent_proactive_records=[],
        ),
        now=1000.0,
    )
    assert "私聊未开放" in digest_text


def test_corepersona_command_split_sv_and_dm_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.bot import Bot, _Bot
    from gsuid_core.models import Event
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.session_registry import get_ai_session_registry
    from gsuid_core.ai_core.proactive.dm_lease import grant_dm_lease, get_active_dm_lease
    from gsuid_core.buildin_plugins.core_command.core_ai_control import (
        switch_persona,
        sv_core_ai_control,
        sv_core_persona_control,
    )

    _setup_isolated_persona_env(tmp_path, monkeypatch)
    _seed_persona(tmp_path, "alpha", "global")
    _seed_persona(tmp_path, "beta", "specific", [], group_scope="specific", private_scope="specific")

    assert sv_core_persona_control is not sv_core_ai_control
    assert sv_core_persona_control.name != sv_core_ai_control.name
    assert sv_core_persona_control.pm == 0
    assert sv_core_ai_control.pm == 0

    sent: list[str] = []

    async def _fake_send(self: Bot, message: object, *args: object, **kwargs: object) -> None:
        sent.append(str(message))

    monkeypatch.setattr(Bot, "send", _fake_send)

    registry = get_ai_session_registry()
    group_sid = "ws1:onebot:10001:group:group_1"
    other_group_sid = "ws2:onebot:10002:group:group_1"
    dm_sid = "ws1:onebot:10001:private:100000001"
    dummy_model = OpenAIChatModel("gpt-test", provider=OpenAIProvider(api_key="test"))

    registry.set_ai_session(
        group_sid,
        GsCoreAIAgent(openai_chat_model=dummy_model, persona_name="alpha", session_id=group_sid),
    )
    registry.set_ai_session(
        other_group_sid,
        GsCoreAIAgent(openai_chat_model=dummy_model, persona_name="alpha", session_id=other_group_sid),
    )
    registry.set_ai_session(
        dm_sid,
        GsCoreAIAgent(openai_chat_model=dummy_model, persona_name="alpha", session_id=dm_sid),
    )

    grant_dm_lease(user_id="100000001", persona_name="alpha", bot_id="onebot")

    async def scenario() -> None:
        try:
            group_ev = Event(
                bot_id="onebot",
                bot_self_id="10001",
                user_type="group",
                group_id="group_1",
                user_id="100000001",
                text="beta",
                WS_BOT_ID="ws1",
            )
            await switch_persona(Bot(_Bot("ws1"), group_ev), group_ev)
            assert "beta" in sent[-1]
            assert persona_config_manager.get_persona_for_session(group_ev.session_id) == "beta"
            assert not registry.has_ai_session(group_sid)
            assert not registry.has_ai_session(other_group_sid)
            assert registry.has_ai_session(dm_sid)
            # 群聊切换绝不动用私聊 dm_lease
            assert get_active_dm_lease("100000001", "onebot") is not None

            dm_ev = Event(
                bot_id="onebot",
                bot_self_id="10001",
                user_type="direct",
                group_id=None,
                user_id="100000001",
                text="beta",
                WS_BOT_ID="ws1",
            )
            await switch_persona(Bot(_Bot("ws1"), dm_ev), dm_ev)
            assert get_active_dm_lease("100000001", "onebot") is None
            assert persona_config_manager.get_persona_for_session(dm_ev.session_id) == "beta"
            assert not registry.has_ai_session(dm_sid)

            # dm_lease 双通道守卫：仅群聊人格拒绝点名签发租约；已有租约在通道不再覆盖该用户时失效
            from gsuid_core.ai_core.proactive.dm_lease import maybe_switch_dm_lease

            _seed_persona(
                tmp_path, "group_only_p", "specific", ["group_1"], group_scope="specific", private_scope="off"
            )
            from gsuid_core.buildin_plugins.core_command.core_ai_control import _list_personas_by_status

            grp_enabled, grp_disabled = _list_personas_by_status(is_private=False)
            dm_enabled, dm_disabled = _list_personas_by_status(is_private=True)
            assert "group_only_p" in grp_enabled and "group_only_p" not in grp_disabled
            assert "group_only_p" in dm_disabled and "group_only_p" not in dm_enabled
            assert maybe_switch_dm_lease("100000001", "onebot", "group_only_p 在吗", "beta") is None
            assert maybe_switch_dm_lease("100000002", "onebot", "beta 在吗", "alpha") is None
            assert maybe_switch_dm_lease("100000001", "onebot", "alpha 在吗", "beta") == "alpha"
            ok_narrow, _ = persona_config_manager.set_channel_scopes("alpha", private_scope="specific")
            assert ok_narrow is True
            ok_users, _ = persona_config_manager.set_target_users("alpha", ["100000099"])
            assert ok_users is True
            # alpha 已切为指定私聊且不含 100000001，旧租约不得继续劫持该用户，且续期时主动清理
            from gsuid_core.ai_core.proactive.dm_lease import renew_dm_lease

            assert persona_config_manager.get_persona_for_session(dm_ev.session_id) == "beta"
            assert renew_dm_lease("100000001", "onebot") is False
            assert get_active_dm_lease("100000001", "onebot") is None
            ok_off_dm, _ = persona_config_manager.set_channel_scopes("alpha", private_scope="off")
            assert ok_off_dm is True
            assert persona_config_manager.get_persona_for_session(dm_ev.session_id) == "beta"
        finally:
            registry.remove_ai_session(group_sid)
            registry.remove_ai_session(other_group_sid)
            registry.remove_ai_session(dm_sid)

    asyncio.run(scenario())


def test_appearance_card_v3_and_self_visual_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """外观指纹卡：非自拍提问不下发 MISS 指纹卡、受信任立绘精确命中仍保留，且旧 v2 卡自动升级 v3。"""
    import io

    from PIL import Image

    from gsuid_core.ai_core.persona import appearance as app_mod
    from gsuid_core.ai_core.prompt_group.catalog import default_body

    assert "图中角色与此一致才是你" not in default_body("appearance_line")

    pdir = tmp_path / "personas" / "alpha"
    pdir.mkdir(parents=True)
    monkeypatch.setattr(app_mod, "PERSONA_PATH", tmp_path / "personas")
    app_mod._HASH_INDEX.clear()

    portrait_img = Image.new("RGB", (32, 32), color=(40, 120, 90))
    for x in range(16):
        for y in range(32):
            portrait_img.putpixel((x, y), (220, 180, 140))
    portrait_path = pdir / "image.png"
    portrait_img.save(portrait_path, format="PNG")
    (pdir / "persona.md").write_text("Name: 测试角色\n特征：深蓝风衣配银色徽章", encoding="utf-8")

    # 旧 v2 卡片应被判定需要重刷，刷新时携带 persona.md 设定消歧并升级至 v3 头
    out_card = pdir / "appearance.txt"
    out_card.write_text("# appearance-card v2\n摘要：旧版待刷新摘要", encoding="utf-8")
    assert app_mod._needs_vision_refresh(out_card, portrait_path) is True

    captured_prompts: list[str] = []

    async def fake_understand(_path: str, prompt: str | None = None, **_kw: object) -> str:
        captured_prompts.append(prompt or "")
        return "摘要：深蓝风衣配银色徽章的角色\n常服：深蓝风衣"

    monkeypatch.setattr("gsuid_core.ai_core.image_understand.understand.understand_image", fake_understand)
    line = asyncio.run(app_mod.refresh_appearance_card("alpha"))
    assert line == "深蓝风衣配银色徽章的角色"
    assert captured_prompts and "深蓝风衣配银色徽章" in captured_prompts[0]
    assert app_mod._needs_vision_refresh(out_card, portrait_path) is False

    # 普通无关表情包：allow_card_fallback=False 时绝不注入 MISS_SELF_PREFIX，显式追问时才注入
    other_buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(200, 20, 20)).save(other_buf, format="PNG")
    other_bytes = other_buf.getvalue()
    assert app_mod.format_look_identity_note("alpha", other_bytes, allow_card_fallback=False) == ""
    assert app_mod.MISS_SELF_PREFIX in app_mod.format_look_identity_note("alpha", other_bytes, allow_card_fallback=True)

    # 精确匹配受信任立绘本身时，即使未显式问是不是自己也返回 HIT_SELF_NOTE
    self_buf = io.BytesIO()
    portrait_img.save(self_buf, format="PNG")
    hit_note = app_mod.format_look_identity_note("alpha", self_buf.getvalue(), allow_card_fallback=False)
    assert hit_note == app_mod.HIT_SELF_NOTE


def test_proactive_speak_layer_and_autonomy_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """主动发言共享层、Autonomy 发信收口、沉默出口护栏与规划轮系统提示边界。"""
    import time

    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.models.test import TestModel

    from gsuid_core.models import Event
    from gsuid_core.ai_core import gs_agent as gs_agent_mod
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.utils.database import base_models as db_base
    from gsuid_core.ai_core.persona import persona as persona_cls_mod
    from gsuid_core.ai_core.autonomy import (
        tools as auto_tools,
        observer as auto_obs,
        scheduler as auto_sched,
    )
    from gsuid_core.ai_core.proactive import speak as pro_speak, emitter as pro_emitter
    from gsuid_core.ai_core.agent_errors import NO_RESULT_TEXT
    from gsuid_core.ai_core.autonomy.models import (
        GroupObservation,
        AutonomyStepTrace,
        ObservationDigest,
        CareTargetSnapshot,
    )
    from gsuid_core.ai_core.autonomy.runner import AUTONOMY_SYSTEM_GUIDE, AutonomyReport
    from gsuid_core.ai_core.database.models import AIAutonomyActivity
    from gsuid_core.ai_core.autonomy.delivery import extract_facts, output_is_failed, output_is_skipped
    from gsuid_core.ai_core.proactive.dispatcher import UnifiedProactiveDispatcher
    from gsuid_core.ai_core.prompt_group.catalog import spec_keys

    _setup_isolated_persona_env(tmp_path / "personas_cfg", monkeypatch)
    _seed_persona(tmp_path / "personas_cfg", "alpha", "specific", ["group_speak_1", "group_speak_2"])
    (tmp_path / "personas_cfg" / "alpha" / "persona.md").write_text(
        "Name: alpha\nStyle (风格): 轻快短句",
        encoding="utf-8",
    )
    monkeypatch.setattr(persona_cls_mod, "PERSONA_PATH", tmp_path / "personas_cfg")

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(db_base, "async_maker", maker)

        now = time.time()

        # 1. 共享系统提示 + autonomy_speak 注册 + 称呼边界
        assert "autonomy_speak" in spec_keys()
        assert "刚才看到你在说" not in AUTONOMY_SYSTEM_GUIDE
        assert "artifact_put" in AUTONOMY_SYSTEM_GUIDE
        speak_sys = pro_speak.build_proactive_speak_system_prompt("alpha", "Name: alpha", include_meme_protocol=True)
        assert "角色扮演" in speak_sys and "Name: alpha" in speak_sys and "框架通用底线" in speak_sys
        assert "<meme:" in speak_sys
        speak_sys_no_meme = pro_speak.build_proactive_speak_system_prompt(
            "alpha", "Name: alpha", include_meme_protocol=False
        )
        assert "<meme:" not in speak_sys_no_meme

        speak_user = pro_speak.build_autonomy_speak_user_prompt(
            draft="长篇测试指标数据 123.45% 词条列表",
            is_group=True,
            recent_context="测试用户A: 今天真热闹",
            master_title="主人",
            target_is_master=False,
            target_name="测试用户A",
            allow_meme=True,
            persona_name="alpha",
        )
        assert "严禁称呼「主人」" in speak_user and "禁止复读数据与词条" in speak_user

        speak_user_open = pro_speak.build_autonomy_speak_user_prompt(
            draft="群里有人在聊新玩具",
            is_group=True,
            recent_context="测试用户A: 新玩具挺好玩",
            master_title="主人",
            target_is_master=False,
            target_name="",
            persona_name="alpha",
        )
        assert "对方不是主人" not in speak_user_open and "只有回应主人本人发的消息时才称「主人」" in speak_user_open

        # 2. 私聊上下文在无/有私聊历史时均保留群触发话头与准确标签
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
        dm_guard = auto_tools.GuardState(
            persona_name="alpha",
            whitelist=frozenset({"100000001"}),
            sessions={},
            bot_id="onebot",
            bot_self_id="10001",
            ws_bot_id="ws1",
            allowed_groups={},
            allowed_users={"100000001": dm_snap},
            now=now,
        )
        dm_ev = Event(bot_id="onebot", bot_self_id="10001", user_id="100000001", user_pm=6, user_type="direct")
        dm_ctx_text, dm_ctx_header = auto_tools._recent_chat_context_for_send(dm_ev, dm_guard, None, "100000001")
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

        from gsuid_core.message_history import get_history_manager

        hm = get_history_manager()
        hm.add_message(dm_ev, "user", "前两天私聊说过的话", user_name="测试用户A")
        try:
            dm_both_text, dm_both_header = auto_tools._recent_chat_context_for_send(dm_ev, dm_guard, None, "100000001")
            assert dm_both_header == ""
            assert "[最近的私聊上下文]" in dm_both_text and "[对方近期在群里的发言]" in dm_both_text
            assert "群里没聊完的话题" in dm_both_text
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
        finally:
            hm._histories.pop(dm_ev, None)

        # 3. 近期发信防重摘要走短话题，不回灌长串数值
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
        pro_recs = await auto_obs._recent_proactive_records("alpha", now)
        assert any("测试话题甲" in item for item in pro_recs)
        assert all("99.9%" not in item for item in pro_recs)

        # 4. send_message_by_ai：silence 按目标锁、error 不计已发、MAX_SPEAK_ATTEMPTS 限流与成功回传日志
        emitted_calls: list[tuple[str, list[str]]] = []

        async def _fake_emit(
            ev: Event,
            message: str,
            *,
            source: str,
            trigger_reason: str,
            generator_log_files: list[str] | None = None,
            suppress_when_heartbeat_recent: bool = True,
            at_user_id: str | None = None,
            persona_name: str | None = None,
            source_group_id: str = "",
        ) -> bool:
            del ev, source, trigger_reason, suppress_when_heartbeat_recent, at_user_id, persona_name, source_group_id
            emitted_calls.append((message, list(generator_log_files or [])))
            return True

        compose_reply: list[pro_speak.ComposeOutcome] = [
            pro_speak.ComposeOutcome(status="silence", log_files=["autonomy_output_silent.json"]),
            pro_speak.ComposeOutcome(status="error", log_files=["autonomy_output_err.json"]),
            pro_speak.ComposeOutcome(status="ok", text="今天这话题挺好玩呀", log_files=["autonomy_output_ok.json"]),
        ]

        compose_contexts: list[tuple[bool, str, str]] = []

        async def _fake_compose(
            *,
            persona_name: str,
            event: Event,
            draft: str,
            recent_context: str,
            target_is_master: bool,
            target_name: str = "",
            target_zone_label: str = "",
            context_header: str = "",
        ) -> pro_speak.ComposeOutcome:
            del (
                persona_name,
                draft,
                target_is_master,
                target_name,
                target_zone_label,
            )
            compose_contexts.append((bool(event.group_id), recent_context, context_header))
            return compose_reply.pop(0)

        g_obs = GroupObservation(
            group_id="group_speak_1",
            summary="",
            recent_lines=["测试用户A: 测试项目提问"],
            last_human_ts=now - 300.0,
            bot_id="onebot",
            bot_self_id="10001",
            ws_bot_id="ws1",
            session_key="group:group_speak_1",
            last_assistant_ts=0.0,
        )
        g_obs2 = GroupObservation(
            group_id="group_speak_2",
            summary="",
            recent_lines=["测试用户B: 另一群话题"],
            last_human_ts=now - 300.0,
            bot_id="onebot",
            bot_self_id="10001",
            ws_bot_id="ws1",
            session_key="group:group_speak_2",
            last_assistant_ts=0.0,
        )
        guard = auto_tools.GuardState(
            persona_name="alpha",
            whitelist=frozenset(),
            sessions={},
            bot_id="onebot",
            bot_self_id="10001",
            ws_bot_id="ws1",
            allowed_groups={"group_speak_1": g_obs, "group_speak_2": g_obs2},
            allowed_users={},
            now=now,
        )
        auto_ctx = RunContext(deps=ToolContext(extra={}), model=TestModel(), usage=RunUsage())
        ab_dispatcher = UnifiedProactiveDispatcher()
        with monkeypatch.context() as mp:
            mp.setattr(auto_tools, "get_dispatcher", lambda: ab_dispatcher)
            mp.setattr(pro_emitter, "emit_proactive_message", _fake_emit)
            mp.setattr(pro_speak, "compose_autonomy_message", _fake_compose)
            tok = auto_tools.bind_guard(guard)
            try:
                silent_res = await auto_tools.send_message_by_ai(
                    auto_ctx,
                    text="刚才看到群里在聊测试数据 88.8%",
                    target_id="group_speak_1",
                    target_type="group",
                )
                assert silent_res.startswith("已跳过发送")
                assert not output_is_failed(silent_res) and output_is_skipped(silent_res)
                skipped_facts = extract_facts(
                    [
                        AutonomyStepTrace(
                            step_index=0,
                            tool_name="send_message_by_ai",
                            args_summary="{}",
                            output_text=silent_res,
                            is_error=output_is_failed(silent_res),
                        )
                    ]
                )
                assert not skipped_facts.has_substantive_outcome
                assert guard.sent_count == 0 and not emitted_calls

                same_target_res = await auto_tools.send_message_by_ai(
                    auto_ctx,
                    text="换个说法再试试",
                    target_id="group_speak_1",
                    target_type="group",
                )
                assert same_target_res.startswith("发送被拦截") and output_is_failed(same_target_res)
                assert len(compose_reply) == 2

                err_res = await auto_tools.send_message_by_ai(
                    auto_ctx,
                    text="刚才看到群里在聊测试数据 88.8%",
                    target_id="group_speak_2",
                    target_type="group",
                )
                assert err_res.startswith("发送失败") and output_is_failed(err_res)
                assert guard.sent_count == 0 and not emitted_calls

                capped_res = await auto_tools.send_message_by_ai(
                    auto_ctx,
                    text="刚才看到群里在聊测试数据 88.8%",
                    target_id="group_speak_2",
                    target_type="group",
                )
                assert capped_res.startswith("发送被拦截") and output_is_failed(capped_res)
                assert len(compose_reply) == 1

                guard.speak_attempts = 0
                sent_res = await auto_tools.send_message_by_ai(
                    auto_ctx,
                    text="刚才看到群里在聊测试数据 88.8%",
                    target_id="group_speak_2",
                    target_type="group",
                )
                assert sent_res.startswith("已成功向") and guard.sent_count == 1
                assert emitted_calls == [("今天这话题挺好玩呀", ["autonomy_output_ok.json"])]
            finally:
                auto_tools.reset_guard(tok)

        # 4b. 共享规划底片不承载分片手账原文；发声轮只在目标确定后做单目标物理隔离注入
        from gsuid_core.ai_core.journal import manager as j_mgr, storage as j_store
        from gsuid_core.ai_core.autonomy.select import render_digest

        monkeypatch.setattr(j_store, "JOURNAL_DIR", tmp_path / "autonomy_journals")
        j_store._SHARD_CACHE.clear()
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
            "alpha",
            j_store.ShardDocument(
                shard_id="user:100000002",
                yesterday=["- 测试用户B的私密健康记录"],
            ),
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
        grp_brief = j_mgr.read_shard_brief("alpha", group_id="group_speak_1", max_chars=80, include_persistent=False)
        assert "模块拆分讨论" in grp_brief
        rendered_digest = render_digest(
            ObservationDigest(
                persona_name="alpha",
                bot_id="onebot",
                bot_self_id="10001",
                ws_bot_id="ws1",
                mood_score=0.5,
                care_targets=[dm_snap],
                group_digests=[g_obs],
            ),
            now=now,
        )
        assert "周末整理书单" not in rendered_digest and "模块拆分讨论" not in rendered_digest
        assert "手账线索" not in rendered_digest

        assert "read_target_journal" not in auto_tools.AUTONOMY_TOOL_ALLOWLIST
        assert all(t_item.name != "read_target_journal" for t_item in auto_tools.build_autonomy_tools())
        ab_guard = auto_tools.GuardState(
            persona_name="alpha",
            whitelist=frozenset({"100000001"}),
            sessions={},
            bot_id="onebot",
            bot_self_id="10001",
            ws_bot_id="ws1",
            allowed_groups={"group_speak_1": g_obs},
            allowed_users={"100000001": dm_snap},
            now=now,
        )
        compose_contexts.clear()
        compose_reply.extend(
            [
                pro_speak.ComposeOutcome(status="ok", text="群里模块拆分推进得挺顺"),
                pro_speak.ComposeOutcome(status="ok", text="周末书单准备好了没"),
            ]
        )
        ab_dispatcher = UnifiedProactiveDispatcher()
        with monkeypatch.context() as mp:
            mp.setattr(auto_tools, "get_dispatcher", lambda: ab_dispatcher)
            mp.setattr(pro_emitter, "emit_proactive_message", _fake_emit)
            mp.setattr(pro_speak, "compose_autonomy_message", _fake_compose)
            tok_ab = auto_tools.bind_guard(ab_guard)
            try:
                # 群发声轮仅注入本群手账，绝不混入私聊手账；私聊发声轮仅注入本人手账，绝不混入群或他人手账
                await auto_tools.send_message_by_ai(
                    auto_ctx, text="跟进模块拆分", target_id="group_speak_1", target_type="group"
                )
                ab_guard.sent_count = 0
                ab_guard.speak_attempts = 0
                await auto_tools.send_message_by_ai(
                    auto_ctx, text="问问周末书单", target_id="100000001", target_type="user"
                )
            finally:
                auto_tools.reset_guard(tok_ab)
        assert len(compose_contexts) == 2
        grp_is_g, grp_speak_ctx, _ = compose_contexts[0]
        dm_is_g, dm_speak_ctx, _ = compose_contexts[1]
        assert grp_is_g is True and "[本群长期默契与近期手账]" in grp_speak_ctx and "模块拆分讨论" in grp_speak_ctx
        assert "周末整理书单" not in grp_speak_ctx and "私密健康记录" not in grp_speak_ctx
        assert (
            dm_is_g is False and "[你与对方的私聊默契与近期手账]" in dm_speak_ctx and "私聊喜欢直截了当" in dm_speak_ctx
        )
        assert "私密健康记录" not in dm_speak_ctx and "模块拆分讨论" not in dm_speak_ctx

        # 5. 共用执行收口 run_proactive_speak_agent：引号/沉默/错误/超时均关闭 logger 并保留日志路径
        assert pro_speak.strip_message_quotes("“外层中文引号”") == "外层中文引号"
        assert pro_speak.strip_message_quotes("“甲”和“乙”") == "“甲”和“乙”"

        disp_meme = UnifiedProactiveDispatcher()
        disp_meme.register_send("group_speak_1", "heartbeat", "早安呀\n<meme:happy>", persona_name="alpha")
        assert not disp_meme.would_repeat_heartbeat("group_speak_1", "去吃饭吗\n<meme:happy>", persona_name="alpha")

        class _StubLogger:
            def __init__(self, path: str) -> None:
                self._file_path = path
                self.closed = False
                self.run_ended = False
                self.errors: list[tuple[str, str]] = []

            def log_error(self, error_type: str, message: str) -> None:
                self.errors.append((error_type, message))

            def log_run_end(self) -> None:
                self.run_ended = True

            def close(self) -> None:
                self.closed = True

        class _StubSpeakAgent:
            def __init__(self, reply: str, path: str, *, raise_timeout: bool = False) -> None:
                self._reply = reply
                self._raise_timeout = raise_timeout
                self._session_logger = _StubLogger(path)

            def bind_budget_scope(self, ev: Event) -> None:
                del ev

            async def run(self, *, user_message: str, return_mode: str = "by_bot", budget_gate: bool = False) -> str:
                del user_message, budget_gate
                assert return_mode == "return"
                if self._raise_timeout:
                    raise asyncio.TimeoutError
                return self._reply

        stub_plan: list[tuple[str, bool]] = [
            ('"这句带引号也该干净发出去"', False),
            ("<SILENCE>", False),
            (NO_RESULT_TEXT, False),
            ("执行出错: Request timed out.", False),
            ("", True),
            ("<SILENCE>", False),
        ]
        created_sids: list[str] = []
        created_loggers: list[_StubLogger] = []

        def _fake_create_agent(
            system_prompt: str,
            *,
            create_by: str = "",
            task_kind: str = "",
            persona_name: str = "",
            session_id: str = "",
            is_subagent: bool = False,
        ) -> _StubSpeakAgent:
            del system_prompt, create_by, task_kind, persona_name, is_subagent
            created_sids.append(session_id)
            reply_text, should_timeout = stub_plan.pop(0)
            agent_stub = _StubSpeakAgent(reply_text, f"{session_id}.json", raise_timeout=should_timeout)
            created_loggers.append(agent_stub._session_logger)
            return agent_stub

        grp_ev = Event(
            bot_id="onebot",
            bot_self_id="10001",
            user_id="",
            user_pm=6,
            user_type="group",
            group_id="group_1",
        )
        with monkeypatch.context() as mp:
            mp.setattr(gs_agent_mod, "create_agent", _fake_create_agent)
            res_ok = await pro_speak.compose_autonomy_message(
                persona_name="alpha",
                event=grp_ev,
                draft="草稿",
                recent_context="测试用户A: 聊聊",
                target_is_master=False,
            )
            res_sil = await pro_speak.compose_autonomy_message(
                persona_name="alpha",
                event=grp_ev,
                draft="草稿",
                recent_context="测试用户A: 聊聊",
                target_is_master=False,
            )
            res_nores = await pro_speak.compose_autonomy_message(
                persona_name="alpha",
                event=grp_ev,
                draft="草稿",
                recent_context="测试用户A: 聊聊",
                target_is_master=False,
            )
            res_err = await pro_speak.compose_autonomy_message(
                persona_name="alpha",
                event=grp_ev,
                draft="草稿",
                recent_context="测试用户A: 聊聊",
                target_is_master=False,
            )
            res_timeout = await pro_speak.run_proactive_speak_agent(
                persona_name="alpha",
                event=grp_ev,
                system_prompt="sys",
                user_prompt="usr",
                create_by="Heartbeat_Output",
                session_id="hb_timeout_test",
            )
            res_hb_sil = await pro_speak.run_proactive_speak_agent(
                persona_name="alpha",
                event=grp_ev,
                system_prompt="sys",
                user_prompt="usr",
                create_by="Heartbeat_Output",
                session_id="hb_silence_test",
            )
        assert res_ok.status == "ok" and res_ok.text == "这句带引号也该干净发出去" and len(res_ok.log_files) == 1
        assert res_sil.status == "silence" and res_sil.text == ""
        assert res_nores.status == "error" and res_err.status == "error"
        assert res_timeout.status == "error" and res_timeout.log_files == ["hb_timeout_test.json"]
        assert created_loggers[4].run_ended and created_loggers[4].errors == [("timeout", "proactive speak timeout")]
        assert res_hb_sil.status == "silence" and res_hb_sil.log_files == ["hb_silence_test.json"]
        assert len(set(created_sids)) == 6 and all(lg.closed for lg in created_loggers)

        # 6. emit_proactive_message 出口兜底拦截空消息与 <SILENCE>
        assert (
            await pro_emitter.emit_proactive_message(
                grp_ev,
                "<SILENCE>",
                source="heartbeat",
                trigger_reason="测试沉默拦截",
            )
            is False
        )
        assert (
            await pro_emitter.emit_proactive_message(
                grp_ev,
                "   ",
                source="autonomy",
                trigger_reason="测试空消息拦截",
            )
            is False
        )

        # 7. Autonomy 规划轮系统提示不附 framework_core，避免与 6 行尾块契约冲突
        captured_planner_sys: list[str] = []

        async def _fake_build_obs(_persona_name: str) -> ObservationDigest:
            return ObservationDigest(
                persona_name="alpha",
                bot_id="onebot",
                bot_self_id="10001",
                ws_bot_id="ws1",
                mood_score=0.5,
                care_targets=[],
                group_digests=[g_obs],
            )

        async def _fake_run_autonomy(
            digest: ObservationDigest,
            *,
            system_prompt: str,
            user_prompt: str,
        ) -> AutonomyReport:
            del digest, user_prompt
            captured_planner_sys.append(system_prompt)
            return AutonomyReport(
                text="DRIVE: rest\nTARGET: -\nTOPIC: -\nOUTCOME: none\nFACT: 无\nTAKE: 先歇会儿。",
                steps=[],
                duration_ms=10,
                session_id="autonomy_planner_test",
            )

        async def _allow_run(_persona_name: str, *, force: bool) -> str:
            del _persona_name, force
            return ""

        sched = auto_sched.AutonomyScheduler()
        with monkeypatch.context() as mp:
            mp.setattr(sched, "_blocked", _allow_run)
            mp.setattr(auto_sched, "build_observation", _fake_build_obs)
            mp.setattr(auto_sched, "run_autonomy_agent", _fake_run_autonomy)
            await sched.run_persona("alpha", force=True)
        assert len(captured_planner_sys) == 1
        assert "框架通用底线" not in captured_planner_sys[0]
        assert "只输出 `<SILENCE>`" not in captured_planner_sys[0]
        assert "Name: alpha" in captured_planner_sys[0]

        await engine.dispose()

    asyncio.run(scenario())


def test_proactive_policy_unified_gates_and_persona_rate_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """统一主动门控：禁言拦截搭话但放行定时/看板，且 autonomy 发信按人格维度执行频次上限。"""
    import time

    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.models.test import TestModel

    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.autonomy import tools as auto_tools
    from gsuid_core.ai_core.autonomy.models import GroupObservation
    from gsuid_core.ai_core.proactive.policy import (
        ProactiveGateInput,
        evaluate_proactive_gate,
        check_target_rate_and_gap,
    )
    from gsuid_core.ai_core.proactive.dispatcher import UnifiedProactiveDispatcher

    # 1. 禁言策略：heartbeat / autonomy_send 被拦，scheduled_task / kanban 放行
    for ch in ("heartbeat", "autonomy_send"):
        res = evaluate_proactive_gate(
            ProactiveGateInput(
                channel=ch, persona_name="alpha", target_key="group_1", is_direct=False, scope_banned=True
            )
        )
        assert not res.allowed and res.code == "scope_banned"
    for ch in ("scheduled_task", "kanban"):
        res = evaluate_proactive_gate(
            ProactiveGateInput(
                channel=ch, persona_name="alpha", target_key="group_1", is_direct=False, scope_banned=True
            )
        )
        assert res.allowed

    # 2. 会话忙闲与私聊互惠退避（互惠退避仅约束私聊）
    busy_res = evaluate_proactive_gate(
        ProactiveGateInput(
            channel="autonomy_send", persona_name="alpha", target_key="group_1", is_direct=False, session_busy=True
        )
    )
    assert not busy_res.allowed and busy_res.code == "session_busy"
    dm_res = evaluate_proactive_gate(
        ProactiveGateInput(
            channel="heartbeat",
            persona_name="alpha",
            target_key="100000001",
            is_direct=True,
            dm_reciprocity_blocked=True,
        )
    )
    assert not dm_res.allowed and dm_res.code == "dm_reciprocity_backoff"
    grp_res = evaluate_proactive_gate(
        ProactiveGateInput(
            channel="heartbeat",
            persona_name="alpha",
            target_key="group_1",
            is_direct=False,
            dm_reciprocity_blocked=True,
        )
    )
    assert grp_res.allowed

    # 3. 回归验证：autonomy send_message_by_ai 必须按 persona_name 查频率配额（修复漏传 persona_name 漏洞）
    disp = UnifiedProactiveDispatcher()
    disp.register_send("group_1", "autonomy", "测试问候一", persona_name="alpha")
    disp.register_send("group_1", "autonomy", "测试问候二", persona_name="alpha")
    # 拨回 _last_send 时间绕过 C8 120s 间隔，纯测每小时配额
    disp._last_send["group_1"] = (time.time() - 600.0, "autonomy")
    _gap_a, rate_a = check_target_rate_and_gap(disp, "group_1", persona_name="alpha", max_per_hour=2, max_per_day=8)
    _gap_b, rate_b = check_target_rate_and_gap(disp, "group_1", persona_name="beta", max_per_hour=2, max_per_day=8)
    assert rate_a is True and rate_b is False

    now = time.time()
    g_obs = GroupObservation(
        group_id="group_1",
        summary="",
        recent_lines=[],
        last_human_ts=now - 300.0,
        bot_id="onebot",
        bot_self_id="10001",
        ws_bot_id="ws1",
        session_key="group:group_1",
    )
    guard = auto_tools.GuardState(
        persona_name="alpha",
        whitelist=frozenset(),
        sessions={},
        bot_id="onebot",
        bot_self_id="10001",
        ws_bot_id="ws1",
        allowed_groups={"group_1": g_obs},
        allowed_users={},
        now=now,
        hour_cap=2,
        day_cap=8,
    )
    ctx = RunContext(deps=ToolContext(extra={}), model=TestModel(), usage=RunUsage())
    tok = auto_tools.bind_guard(guard)
    try:
        with monkeypatch.context() as mp:
            mp.setattr(auto_tools, "get_dispatcher", lambda: disp)
            mp.setattr(auto_tools, "is_session_scope_banned", lambda _sid: False)
            mp.setattr(auto_tools, "is_session_busy", lambda _sid: False)
            msg = asyncio.run(auto_tools.send_message_by_ai(ctx, "顺口打个招呼", target_id="group_1"))
            assert "配额已达上限" in msg
    finally:
        auto_tools.reset_guard(tok)


def test_persona_reset_guard_and_archive_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import zipfile

    from gsuid_core.ai_core.persona_reset import guard, archive

    # 1. 冻结标记可重入 + self scope 按人格隔离 + 非 self 看共享标记
    guard.freeze(["alpha"], shared=False, scope_keys=["self:10001"])
    try:
        assert guard.is_persona_frozen("alpha") is True
        assert guard.is_persona_frozen("beta") is False
        assert guard.is_scope_frozen("self:10001:alpha") is True
        assert guard.is_scope_frozen("self:10001:beta") is False
        assert guard.is_scope_frozen("self:10001") is True
        assert guard.is_scope_frozen("group:group_1") is False

        guard.freeze(["alpha"], shared=True)
        assert guard.is_scope_frozen("group:group_1") is True
        assert guard.is_scope_frozen("user_global:100000001") is True
        guard.unfreeze(["alpha"], shared=True)
        assert guard.is_persona_frozen("alpha") is True
        assert guard.is_scope_frozen("group:group_1") is False
    finally:
        guard.unfreeze(["alpha"], shared=False, scope_keys=["self:10001"])
    assert not guard.is_persona_frozen("alpha")
    assert not guard.is_scope_frozen("self:10001")
    assert not guard.is_scope_frozen("group:group_1")

    # 2. zip-slip 越界条目拒绝 + 损坏 zip 安全返回 None + 仅保留最近 5 份
    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../escape.txt", "boom")
    with pytest.raises(ValueError, match="unsafe zip entry"):
        archive.extract_sync(evil_zip)

    bad_zip = tmp_path / "corrupt.zip"
    bad_zip.write_bytes(b"not-a-zip")
    assert archive.read_manifest_sync(bad_zip) is None

    monkeypatch.setattr(archive, "BACKUP_ROOT", tmp_path / "backups")
    out_dir = tmp_path / "backups" / "personas" / "alpha"
    out_dir.mkdir(parents=True)
    for idx in range(7):
        (out_dir / f"20261125_00000{idx}.zip").write_bytes(b"x")
    archive._prune_sync(out_dir, archive.KEEP_BACKUPS)
    kept = sorted(p.name for p in out_dir.glob("*.zip"))
    assert len(kept) == 5 and kept[0] == "20261125_000002.zip"


def test_persona_reset_backup_restore_and_vector_resume_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端覆盖：预览 → 重置（含备份）→ 跨人格隔离 → 还原（含认知节点与边）→ 向量断点续跑。"""
    import time
    from datetime import datetime, timezone, timedelta

    from sqlmodel import select
    from sqlalchemy import delete as sql_delete

    from gsuid_core.models import Event
    from gsuid_core.ai_core.rag import base as rag_base
    from gsuid_core.ai_core.memory import startup as mem_startup
    from gsuid_core.ai_core.journal import storage as jstorage
    from gsuid_core.ai_core.persona import mood as mood_mod
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.autonomy import tools as auto_tools
    from gsuid_core.ai_core.state_store import state_get_value, state_set_value
    from gsuid_core.ai_core.memory.vector import ops as vec_ops
    from gsuid_core.ai_core.persona_reset import jobs, guard, archive, service
    from gsuid_core.ai_core.scheduled_task import executor as sched_exec
    from gsuid_core.ai_core.cognition.nodes import AICogEdge, AICogNode, CogEdgeKind, AICogAttachment
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.database.models import UserFavorability, AIAutonomyActivity
    from gsuid_core.ai_core.memory.database import fts as fts_mod, models as mem_models
    from gsuid_core.ai_core.memory.observer import ObservationRecord
    from gsuid_core.ai_core.memory.ingestion import algebra, window_router
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
    from gsuid_core.ai_core.memory.ingestion.worker import ingest_window
    from gsuid_core.ai_core.memory.database.preference import AIMemPreference

    _setup_isolated_persona_env(tmp_path / "personas", monkeypatch)
    _seed_persona(tmp_path / "personas", "alpha", "specific", ["group_1"], group_scope="specific", private_scope="all")
    _seed_persona(tmp_path / "personas", "beta", "specific", ["group_2"], group_scope="specific", private_scope="off")

    journal_root = tmp_path / "journals"
    notes_root = tmp_path / "notes"
    backup_root = tmp_path / "persona_backups"
    monkeypatch.setattr(jstorage, "JOURNAL_DIR", journal_root)
    monkeypatch.setattr(auto_tools, "artifact_dir", lambda name, root=None: notes_root / name)
    monkeypatch.setattr(archive, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(archive, "JOBS_DIR", backup_root / "_jobs")
    jstorage._SHARD_CACHE.clear()
    jstorage._SHARD_UPDATE_TS.clear()
    jstorage._RECENT_ENTRY_TS.clear()
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

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

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

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reset_e2e.db'}", poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(base_models, "async_maker", maker)
        monkeypatch.setattr(mem_models, "async_maker", maker)
        monkeypatch.setattr(base_models, "_db_type", "sqlite")
        monkeypatch.setattr(base_models, "engine", engine)

        now_dt = datetime.now(timezone.utc)
        # 1. 灌入 alpha、beta 与共享测试数据（全脱敏虚拟数据）
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
                id="pref-u1",
                scope_key="user_global:100000001",
                user_id="100000001",
                preference_rule="回答简洁",
            )
            s.add_all([ent_a, ent_b, ent_g1, ent_g2, ent_u1, th_g, ep_a, ep_b, ep_g, edge_g, pref_u])
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
            assert hub.id is not None and env_node.id is not None and self_note.id is not None
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

        # 手账、笔记、状态、心情、会话
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
        await mood_mod.update_mood("alpha", "group_1", "praise")

        ev_g1 = Event(bot_id="onebot", bot_self_id="10001", user_id="100000001", group_id="group_1", user_type="group")
        get_history_manager().add_message(ev_g1, "user", "测试消息")

        # 2. 预览校验
        prev = await service.preview_reset("alpha")
        assert prev["other_personas"] == ["beta"]
        by_key = {item["key"]: item for item in prev["items"]}
        assert by_key["self_memory"]["default_checked"] is True and by_key["self_memory"]["count"] == 3
        assert by_key["global_group"]["default_checked"] is False and by_key["global_group"]["count"] == 6
        assert by_key["global_user"]["default_checked"] is False and by_key["global_user"]["count"] == 2
        assert by_key["scheduled_tasks"]["default_checked"] is False and by_key["scheduled_tasks"]["count"] == 1
        assert by_key["session_history"]["backup_supported"] is False and by_key["session_history"]["count"] >= 1

        # 错误确认名被拒
        bad = await service.start_reset("alpha", ["journal"], "wrong", False, False)
        assert bad["error"]

        # 全局图谱项未二次确认被拒
        no_global = await service.start_reset("alpha", ["global_group"], "alpha", False, False)
        assert no_global["error"]

        # 摄入 flush 等不到空闲 → 任务失败、手账未删、人格解冻（异常经 done-callback 收口）
        class _StuckWorker:
            async def wait_frozen_flush_idle(self, timeout: float) -> bool:
                del timeout
                return False

            def drop_frozen_buffers(self) -> int:
                return 0

        orig_worker_getter = mem_startup.get_ingestion_worker
        monkeypatch.setattr(mem_startup, "get_ingestion_worker", lambda: _StuckWorker())
        try:
            stuck = await service.start_reset("alpha", ["journal"], "alpha", False, False)
            assert not stuck["error"] and stuck["job_id"]
            stuck_view = await _wait_job(stuck["job_id"])
            assert stuck_view["status"] == "failed"
            assert j_alpha.exists()
            assert guard.is_persona_frozen("alpha") is False
        finally:
            monkeypatch.setattr(mem_startup, "get_ingestion_worker", orig_worker_getter)

        # 冻结人格的一次性定时任务不点火：顺延补跑一次后返回
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

        # 3. 执行全量重置（含备份）
        started = await service.start_reset("alpha", list(ALL_ITEMS), "alpha", True, True)
        assert not started["error"] and started["job_id"]
        done_view = await _wait_job(started["job_id"])
        assert done_view["status"] == "done", done_view
        assert done_view["backup_file"].endswith(".zip")
        backup_file = done_view["backup_file"]

        # 备份人格不匹配：拷进 beta 备份目录后预览还原直接拒绝
        import shutil

        beta_backup_dir = archive.persona_backup_dir("beta")
        beta_backup_dir.mkdir(parents=True, exist_ok=True)
        copied = beta_backup_dir / backup_file
        shutil.copy2(archive.persona_backup_dir("alpha") / backup_file, copied)
        try:
            assert await service.preview_restore("beta", backup_file) == "备份不属于该人格"
        finally:
            copied.unlink(missing_ok=True)

        # 校验清空结果 & beta / 公共枢纽完好无损
        assert not j_alpha.exists() and j_beta.exists()
        assert not (notes_root / "alpha").exists()
        assert jstorage.read_journal_state_sync("alpha") is None
        assert await state_get_value(persona_scope("alpha"), "homeostasis") is None
        assert await state_get_value(persona_scope("beta"), "homeostasis") == {"energy": 0.9}
        assert await state_get_value("__gscore_group_profile__", "group:group_1") is None
        assert mood_mod.count_mood_states("alpha") == 0
        assert fake_qdrant.deleted_collections

        async with maker() as s:
            eps = (await s.execute(select(AIMemEpisode))).scalars().all()
            assert [e.id for e in eps] == ["ep-b1"]
            favs = (await s.execute(select(UserFavorability))).scalars().all()
            assert [(f.persona_name, f.favorability) for f in favs] == [("beta", 88)]
            acts = (await s.execute(select(AIAutonomyActivity))).scalars().all()
            assert [a.persona_name for a in acts] == ["beta"]
            cogs = (await s.execute(select(AICogNode))).scalars().all()
            assert [c.ref for c in cogs] == ["world:test_hub"]
            task_row = (await s.execute(select(AIScheduledTask))).scalars().first()
            assert task_row is not None and task_row.status == "cancelled"

        # 4. 还原预览（验证吞吐缓存，重复打开不重复调嵌入）+ 还原（模拟向量中途失败 → 断点续跑）
        rprev = await service.preview_restore("alpha", backup_file)
        assert not isinstance(rprev, str)
        assert rprev["overwrite_counts"]["scheduled_tasks"] == 0
        assert rprev["vector_texts"] == 11 and rprev["throughput_per_sec"] > 0
        rprev_cached = await service.preview_restore("alpha", backup_file)
        assert not isinstance(rprev_cached, str) and embed_calls["n"] == 1

        # 重置后再写入一条新记录，验证覆盖还原会先清掉它
        await UserFavorability.restore_rows(
            [UserFavorability(user_id="100000002", bot_id="onebot", persona_name="alpha", favorability=5)]
        )

        rstart = await service.start_restore("alpha", backup_file, list(rprev["items"]), "alpha")
        assert not rstart["error"] and rstart["job_id"]
        interrupted = await _wait_job(rstart["job_id"])
        assert interrupted["status"] == "resumable", interrupted
        vec_step = [st for st in interrupted["steps"] if st["key"] == "vectors"][0]
        assert vec_step["status"] == "failed"

        # SQL / 认知节点 / 边 / 挂件 / 手账 / 好感 / 状态 / 定时任务均已立即恢复
        assert j_alpha.read_text(encoding="utf-8") == "# alpha 手账\n"
        assert (notes_root / "alpha" / "n1.txt").read_text(encoding="utf-8") == "alpha 笔记"
        assert jstorage.read_journal_state_sync("alpha") is not None
        assert await state_get_value(persona_scope("alpha"), "homeostasis") == {"energy": 0.5}
        assert await state_get_value("__gscore_group_profile__", "group:group_1") == {"tags": ["测试"]}
        assert "task-alpha-1" in registered_jobs

        async with maker() as s:
            eps = sorted(e.id for e in (await s.execute(select(AIMemEpisode))).scalars().all())
            assert eps == ["ep-a1", "ep-b1", "ep-g1"]
            favs = sorted(
                (f.persona_name, f.user_id, f.favorability)
                for f in (await s.execute(select(UserFavorability))).scalars().all()
            )
            assert favs == [("alpha", "100000001", 42), ("beta", "100000001", 88)]
            cogs = sorted(c.ref for c in (await s.execute(select(AICogNode))).scalars().all())
            assert cogs == ["env:ent-g2", "self:10001:note:1", "world:test_hub"]
            edges_cog = (await s.execute(select(AICogEdge))).scalars().all()
            atts_cog = (await s.execute(select(AICogAttachment))).scalars().all()
            assert len(edges_cog) == 1 and len(atts_cog) == 1
            task_row = (await s.execute(select(AIScheduledTask))).scalars().first()
            assert task_row is not None and task_row.status == "pending"

        # 模拟进程重启：清掉内存任务表，仍能从磁盘游标发现 resumable 并续跑完成
        jobs._jobs.clear()
        jobs._active_job_id = ""
        active = await service.active_job_view("alpha")
        assert active is not None and active["status"] == "resumable" and active["job_id"] == rstart["job_id"]

        resumed = await service.resume_job(rstart["job_id"])
        assert not resumed["error"]
        final_view = await _wait_job(resumed["job_id"])
        assert final_view["status"] == "done", final_view
        assert await service.active_job_view("alpha") is None
        assert sorted(upserted["episode"]) == ["ep-a1", "ep-g1"]
        assert sorted(upserted["entity"]) == ["ent-a1", "ent-g1", "ent-g2", "ent-u1"]
        assert sorted(upserted["edge"]) == ["edge-g1"]

        # 摄入循环在 extract 前复查冻结：首个 cluster 冻结后，第二个 cluster 不再落库
        extract_calls = {"n": 0}

        async def _two_extract_route(
            scope_key: str,
            records: list[ObservationRecord],
            threads: list[AIMemThread],
            *,
            force_settle: bool = False,
        ) -> window_router.WindowRouteOutput:
            del scope_key, records, threads, force_settle
            return {
                "clusters": [
                    {"lines": [0], "action": "extract", "thread_ref": None, "topic_title": ""},
                    {"lines": [1], "action": "extract", "thread_ref": None, "topic_title": ""},
                ],
                "open_lines": [],
            }

        async def _freeze_on_extract(
            scope_key: str,
            records: list[ObservationRecord],
            lines: list[int],
            *,
            thread_ref: str | None,
            topic_title: str,
            episode_id: str,
            threads: list[AIMemThread] | None = None,
        ) -> algebra.AlgebraStats:
            del records, lines, thread_ref, topic_title, episode_id, threads
            extract_calls["n"] += 1
            guard.freeze(["alpha"], shared=False)
            assert guard.is_scope_frozen(scope_key)
            return {"entities": 0, "edges": 0, "preferences": 0, "thread_id": ""}

        monkeypatch.setattr(window_router, "route_window", _two_extract_route)
        monkeypatch.setattr(algebra, "extract_cluster", _freeze_on_extract)

        async def _noop_upsert_ep(**kwargs: object) -> None:
            del kwargs

        monkeypatch.setattr(mem_models, "upsert_episode_vector", _noop_upsert_ep)
        recs = [
            ObservationRecord(
                raw_content=f"窗口消息{idx}",
                speaker_id="100000001",
                group_id="group_1",
                scope_key="self:10001:alpha",
                timestamp=now_dt,
                message_type="text",
            )
            for idx in (1, 2)
        ]
        try:
            w_stats = await ingest_window(recs, "self:10001:alpha")
        finally:
            guard.unfreeze(["alpha"], shared=False)
        assert extract_calls["n"] == 1
        assert w_stats["episodes"] == 1

        # 5. FastAPI HTTP 契约端到端验证（路由无遮蔽 + 预览 / 备份列表 / 下载 / 删除 / 任务进度）
        import httpx

        from gsuid_core.webconsole import persona_api as _persona_api  # noqa: F401
        from gsuid_core.webconsole.app_app import app as web_app
        from gsuid_core.webconsole.web_api import require_admin
        from gsuid_core.webconsole.session_store import SessionRecord

        admin_rec: SessionRecord = {
            "user": {"id": "admin", "email": "admin@example.com", "name": "admin", "role": "admin", "avatar": None},
            "email": "admin@example.com",
            "created": now_dt.isoformat(),
            "expires": (now_dt + timedelta(hours=1)).isoformat(),
        }
        web_app.dependency_overrides[require_admin] = lambda: admin_rec
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
                assert resp_list.status_code == 200
                listed = resp_list.json()["data"]
                assert len(listed) == 1 and listed[0]["file"] == backup_file

                resp_dl = await http.get(f"/api/persona/alpha/backups/{backup_file}/download")
                assert resp_dl.status_code == 200 and resp_dl.content[:2] == b"PK"

                resp_del = await http.delete(f"/api/persona/alpha/backups/{backup_file}")
                assert resp_del.status_code == 200 and resp_del.json()["status"] == 0
                assert await service.list_backups("alpha") == []
        finally:
            web_app.dependency_overrides.pop(require_admin, None)

        # 6. 回归防线：prepare 阶段异常不锁死全局任务，且新一轮记忆重置会清理旧 resumable 游标
        orig_scope_map = service._scope_map

        async def _boom_scope_map(
            persona: str, items: list[service.ResetItemKey]
        ) -> tuple[dict[service.ResetItemKey, list[str]], list[str]]:
            del persona, items
            raise RuntimeError("simulated db error before freeze")

        monkeypatch.setattr(service, "_scope_map", _boom_scope_map)
        try:
            fail_start = await service.start_reset("alpha", ["self_memory"], "alpha", False, False)
            assert not fail_start["error"] and fail_start["job_id"]
            fail_view = await _wait_job(fail_start["job_id"])
            assert fail_view["status"] == "failed" and jobs.active_job() is None
        finally:
            monkeypatch.setattr(service, "_scope_map", orig_scope_map)

        stale_cursor = archive.JOBS_DIR / "stale_alpha_job.json"
        service._save_cursor_sync(
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
        )
        assert stale_cursor.exists()
        fresh_start = await service.start_reset("alpha", ["self_memory"], "alpha", False, False)
        assert not fresh_start["error"] and fresh_start["job_id"]
        fresh_view = await _wait_job(fresh_start["job_id"])
        assert fresh_view["status"] == "done" and not stale_cursor.exists()

        # 清理内存会话，避免污染其他用例
        get_history_manager().delete_session(ev_g1)
        async with maker() as s:
            await s.execute(sql_delete(AIMemEdge))
            await s.commit()
        await engine.dispose()

    asyncio.run(scenario())
