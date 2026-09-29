"""Issue #282 回归：备份快照带 WAL 数据 / dashboard 等待不永久挂起 / TEMP_DICT 有界。

对应 issue https://github.com/Genshin-bots/gsuid_core/issues/282
"""

from __future__ import annotations

import asyncio
import sqlite3
import zipfile
import threading
from pathlib import Path
from datetime import date, timedelta

import pytest
from starlette.requests import Request

from gsuid_core.i18n import t
from gsuid_core.webconsole import dashboard_api
from gsuid_core.webconsole.web_api import TEMP_DICT, TEMP_DICT_MAX_ENTRIES, DailyCountCache, set_temp_dict
from gsuid_core.utils.backup.backup_core import copy_and_rebase_paths
from gsuid_core.utils.database.base_models import sqlite_consistent_snapshot
from gsuid_core.utils.database.global_val_models import DataType

# ── 1. 备份必须带上 WAL 里尚未 checkpoint 的写入 ──────────────────────


def _make_wal_db(path: Path) -> sqlite3.Connection:
    """建一个 WAL 库并写入后保持连接，让数据留在 -wal 里不被 checkpoint。"""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    return conn


def test_snapshot_includes_uncommitted_to_main_file(tmp_path: Path) -> None:
    """在线备份必须能读出仅存在于 WAL 的数据（普通 copy2 会读成 no such table）。"""
    live = tmp_path / "live.db"
    conn = _make_wal_db(live)
    try:
        wal = tmp_path / "live.db-wal"
        assert wal.exists() and wal.stat().st_size > 0, "前置条件：数据应在 WAL 中"

        dest = tmp_path / "snap.db"
        sqlite_consistent_snapshot(live, dest)

        rows = sqlite3.connect(str(dest)).execute("SELECT x FROM t").fetchall()
        assert rows == [(1,)]
    finally:
        conn.close()


def test_naive_copy_would_lose_wal_data(tmp_path: Path) -> None:
    """反证：普通 copy2 确实读不到 WAL 数据（证明上一条不是空断言）。"""
    import shutil

    live = tmp_path / "live.db"
    conn = _make_wal_db(live)
    try:
        naive = tmp_path / "naive.db"
        shutil.copy2(live, naive)
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            sqlite3.connect(str(naive)).execute("SELECT x FROM t").fetchall()
    finally:
        conn.close()


def test_snapshot_overwrites_stale_destination(tmp_path: Path) -> None:
    """目标已存在时必须先删再写，避免把半截/旧库当有效备份。"""
    live = tmp_path / "live.db"
    conn = _make_wal_db(live)
    try:
        dest = tmp_path / "snap.db"
        dest.write_bytes(b"stale garbage")
        sqlite_consistent_snapshot(live, dest)

        assert sqlite3.connect(str(dest)).execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        conn.close()


# ── 2. dashboard 等待必须有上限，写入方缺席时不许永久挂起 ──────────────


class _Stat:
    def __init__(self, data_type: DataType, command_name: str, command_count: int, target_id: str) -> None:
        self.data_type = data_type
        self.command_name = command_name
        self.command_count = command_count
        self.target_id = target_id


_ROWS = (
    _Stat(DataType.GROUP, "ping", 3, "g1"),
    _Stat(DataType.USER, "ping", 2, "u1"),
)


class _BackupBoom:
    """只拦截源库的 backup()。sqlite3.Connection 是不可变类型，方法替换不了。"""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def execute(self, sql: str) -> sqlite3.Cursor:
        return self._inner.execute(sql)

    def backup(self, target: sqlite3.Connection) -> None:
        del target
        raise sqlite3.OperationalError("locked")

    def close(self) -> None:
        self._inner.close()


def test_snapshot_failure_removes_partial_files(tmp_path: Path, monkeypatch) -> None:
    """backup() 失败时不能留下空库或 -wal/-shm，否则会被当成有效备份。"""
    live = tmp_path / "live.db"
    conn = _make_wal_db(live)
    try:
        dest = tmp_path / "snap.db"
        Path(str(dest) + "-wal").write_bytes(b"wal")
        Path(str(dest) + "-shm").write_bytes(b"shm")
        real_connect = sqlite3.connect

        def connect(path: str, timeout: float = 5.0) -> sqlite3.Connection | _BackupBoom:
            opened = real_connect(path, timeout=timeout)
            if Path(path).resolve() == live.resolve():
                return _BackupBoom(opened)
            return opened

        monkeypatch.setattr("gsuid_core.utils.database.base_models.sqlite3.connect", connect)
        with pytest.raises(sqlite3.OperationalError):
            sqlite_consistent_snapshot(live, dest)
        assert not dest.exists()
        assert not Path(str(dest) + "-wal").exists()
        assert not Path(str(dest) + "-shm").exists()
    finally:
        conn.close()


