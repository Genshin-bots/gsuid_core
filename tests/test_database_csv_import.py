"""CSV import reads export cells, merges by primary key, and replaces only after a full file checks out."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlmodel import Field, SQLModel, col, select
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from gsuid_core.utils.database import csv_import
from gsuid_core.utils.database.admin_api import (
    CSV_BOM,
    ColumnInfo,
    DatabaseTableInfo,
    csv_cell,
    encode_csv_rows,
)
from gsuid_core.utils.database.csv_import import (
    CSV_IMPORT_MAX_BYTES,
    ImportMode,
    ImportField,
    CsvImportError,
    decode_csv_bytes,
    import_table_csv,
    import_error_text,
    prepare_import_rows,
)


class CsvImportProbe(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    score: int | None = None
    active: bool = False
    note: str | None = None


def _fields() -> list[ImportField]:
    return [
        ImportField("id", "int", False, True, True, True),
        ImportField("name", "str", False, False, False, False),
        ImportField("score", "int", True, False, False, False),
        ImportField("active", "bool", False, False, True, False),
        ImportField("note", "str", True, False, False, False),
    ]


def _info() -> DatabaseTableInfo:
    columns = [
        ColumnInfo("id", "id", "int", True, None),
        ColumnInfo("name", "name", "str", False, None),
        ColumnInfo("score", "score", "int", True, None),
        ColumnInfo("active", "active", "bool", False, False),
        ColumnInfo("note", "note", "str", True, None),
    ]
    return DatabaseTableInfo("CsvImportProbe", "probe", columns, CsvImportProbe)


def _csv_bytes(rows: list[list[str]]) -> bytes:
    return CSV_BOM + encode_csv_rows(rows)


def test_prepare_round_trips_export_cells() -> None:
    fields = [
        ImportField("name", "str", False, False, False, False),
        ImportField("score", "int", True, False, False, False),
        ImportField("active", "bool", False, False, True, False),
        ImportField("payload", "json", True, False, False, False),
    ]
    payload = {"k": "v", "n": 1}
    raw = _csv_bytes(
        [
            ["name", "score", "active", "payload"],
            [
                csv_cell("=cmd", "str"),
                csv_cell(-1, "int"),
                csv_cell(False, "bool"),
                csv_cell(payload, "json"),
            ],
            [csv_cell("+cmd", "str"), "", csv_cell(True, "bool"), ""],
        ]
    )
    rows = prepare_import_rows(fields, decode_csv_bytes(raw), "merge")
    assert rows[0].values["name"] == "=cmd"
    assert rows[0].values["score"] == -1
    assert rows[0].values["active"] is False
    assert rows[0].values["payload"] == {"k": "v", "n": 1}
    assert rows[1].values["name"] == "+cmd"
    assert rows[1].values["score"] is None
    assert rows[1].values["active"] is True
    assert rows[1].values["payload"] is None


def test_prepare_parses_integral_decimal_and_skips_blank_lines() -> None:
    text = "id,name,score\n1,a,-1.0,\n\n,,,\n"
    rows = prepare_import_rows(_fields(), text, "merge")
    assert len(rows) == 1
    assert rows[0].pk == (1,)
    assert rows[0].values["score"] == -1
    assert rows[0].values["name"] == "a"


def test_prepare_rejects_bad_files() -> None:
    fields = _fields()
    cases: list[tuple[str, ImportMode, str]] = [
        ("id,name,extra\n1,a,b\n", "merge", "unknown_columns"),
        ("id,id\n1,1\n", "merge", "duplicate_header"),
        ("id,name\n1,a\n1,b\n", "merge", "duplicate_pk"),
        ("id,name\nx,a\n", "merge", "bad_cell"),
        ("id,name\n1,a,zzz\n", "merge", "extra_cells"),
        ("id,name\n", "replace", "replace_empty"),
        ("id,code,name\n,abc,a\n", "merge", "partial_pk"),
        ("id,code,name\n1,,a\n", "merge", "missing_pk"),
    ]
    pk_fields = [
        ImportField("id", "int", False, True, True, True),
        ImportField("code", "str", False, True, False, False),
        ImportField("name", "str", False, False, False, False),
    ]
    for text, mode, suffix in cases:
        header = text.split("\n", 1)[0]
        use = pk_fields if "code" in header else fields
        with pytest.raises(CsvImportError) as caught:
            prepare_import_rows(use, text, mode)
        assert caught.value.key.endswith(suffix)


def test_decode_csv_bytes_accepts_bom_utf16_and_gb18030() -> None:
    assert decode_csv_bytes("名字".encode("gb18030")) == "名字"
    assert decode_csv_bytes("id,name\n".encode("utf-16")).startswith("id,name")
    assert decode_csv_bytes(CSV_BOM + b"id,name\n").startswith("id")
    with pytest.raises(CsvImportError) as caught:
        decode_csv_bytes(b"id\x00")
    assert caught.value.key.endswith("bad_encoding")
    with pytest.raises(CsvImportError) as caught:
        decode_csv_bytes(b" \n\t")
    assert caught.value.key.endswith("empty_file")


def test_import_error_text_names_row_and_column() -> None:
    from gsuid_core.i18n import load_catalogs

    load_catalogs()
    text = import_error_text(CsvImportError("msg.webconsole.database_import.bad_cell", row=4, column="score"))
    assert "4" in text
    assert "score" in text


def test_import_table_csv_rejects_before_write(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise AssertionError("database opened")

    monkeypatch.setattr(csv_import, "async_maker", _boom)
    monkeypatch.setattr(csv_import, "get_table_info", lambda _name: _info())

    async def _run() -> None:
        with pytest.raises(CsvImportError) as bad_mode:
            await import_table_csv("CsvImportProbe", b"id,name\n1,a\n", "nope", "CsvImportProbe")
        assert bad_mode.value.key.endswith("bad_mode")

        with pytest.raises(CsvImportError) as mismatch:
            await import_table_csv("CsvImportProbe", b"id,name\n1,a\n", "merge", "other")
        assert mismatch.value.key.endswith("confirm_mismatch")

        monkeypatch.setattr(csv_import, "CSV_IMPORT_MAX_BYTES", 4)
        with pytest.raises(CsvImportError) as huge:
            await import_table_csv("CsvImportProbe", b"12345", "merge", "CsvImportProbe")
        assert huge.value.key.endswith("file_too_large")
        monkeypatch.setattr(csv_import, "CSV_IMPORT_MAX_BYTES", CSV_IMPORT_MAX_BYTES)

        with pytest.raises(CsvImportError) as empty_replace:
            await import_table_csv("CsvImportProbe", b"id,name\n", "replace", "CsvImportProbe")
        assert empty_replace.value.key.endswith("replace_empty")

        monkeypatch.setattr(csv_import, "get_table_info", lambda _name: None)
        with pytest.raises(CsvImportError) as missing:
            await import_table_csv("Missing", b"id,name\n1,a\n", "merge", "Missing")
        assert missing.value.key.endswith("table_not_found")

        monkeypatch.setattr(csv_import, "get_table_info", lambda _name: _info())
        counts = await import_table_csv("CsvImportProbe", b"id,name\n", "merge", "CsvImportProbe")
        assert counts.rows == 0
        assert counts.inserted == 0

    asyncio.run(_run())


def _patch_db(monkeypatch: pytest.MonkeyPatch, maker: async_sessionmaker[AsyncSession]) -> None:
    monkeypatch.setattr(csv_import, "get_table_info", lambda _name: _info())
    monkeypatch.setattr(csv_import, "async_maker", maker)
    monkeypatch.setattr(csv_import, "_db_type", "sqlite")


async def _create_probe(db_path: Path) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    db_engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
    )
    maker = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    tables = CsvImportProbe.metadata.tables
    name = CsvImportProbe.__name__.lower()
    if name not in tables:
        raise RuntimeError(f"missing table {name}")
    probe = tables[name]

    async with db_engine.begin() as conn:
        await conn.run_sync(probe.create, checkfirst=True)
    return db_engine, maker


async def _seed(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session:
        async with session.begin():
            session.add(CsvImportProbe(id=1, name="old", score=1, active=True, note="keep"))
            session.add(CsvImportProbe(id=2, name="stay", score=2, active=False, note="x"))


ProbeRow = tuple[int | None, str, int | None, bool, str | None]


async def _snapshot(maker: async_sessionmaker[AsyncSession]) -> list[ProbeRow]:
    async with maker() as session:
        result = await session.execute(select(CsvImportProbe).order_by(col(CsvImportProbe.id)))
        items = result.scalars().all()
        return [(item.id, item.name, item.score, item.active, item.note) for item in items]


def test_merge_updates_inserts_and_keeps_other_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        db_engine, maker = await _create_probe(tmp_path / "merge.db")
        try:
            _patch_db(monkeypatch, maker)
            await _seed(maker)
            counts = await import_table_csv(
                "CsvImportProbe",
                _csv_bytes(
                    [
                        ["id", "name", "score", "active", "note"],
                        ["1", "new", "9", "false", ""],
                        ["", "extra", "3", "true", "hi"],
                    ]
                ),
                "merge",
                "CsvImportProbe",
            )
            assert counts.inserted == 1
            assert counts.updated == 1
            assert counts.deleted == 0
            rows = await _snapshot(maker)
            by_name = {row[1]: row for row in rows}
            assert by_name["new"] == (1, "new", 9, False, None)
            assert by_name["stay"] == (2, "stay", 2, False, "x")
            assert by_name["extra"][1:] == ("extra", 3, True, "hi")
            assert by_name["extra"][0] not in (1, 2)
        finally:
            await db_engine.dispose()

    asyncio.run(_run())


def test_replace_deletes_rows_missing_from_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        db_engine, maker = await _create_probe(tmp_path / "replace.db")
        try:
            _patch_db(monkeypatch, maker)
            await _seed(maker)
            counts = await import_table_csv(
                "CsvImportProbe",
                _csv_bytes(
                    [
                        ["id", "name", "score", "active", "note"],
                        ["5", "only", "1", "true", "z"],
                    ]
                ),
                "replace",
                "CsvImportProbe",
            )
            assert counts.deleted == 2
            assert counts.inserted == 1
            assert counts.updated == 0
            assert await _snapshot(maker) == [(5, "only", 1, True, "z")]
        finally:
            await db_engine.dispose()

    asyncio.run(_run())


def test_failed_import_rolls_back_flushed_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        db_engine, maker = await _create_probe(tmp_path / "rollback.db")
        try:
            _patch_db(monkeypatch, maker)
            monkeypatch.setattr(csv_import, "CSV_IMPORT_BATCH", 1)
            await _seed(maker)
            with pytest.raises(CsvImportError) as caught:
                await import_table_csv(
                    "CsvImportProbe",
                    _csv_bytes(
                        [
                            ["id", "score"],
                            ["1", "9"],
                            ["99", "1"],
                        ]
                    ),
                    "merge",
                    "CsvImportProbe",
                )
            assert caught.value.key.endswith("missing_column")
            rows = await _snapshot(maker)
            assert (1, "old", 1, True, "keep") in rows
            assert all(row[0] != 99 for row in rows)
        finally:
            await db_engine.dispose()

    asyncio.run(_run())
