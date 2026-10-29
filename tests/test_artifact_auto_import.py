"""测试受信任外部产物自动拷贝登记与工作区沙盒防护。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic_ai import RunContext
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import gsuid_core.utils.database.base_models as base_models
from gsuid_core.models import Event
from gsuid_core.data_store import gs_data_path
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.planning.runtime import (
    PlanRunContext,
    bind_plan_context,
    reset_plan_context,
)
from gsuid_core.ai_core.planning.workspace import (
    put_artifact,
    _can_auto_import_file,
)
from gsuid_core.ai_core.planning.kanban_tools import artifact_put


def _install_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'artifacts.db').as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    monkeypatch.setattr(
        base_models,
        "async_maker",
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
    )
    monkeypatch.setattr(base_models, "sqlite_read_semaphore", None)
    monkeypatch.setattr(base_models, "_db_type", "sqlite")


def test_can_auto_import_file_rules(tmp_path: Path) -> None:
    """验证可安全自动导入工作区的路径判定。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    plan_ctx = PlanRunContext(
        root_task_id="root_1",
        task_id="task_1",
        artifact_workspace=ws,
        allowed_write_roots=[tmp_path / "allowed"],
    )
    (tmp_path / "allowed").mkdir()

    # 1. 允许显式授权根目录
    allowed_file = tmp_path / "allowed" / "out.png"
    allowed_file.write_bytes(b"data")
    assert _can_auto_import_file(allowed_file, plan_ctx) is True

    # 2. 允许系统临时目录
    temp_file = Path(tempfile.gettempdir()) / "temp_render.png"
    temp_file.write_bytes(b"temp")
    assert _can_auto_import_file(temp_file, plan_ctx) is True

    # 3. 允许 data 下的插件输出目录
    plugin_shot = gs_data_path / "GsBrowserUse" / "shots" / "test_shot.png"
    plugin_shot.parent.mkdir(parents=True, exist_ok=True)
    plugin_shot.write_bytes(b"png")
    assert _can_auto_import_file(plugin_shot, plan_ctx) is True

    # 4. 禁止敏感后缀
    blocked_key = gs_data_path / "GsBrowserUse" / "shots" / "secret.key"
    blocked_key.write_bytes(b"secret")
    assert _can_auto_import_file(blocked_key, plan_ctx) is False

    # 5. 禁止 data 根目录下的直接配置/数据库文件
    config_file = gs_data_path / "test_config.json"
    config_file.write_bytes(b"{}")
    assert _can_auto_import_file(config_file, plan_ctx) is False

    # 6. 禁止敏感数据目录（日志/会话记录等）
    log_file = gs_data_path / "logs" / "2026.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_bytes(b"log")
    assert _can_auto_import_file(log_file, plan_ctx) is False


@pytest.mark.asyncio
async def test_put_artifact_auto_copies_trusted_external_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证外部受信任产物在 put_artifact 时自动拷贝至工作区。"""
    _install_db(tmp_path, monkeypatch)
    from sqlmodel import SQLModel

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'artifacts.db').as_posix()}",
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    ws = tmp_path / "task_ws"
    ws.mkdir()
    plan_ctx = PlanRunContext(
        root_task_id="root_test",
        task_id="task_test",
        agent_profile="browser_agent",
        artifact_workspace=ws,
    )

    # 模拟外部插件在 data 插件目录下生成的截图
    external_shot = gs_data_path / "GsBrowserUse" / "shots" / "mock_landing.png"
    await asyncio.to_thread(external_shot.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(external_shot.write_bytes, b"PNG_CONTENT_12345")

    art = await put_artifact(
        summary="落地页截图",
        mime="image/png",
        plan_ctx=plan_ctx,
        file_path=external_shot,
    )
    assert art is not None
    assert art.mime == "image/png"
    # 文件已自动拷贝进工作区，路径指向工作区内部
    dest_path = Path(art.payload_path or "")
    assert dest_path.parent.resolve() == ws.resolve()
    assert dest_path.name == "mock_landing.png"
    content = await asyncio.to_thread(dest_path.read_bytes)
    assert content == b"PNG_CONTENT_12345"


@pytest.mark.asyncio
async def test_artifact_put_tool_distinguishes_missing_vs_untrusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 artifact_put 工具在文件不存在 vs 越界未受信任时的返回差异。"""
    _install_db(tmp_path, monkeypatch)
    from sqlmodel import SQLModel

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'artifacts.db').as_posix()}",
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    ws = tmp_path / "kanban_ws"
    ws.mkdir()
    plan_ctx = PlanRunContext(
        root_task_id="root_tool_test",
        task_id="task_tool_test",
        artifact_workspace=ws,
    )
    token = bind_plan_context(plan_ctx)
    try:
        ev = Event(user_id="test_user_42", user_pm=0)
        tool_ctx = ToolContext(ev=ev)
        run_ctx = MagicMock(spec=RunContext)
        run_ctx.deps = tool_ctx

        # 1. 真实不存在的文件
        missing = ws / "non_existent.png"
        res_missing = await artifact_put(run_ctx, file_path=str(missing))
        assert "找不到文件" in res_missing

        # 2. 真实存在但不可信的越界敏感文件
        untrusted = gs_data_path / "forbidden_root.json"
        await asyncio.to_thread(untrusted.write_bytes, b"{}")
        res_untrusted = await artifact_put(run_ctx, file_path=str(untrusted))
        assert "超出允许的沙盒工作区" in res_untrusted
    finally:
        reset_plan_context(token)