def test_snapshot_drops_sidecars_and_refuses_live_db(tmp_path: Path) -> None:
    """成功的快照是独立主库；不能覆盖源库本身，也不能留下边车。"""
    live = tmp_path / "live.db"
    conn = _make_wal_db(live)
    try:
        with pytest.raises(RuntimeError):
            sqlite_consistent_snapshot(live, live)
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]

        dest = tmp_path / "snap.db"
        Path(str(dest) + "-wal").write_bytes(b"wal")
        Path(str(dest) + "-shm").write_bytes(b"shm")
        sqlite_consistent_snapshot(live, dest)
        # 再打开会按库头里的 WAL 模式重建边车；打包发生在打开之前。
        assert not Path(str(dest) + "-wal").exists()
        assert not Path(str(dest) + "-shm").exists()
        checked = sqlite3.connect(str(dest))
        try:
            assert checked.execute("SELECT x FROM t").fetchall() == [(1,)]
        finally:
            checked.close()
    finally:
        conn.close()


def test_overlapping_same_target_does_not_both_succeed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一目录的第二次备份必须马上失败，不能把对方正在写的 zip 也报成成功。"""
    import shutil

    root = tmp_path / "data"
    root.mkdir()
    src = root / "note.txt"
    src.write_text("payload", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.gs_data_path", root)
    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.backup_path", backups)

    started = threading.Event()
    release = threading.Event()
    real_copy = shutil.copy2

    def slow_copy(src_path: str, dest_path: str) -> str:
        started.set()
        assert release.wait(5)
        return real_copy(src_path, dest_path)

    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.shutil.copy2", slow_copy)
    results: list[int] = []

    def run() -> None:
        results.append(copy_and_rebase_paths([src], "same"))

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert started.wait(5)
    second.start()
    second.join(1)
    assert not second.is_alive()
    release.set()
    first.join(5)
    assert not first.is_alive()
    assert results == [-7, 0]
    zips = list(backups.glob("same-*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as archive:
        assert archive.read("note.txt") == b"payload"


def test_copy_error_is_not_reported_as_success(tmp_path: Path, monkeypatch) -> None:
    """快照失败时备份必须返回非 0、不打 zip，也不留下半截目录。"""
    root = tmp_path / "data"
    root.mkdir()
    db = root / "GsData.db"
    db.write_bytes(b"x")
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.gs_data_path", root)
    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.backup_path", backups)
    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.is_live_sqlite", lambda _path: True)

    def boom(_src: Path, _dest: Path) -> None:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr("gsuid_core.utils.backup.backup_core.sqlite_consistent_snapshot", boom)
    assert copy_and_rebase_paths([db], "case") != 0
    assert list(backups.glob("*.zip")) == []
    # 半截目录会让保留期清理和磁盘统计失真
    assert [p for p in backups.iterdir() if p.is_dir()] == []


def test_failed_round_does_not_poison_the_next_read(monkeypatch) -> None:
    """上一轮查库失败不能让下一次并发的群/个人统计直接读到空图。"""

    async def boom(*_args: object, **_kwargs: object) -> list[_Stat]:
        raise RuntimeError("db down")

    async def scenario() -> None:
        monkeypatch.setattr(dashboard_api.CoreDataAnalysis, "get_sp_data", boom)
        failed = await dashboard_api.get_daily_commands(_make_request(), "2026-09-29", "all", {"uid": 1})
        assert failed["status"] == 1
        assert "None/None/2026-09-29" not in TEMP_DICT

        release = asyncio.Event()
        entered = asyncio.Event()

        async def block(*_args: object, **_kwargs: object) -> tuple[_Stat, ...]:
            entered.set()
            await release.wait()
            return _ROWS

        monkeypatch.setattr(dashboard_api.CoreDataAnalysis, "get_sp_data", block)
        cmd_task = asyncio.create_task(
            dashboard_api.get_daily_commands(_make_request(), "2026-09-29", "all", {"uid": 1})
        )
        await entered.wait()
        grp_task = asyncio.create_task(
            dashboard_api.get_daily_group_triggers(_make_request(), "2026-09-29", "all", {"uid": 1})
        )
        per_task = asyncio.create_task(
            dashboard_api.get_daily_personal_triggers(_make_request(), "2026-09-29", "all", {"uid": 1})
        )
        done, _pending = await asyncio.wait({grp_task, per_task}, timeout=0.1)
        assert not done
        release.set()
        cmd_res, grp_res, per_res = await asyncio.gather(cmd_task, grp_task, per_task)
        assert cmd_res["status"] == 0
        assert grp_res["status"] == 0
        assert per_res["status"] == 0
        assert any(item["group"] == "g1" for item in grp_res["data"])
        assert any(item["user"] == "u1" for item in per_res["data"])

    asyncio.run(scenario())


def test_daily_group_timeout_is_an_error(monkeypatch) -> None:
    """没有 commands 写入时必须超时失败，不能把空列表当成当天没有命令。"""
    monkeypatch.setattr(dashboard_api, "_DAILY_CACHE_MAX_WAIT_S", 0.0)
    result = asyncio.run(dashboard_api.get_daily_group_triggers(_make_request(), "2026-09-29", "all", {"uid": 1}))
    assert result["status"] == 1
    assert result["data"] == []


@pytest.mark.parametrize("endpoint_name", ["get_daily_group_triggers", "get_daily_personal_triggers"])
def test_trigger_endpoint_error_is_not_reported_as_success(endpoint_name: str) -> None:
    """端点自身出错也必须 status=1；返回 status 0 的空图等于把 bug 伪装成「当天没数据」。"""
    endpoint = getattr(dashboard_api, endpoint_name)

    async def scenario() -> None:
        # 非法日期在 datetime.fromisoformat 处抛出，走 except 分支
        result = await endpoint(_make_request(), "not-a-date", "all", {"uid": 1})
        assert result["status"] == 1
        assert result["data"] == []
        assert result["msg"] != "ok"

    asyncio.run(scenario())


# ── 3. TEMP_DICT / 轮次元数据都不能无界增长 ────────────────────────────


def test_temp_dict_is_bounded() -> None:
    total = TEMP_DICT_MAX_ENTRIES + 20
    for i in range(total):
        set_temp_dict(f"bounded/{i}", {"c_data": {}, "g_data": {}, "u_data": {}})
    assert len(TEMP_DICT) <= TEMP_DICT_MAX_ENTRIES


def test_temp_dict_refresh_does_not_consume_slot() -> None:
    key = "stable/key"
    set_temp_dict(key, {"c_data": {"a": 1}, "g_data": {}, "u_data": {}})
    before = len(TEMP_DICT)
    set_temp_dict(key, {"c_data": {"a": 2}, "g_data": {}, "u_data": {}})
    assert len(TEMP_DICT) == before
    assert TEMP_DICT[key]["c_data"] == {"a": 2}


def test_daily_round_state_is_bounded() -> None:
    """轮次三件套服务同一批 key，必须跟着 TEMP_DICT 一起封顶，否则按天累积。"""

    async def scenario() -> None:
        total = dashboard_api._DAILY_STATE_MAX_ENTRIES + 20
        for i in range(total):
            key = f"None/None/2026-01-{i:02d}"
            version = await dashboard_api.begin_daily_round(key)
            await dashboard_api.publish_daily_round(key, version, {"c_data": {}, "g_data": {}, "u_data": {}})

        assert len(dashboard_api._daily_version) <= dashboard_api._DAILY_STATE_MAX_ENTRIES
        assert len(dashboard_api._daily_settled) <= dashboard_api._DAILY_STATE_MAX_ENTRIES
        assert len(dashboard_api._daily_failed) <= dashboard_api._DAILY_STATE_MAX_ENTRIES
        # 被淘汰的 key 不该再残留在 settled/failed 里
        assert len(dashboard_api._daily_settled) == len(dashboard_api._daily_version)

    asyncio.run(scenario())


def test_evicted_day_drops_chart_and_kept_day_still_serves() -> None:
    """淘汰一天时图表必须跟着标记走；还在缓存里的另一天仍能画出群统计。"""

    async def scenario() -> None:
        base = date(2020, 1, 1)
        cap = dashboard_api._DAILY_STATE_MAX_ENTRIES
        chart: DailyCountCache = {
            "c_data": {"ping": 4},
            "g_data": {"g1": {"ping": 4}},
            "u_data": {},
        }
        for offset in range(cap):
            key = f"None/None/{(base + timedelta(days=offset)).isoformat()}"
            version = await dashboard_api.begin_daily_round(key)
            await dashboard_api.publish_daily_round(key, version, chart)
        doomed = f"None/None/{base.isoformat()}"
        kept_day = (base + timedelta(days=1)).isoformat()
        kept = f"None/None/{kept_day}"
        extra = f"None/None/{(base + timedelta(days=cap)).isoformat()}"
        version = await dashboard_api.begin_daily_round(extra)
        assert doomed not in TEMP_DICT
        assert doomed not in dashboard_api._daily_settled
        assert kept in TEMP_DICT
        await dashboard_api.publish_daily_round(extra, version, None)
        assert doomed not in TEMP_DICT

        served = await dashboard_api.get_daily_group_triggers(_make_request(), kept_day, "all", {"uid": 1})
        assert served["status"] == 0
        assert any(item["group"] == "g1" and item["ping"] == 4 for item in served["data"])

    asyncio.run(scenario())


def test_chart_is_served_when_round_metadata_was_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """标记被清掉但图表还在时，群统计必须把图交出去，不能干等成超时。"""
    monkeypatch.setattr(dashboard_api, "_DAILY_CACHE_MAX_WAIT_S", 0.0)

    async def scenario() -> None:
        day = "2024-03-01"
        key = f"None/None/{day}"
        version = await dashboard_api.begin_daily_round(key)
        await dashboard_api.publish_daily_round(
            key,
            version,
            {"c_data": {"ping": 4}, "g_data": {"g1": {"ping": 4}}, "u_data": {}},
        )
        dashboard_api._daily_version.pop(key, None)
        dashboard_api._daily_settled.discard(key)
        result = await dashboard_api.get_daily_group_triggers(_make_request(), day, "all", {"uid": 1})
        assert result["status"] == 0
        assert any(item["group"] == "g1" for item in result["data"])

    asyncio.run(scenario())


def test_missing_payload_is_not_reported_as_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """图表被挤掉、标记还在时，不能把缓存未命中说成统计失败。"""
    monkeypatch.setattr(dashboard_api, "_DAILY_CACHE_MAX_WAIT_S", 0.0)

    async def scenario() -> None:
        day = "2024-04-01"
        key = f"None/None/{day}"
        version = await dashboard_api.begin_daily_round(key)
        await dashboard_api.publish_daily_round(
            key,
            version,
            {"c_data": {"ping": 1}, "g_data": {"g1": {"ping": 1}}, "u_data": {}},
        )
        TEMP_DICT.pop(key, None)
        result = await dashboard_api.get_daily_group_triggers(_make_request(), day, "all", {"uid": 1})
        assert result["status"] == 1
        assert result["data"] == []
        assert result["msg"] == t("msg.webconsole.daily_stats_timeout")

    asyncio.run(scenario())


def test_evicted_round_key_forgets_its_failure() -> None:
    """淘汰键必须连带清掉失败标记，否则 _daily_failed 单独无界增长。"""

    async def scenario() -> None:
        first = "None/None/1999-01-01"
        version = await dashboard_api.begin_daily_round(first)
        await dashboard_api.publish_daily_round(first, version, None)
        assert first in dashboard_api._daily_failed

        for i in range(dashboard_api._DAILY_STATE_MAX_ENTRIES + 5):
            key = f"None/None/2000-01-{i:02d}"
            v = await dashboard_api.begin_daily_round(key)
            await dashboard_api.publish_daily_round(key, v, None)

        assert first not in dashboard_api._daily_version
        assert first not in dashboard_api._daily_failed
        assert first not in dashboard_api._daily_settled

    asyncio.run(scenario())


# ── 4. 面向用户的失败文案必须走 msg.webconsole.* ───────────────────────


def test_daily_failure_messages_are_localized() -> None:
    """响应 msg 走 msg.* 词条，英文环境不能回落到中文硬编码。"""
    blocked = dashboard_api._daily_blocked_response("k", "failed")
    assert blocked["msg"] == t("msg.webconsole.daily_stats_failed")
    assert dashboard_api._daily_failed_response()["msg"] == t("msg.webconsole.daily_stats_failed")

    timed_out = dashboard_api._daily_blocked_response("k", "timeout")
    assert timed_out["msg"] == t("msg.webconsole.daily_stats_timeout")

    for lang in ("zh-cn", "en", "ja"):
        assert t("msg.webconsole.daily_stats_failed", lang=lang)
        assert t("msg.webconsole.daily_stats_timeout", lang=lang)
    # 英文词条必须是英文，不能是漏翻的中文
    assert t("msg.webconsole.daily_stats_failed", lang="en") == "Failed to compute daily stats"


def _make_request() -> Request:
    """构造真实 Request：端点签名要求 starlette Request，替身会破坏类型链（AGENTS.md §1.8）。"""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/dashboard/daily/commands",
            "headers": [],
            "query_string": b"",
        }
    )


@pytest.fixture(autouse=True)
def _clean_temp_dict():
    TEMP_DICT.clear()
    dashboard_api._daily_cond = None
    dashboard_api._daily_version.clear()
    dashboard_api._daily_settled.clear()
    dashboard_api._daily_failed.clear()
    yield
    TEMP_DICT.clear()
    dashboard_api._daily_cond = None
    dashboard_api._daily_version.clear()
    dashboard_api._daily_settled.clear()
    dashboard_api._daily_failed.clear()
