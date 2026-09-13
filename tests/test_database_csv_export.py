"""Database table CSV export: quoting, attachment header, non-blocking iterator."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from gsuid_core.webconsole.database_api import _csv_content_disposition
from gsuid_core.utils.database.admin_api import (
    CSV_BOM,
    csv_cell,
    iter_table_csv,
    encode_csv_rows,
)


def test_csv_cell_none_bool_and_json() -> None:
    assert csv_cell(None, "str") == ""
    assert csv_cell(True, "bool") == "true"
    assert csv_cell(False, "bool") == "false"
    assert csv_cell({"k": "v"}, "json") == '{"k": "v"}'


def test_csv_cell_neutralizes_excel_formulas() -> None:
    assert csv_cell("=cmd|'/c calc'!A0", "str").startswith("'")
    assert csv_cell("@SUM(A1)", "str").startswith("'")
    assert csv_cell("+cmd", "str").startswith("'")
    assert csv_cell(-3, "int") == "-3"


def test_encode_csv_quotes_comma_and_quote() -> None:
    raw = encode_csv_rows([["a,b", 'say "hi"', "plain"]])
    assert raw.decode("utf-8") == '"a,b","say ""hi""",plain\n'


def test_content_disposition_strips_path_chars() -> None:
    header = _csv_content_disposition("User/../x")
    ascii_part = header.split("filename=")[1].split(";")[0]
    assert ".." not in ascii_part
    assert "/" not in ascii_part
    assert "filename*=UTF-8''" in header
    assert header.startswith("attachment; ")


class _DummyQuery:
    def where(self, *_a, **_k):
        return self

    def order_by(self, *_a, **_k):
        return self

    def offset(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self


def _patch_export_session(monkeypatch, rows: list[object]) -> None:
    from gsuid_core.utils.database import admin_api as mod

    table = SimpleNamespace(
        columns=[
            SimpleNamespace(name="id", col_type="int"),
            SimpleNamespace(name="name", col_type="str"),
        ],
        model_class=object,
        table_name="t",
    )
    monkeypatch.setattr(mod, "get_table_info", lambda _name: table)
    monkeypatch.setattr(mod, "_build_where_clause", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "select", lambda *_a, **_k: _DummyQuery())

    remaining = list(rows)

    class FakeSession:
        async def execute(self, _query):
            batch = remaining[: mod.CSV_EXPORT_BATCH]
            del remaining[: mod.CSV_EXPORT_BATCH]
            result = MagicMock()
            result.scalars.return_value.all.return_value = batch
            return result

    class FakeMaker:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(mod, "async_maker", lambda: FakeMaker())


def test_iter_table_csv_header_only_when_empty(monkeypatch) -> None:
    _patch_export_session(monkeypatch, [])

    async def _run() -> bytes:
        chunks: list[bytes] = []
        async for chunk in iter_table_csv("t"):
            chunks.append(chunk)
        return b"".join(chunks)

    body = asyncio.run(_run())
    assert body.startswith(CSV_BOM)
    assert body[len(CSV_BOM) :] == b"id,name\n"


def test_iter_table_csv_batches_and_quotes(monkeypatch) -> None:
    from gsuid_core.utils.database import admin_api as mod

    monkeypatch.setattr(mod, "CSV_EXPORT_BATCH", 1)
    _patch_export_session(
        monkeypatch,
        [
            SimpleNamespace(id=1, name="alpha"),
            SimpleNamespace(id=2, name="a,b"),
        ],
    )

    async def _run() -> list[bytes]:
        chunks: list[bytes] = []
        async for chunk in iter_table_csv("t"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    assert chunks[0].startswith(CSV_BOM)
    body = b"".join(chunks)
    text = body.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == "id,name"
    assert lines[1] == "1,alpha"
    assert lines[2] == '2,"a,b"'
    # header + 2 data batches (CSV_EXPORT_BATCH=1)
    assert len(chunks) == 3


def test_iter_table_csv_keyset_uses_id(monkeypatch) -> None:
    from sqlmodel import SQLModel

    from gsuid_core.utils.database import admin_api as mod

    class _CsvRow(SQLModel):
        id: int
        name: str

    class _Pk:
        def __gt__(self, other: object) -> bool:
            return True

    monkeypatch.setattr(mod, "CSV_EXPORT_BATCH", 1)
    monkeypatch.setattr(mod, "_pk_column", lambda _cls: _Pk())
    _patch_export_session(
        monkeypatch,
        [
            _CsvRow(id=1, name="alpha"),
            _CsvRow(id=2, name="beta"),
        ],
    )
    monkeypatch.setattr(
        mod,
        "get_table_info",
        lambda _name: SimpleNamespace(
            columns=[
                SimpleNamespace(name="id", col_type="int"),
                SimpleNamespace(name="name", col_type="str"),
            ],
            model_class=_CsvRow,
            table_name="t",
        ),
    )

    async def _run() -> str:
        chunks: list[bytes] = []
        async for chunk in iter_table_csv("t"):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8-sig")

    text = asyncio.run(_run())
    lines = text.splitlines()
    assert lines[0] == "id,name"
    assert lines[1] == "1,alpha"
    assert lines[2] == "2,beta"
