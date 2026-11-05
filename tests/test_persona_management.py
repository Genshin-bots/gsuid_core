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


def _make_persona(root: Path, name: str) -> Path:
    p = root / name
    p.mkdir(parents=True)
    (p / "persona.md").write_text(f"# {name}\n", encoding="utf-8")
    return p


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


def _engine(tmp_path: Path, name: str = "test.db"):
    return create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )


def _patch_db(monkeypatch: pytest.MonkeyPatch, engine) -> async_sessionmaker[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(base_models, "async_maker", maker)
    monkeypatch.setattr(base_models, "engine", engine)
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr("gsuid_core.ai_core.state_store.store._table_ensured", True)
    return maker


def test_source_marker_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    assert persona_store.read_source("sayu") is None
    assert not persona_store.is_git_managed("sayu")

    persona_store.write_source("sayu", _make_source())
    src = persona_store.read_source("sayu")
    assert src is not None
    assert src["repo_name"] == "persona_a"
    assert src["packs"] == ["测试人格"]
    assert persona_store.is_git_managed("sayu")

    (tmp_path / "sayu" / persona_store.SOURCE_MARKER).write_text("{bad", encoding="utf-8")
    assert persona_store.read_source("sayu") is None


def test_drop_source_detaches_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    persona_store.write_source("sayu", _make_source())
    persona_store.drop_source("sayu")
    assert not persona_store.is_git_managed("sayu")
    persona_store.drop_source("sayu")


def test_find_bundle_root_root_and_subdir(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    assert persona_store.find_bundle_root(tmp_path) == tmp_path

    sub = tmp_path / "persona_x"
    sub.mkdir()
    (sub / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "manifest.json").unlink()
    assert persona_store.find_bundle_root(tmp_path) == sub

    sub.joinpath("manifest.json").unlink()
    with pytest.raises(BundleError):
        persona_store.find_bundle_root(tmp_path)


def test_managed_block_rejects_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_persona_path(tmp_path, monkeypatch)
    _make_persona(tmp_path, "sayu")
    from gsuid_core.webconsole.persona_api import _managed_block

    assert _managed_block("sayu") is None
    persona_store.write_source("sayu", _make_source())
    blocked = _managed_block("sayu")
    assert blocked is not None and blocked["status"] == 1


def test_relationship_scored_by_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        from gsuid_core.ai_core.relationship import fetch_relationship
        from gsuid_core.ai_core.database.models import UserFavorability

        engine = _engine(tmp_path)
        maker = _patch_db(monkeypatch, engine)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            async with maker() as session:
                session.add(UserFavorability(user_id="u1", bot_id="b1", persona_name="A", favorability=85))
                await session.commit()

            view_a = await fetch_relationship("u1", "b1", "A")
            assert view_a.scored and view_a.score == 85
            view_b = await fetch_relationship("u1", "b1", "B")
            assert not view_b.scored
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_self_note_isolated_by_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        from gsuid_core.ai_core.self_cognition import add_self_note, build_self_cognition_context

        async def _noop_distill(**kwargs: object) -> None:
            return None

        monkeypatch.setattr("gsuid_core.ai_core.cognition.distill.distill_self_note", _noop_distill)
        engine = _engine(tmp_path)
        _patch_db(monkeypatch, engine)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            assert await add_self_note("b1", "A", "我对用户承诺过每天汇报", "self_notes")

            own = await build_self_cognition_context("b1", "A", include_relationship=False)
            assert "每天汇报" in own
            other = await build_self_cognition_context("b1", "B", include_relationship=False)
            assert "每天汇报" not in other
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_aside_advice_isolated_by_persona(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.command_aside.recall import save_advice, lookup_advice

    target = tmp_path / "aside.json"
    monkeypatch.setattr("gsuid_core.ai_core.command_aside.recall._path", lambda: target)

    save_advice(
        plugin="p",
        trigger_id="t1",
        receipt="命令 foo 执行成功",
        advice="用中文解释结果",
        persona_name="A",
    )
    assert lookup_advice("p", "命令 foo 执行成功", "t1", "A") == "用中文解释结果"
    assert lookup_advice("p", "命令 foo 执行成功", "t1", "B") == ""


def test_pick_default_persona_priority(tmp_path: Path) -> None:
    from gsuid_core.ai_core.migrations.persona_scope_v1 import pick_default_persona

    configs: Dict[str, StringConfig] = {
        "sp1": _persona_cfg(tmp_path, "sp1", "specific", ["g1"]),
        "gg": _persona_cfg(tmp_path, "gg", "global_group"),
        "gp": _persona_cfg(tmp_path, "gp", "global_private"),
        "gl": _persona_cfg(tmp_path, "gl", "global"),
        "off": _persona_cfg(tmp_path, "off", "disabled"),
    }
    assert pick_default_persona(configs) == "gl"

    no_global = {k: v for k, v in configs.items() if k != "gl"}
    assert pick_default_persona(no_global) == "gg"

    no_gg = {k: v for k, v in no_global.items() if k != "gg"}
    assert pick_default_persona(no_gg) == "gp"

    specific_only = {
        "za": _persona_cfg(tmp_path, "za", "specific", ["g1"]),
        "zb": _persona_cfg(tmp_path, "zb", "specific", ["g1", "g2"]),
        "zc": _persona_cfg(tmp_path, "zc", "specific", ["g1", "g2", "g3"]),
        "off": _persona_cfg(tmp_path, "off", "disabled"),
    }
    assert pick_default_persona(specific_only) == "zc"
