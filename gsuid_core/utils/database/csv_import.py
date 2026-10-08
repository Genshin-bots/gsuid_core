"""把控制台导出的 CSV 写回已注册的数据表。

增量按主键更新已有行并插入新行。覆盖先清空该表再插入。
"""

from __future__ import annotations

import io
import re
import csv
import json
import asyncio
from typing import Literal
from datetime import date, time, datetime
from dataclasses import dataclass

from sqlmodel import SQLModel, or_, and_, func, delete as delete_rows, select
from sqlalchemy import Table, Column
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from gsuid_core.i18n import t
from gsuid_core.utils.database.admin_api import ColumnInfo, DatabaseTableInfo, _sa_table, get_table_info
from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.utils.database.base_models import engine, _db_type, async_maker

CSV_IMPORT_BATCH = 200
CSV_IMPORT_MAX_ROWS = 50_000
CSV_IMPORT_MAX_BYTES = 32 * 1024 * 1024

ImportMode = Literal["merge", "replace"]
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
CellValue = JsonValue | datetime | date | time

_TRUE = {"true", "1", "yes", "y"}
_FALSE = {"false", "0", "no", "n"}
_INT_RE = re.compile(r"[+-]?\d+\Z")
_INT_DOT_RE = re.compile(r"([+-]?\d+)\.0+\Z")
_FLOAT_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


class CsvImportError(Exception):
    def __init__(self, key: str, **params: str | int) -> None:
        self.key = key
        self.params: dict[str, str | int] = params
        super().__init__(key)


@dataclass(frozen=True)
class ImportField:
    name: str
    col_type: str
    nullable: bool
    primary_key: bool
    has_default: bool
    omit_when_empty: bool


@dataclass(frozen=True)
class PreparedRow:
    line: int
    values: dict[str, CellValue]
    pk: tuple[str | int, ...] | None


@dataclass(frozen=True)
class ImportCounts:
    mode: ImportMode
    inserted: int
    updated: int
    deleted: int
    rows: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "mode": self.mode,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "rows": self.rows,
        }


def import_error_text(err: CsvImportError) -> str:
    params = err.params
    key = err.key
    if key == "msg.webconsole.database_import.bad_cell":
        return t("msg.webconsole.database_import.bad_cell", row=params["row"], column=params["column"])
    if key == "msg.webconsole.database_import.bad_encoding":
        return t("msg.webconsole.database_import.bad_encoding")
    if key == "msg.webconsole.database_import.bad_mode":
        return t("msg.webconsole.database_import.bad_mode")
    if key == "msg.webconsole.database_import.confirm_mismatch":
        return t("msg.webconsole.database_import.confirm_mismatch")
    if key == "msg.webconsole.database_import.duplicate_header":
        return t("msg.webconsole.database_import.duplicate_header", column=params["column"])
    if key == "msg.webconsole.database_import.duplicate_pk":
        return t("msg.webconsole.database_import.duplicate_pk", row=params["row"], pk=params["pk"])
    if key == "msg.webconsole.database_import.empty_file":
        return t("msg.webconsole.database_import.empty_file")
    if key == "msg.webconsole.database_import.empty_header":
        return t("msg.webconsole.database_import.empty_header")
    if key == "msg.webconsole.database_import.extra_cells":
        return t("msg.webconsole.database_import.extra_cells", row=params["row"])
    if key == "msg.webconsole.database_import.file_too_large":
        return t("msg.webconsole.database_import.file_too_large", limit_mb=params["limit_mb"])
    if key == "msg.webconsole.database_import.missing_column":
        return t("msg.webconsole.database_import.missing_column", row=params["row"], column=params["column"])
    if key == "msg.webconsole.database_import.missing_pk":
        return t("msg.webconsole.database_import.missing_pk", row=params["row"], column=params["column"])
    if key == "msg.webconsole.database_import.no_header":
        return t("msg.webconsole.database_import.no_header")
    if key == "msg.webconsole.database_import.partial_pk":
        return t("msg.webconsole.database_import.partial_pk", row=params["row"])
    if key == "msg.webconsole.database_import.replace_empty":
        return t("msg.webconsole.database_import.replace_empty")
    if key == "msg.webconsole.database_import.table_not_found":
        return t("msg.webconsole.database_import.table_not_found", table=params["table"])
    if key == "msg.webconsole.database_import.too_many_rows":
        return t("msg.webconsole.database_import.too_many_rows", limit=params["limit"])
    if key == "msg.webconsole.database_import.unknown_columns":
        return t("msg.webconsole.database_import.unknown_columns", columns=params["columns"])
    return t("msg.webconsole.database_import.bad_mode")


def decode_csv_bytes(raw: bytes) -> str:
    if raw.strip() == b"":
        raise CsvImportError("msg.webconsole.database_import.empty_file")
    text = _decode_text(raw)
    if "\x00" in text:
        raise CsvImportError("msg.webconsole.database_import.bad_encoding")
    return text


def _decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise CsvImportError("msg.webconsole.database_import.bad_encoding") from exc
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise CsvImportError("msg.webconsole.database_import.bad_encoding") from exc


def _parse_mode(mode: str) -> ImportMode:
    cleaned = mode.strip().lower()
    if cleaned == "merge" or cleaned == "replace":
        return cleaned
    raise CsvImportError("msg.webconsole.database_import.bad_mode")


def _undo_excel_neutralize(text: str) -> str:
    # 导出给公式加了前导单引号，导入按同一规则还原。
    if len(text) < 2 or text[0] != "'":
        return text
    rest = text[1:]
    head = rest[0]
    if head in ("=", "@", "\t", "\r"):
        return rest
    if head in ("+", "-") and (len(rest) == 1 or not rest[1].isdigit()):
        return rest
    return text


def _is_blank(text: str, col_type: str) -> bool:
    if col_type in ("str", "text"):
        return text == ""
    return text.strip() == ""


def _bad_cell(line: int, column: str) -> CsvImportError:
    return CsvImportError("msg.webconsole.database_import.bad_cell", row=line, column=column)


def _coerce_blank(field: ImportField, line: int) -> None | str:
    if field.nullable:
        return None
    if field.col_type in ("str", "text"):
        return ""
    raise _bad_cell(line, field.name)


def _parse_int(text: str, field: ImportField, line: int) -> int:
    if _INT_RE.fullmatch(text):
        return int(text)
    dotted = _INT_DOT_RE.fullmatch(text)
    if dotted is not None:
        return int(dotted.group(1))
    raise _bad_cell(line, field.name)


def _parse_float(text: str, field: ImportField, line: int) -> float:
    if _FLOAT_RE.fullmatch(text) is None:
        raise _bad_cell(line, field.name)
    return float(text)


def _as_json(value: object) -> JsonValue | None:
    """返回解析结果。无法表示时返回 None；JSON null 也是 None，由调用方先判断。"""
    if value is None:
        return None
    if isinstance(value, str | float | bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        items: list[JsonValue] = []
        for item in value:
            if item is None:
                items.append(None)
                continue
            parsed = _as_json(item)
            if parsed is None:
                return None
            items.append(parsed)
        return items
    if isinstance(value, dict):
        out: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return None
            if item is None:
                out[key] = None
                continue
            parsed = _as_json(item)
            if parsed is None:
                return None
            out[key] = parsed
        return out
    return None


def _json_ok(value: object) -> bool:
    if value is None or isinstance(value, str | float | bool | int):
        return True
    if isinstance(value, list):
        return all(_json_ok(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_ok(item) for key, item in value.items())
    return False


def _parse_json(text: str, field: ImportField, line: int) -> JsonValue:
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _bad_cell(line, field.name) from exc
    if not _json_ok(loaded):
        raise _bad_cell(line, field.name)
    if loaded is None:
        return None
    parsed = _as_json(loaded)
    if parsed is None:
        raise _bad_cell(line, field.name)
    return parsed


def _parse_datetime(text: str, field: ImportField, line: int) -> datetime:
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise _bad_cell(line, field.name) from exc


def _parse_date(text: str, field: ImportField, line: int) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise _bad_cell(line, field.name) from exc


def _parse_time(text: str, field: ImportField, line: int) -> time:
    try:
        return time.fromisoformat(text)
    except ValueError as exc:
        raise _bad_cell(line, field.name) from exc


def _coerce_value(text: str, field: ImportField, line: int) -> CellValue:
    kind = field.col_type
    if kind in ("str", "text"):
        return text
    stripped = text.strip()
    if kind == "bool":
        token = stripped.lower()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
        raise _bad_cell(line, field.name)
    if kind == "int":
        return _parse_int(stripped, field, line)
    if kind == "float":
        return _parse_float(stripped, field, line)
    if kind == "json":
        return _parse_json(stripped, field, line)
    if kind == "datetime":
        return _parse_datetime(stripped, field, line)
    if kind == "date":
        return _parse_date(stripped, field, line)
    if kind == "time":
        return _parse_time(stripped, field, line)
    return text


def _pk_token(value: CellValue, field: ImportField, line: int) -> str | int:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _bad_cell(line, field.name)
    return value


def _missing_required(fields: list[ImportField], values: dict[str, CellValue]) -> str | None:
    for field in fields:
        if field.nullable or field.has_default or field.omit_when_empty:
            continue
        if field.name not in values:
            return field.name
    return None


def _read_header(header: list[str]) -> tuple[list[str], dict[str, int]]:
    if not header:
        raise CsvImportError("msg.webconsole.database_import.no_header")
    if header[0].startswith("\ufeff"):
        header[0] = header[0].lstrip("\ufeff")
    names: list[str] = []
    index: dict[str, int] = {}
    for position, cell in enumerate(header):
        name = cell.strip()
        if name == "":
            raise CsvImportError("msg.webconsole.database_import.empty_header")
        if name in index:
            raise CsvImportError("msg.webconsole.database_import.duplicate_header", column=name)
        index[name] = position
        names.append(name)
    return names, index


def _unknown_column_text(names: list[str]) -> str:
    shown = ", ".join(names[:8])
    extra = len(names) - 8
    if extra > 0:
        return f"{shown} (+{extra})"
    return shown


def prepare_import_rows(fields: list[ImportField], text: str, mode: ImportMode) -> list[PreparedRow]:
    reader = csv.reader(io.StringIO(text, newline=""))
    parsed = list(reader)
    if not parsed:
        raise CsvImportError("msg.webconsole.database_import.no_header")
    header_names, header_index = _read_header(parsed[0])
    field_by_name = {field.name: field for field in fields}
    unknown = [name for name in header_names if name not in field_by_name]
    if unknown:
        raise CsvImportError(
            "msg.webconsole.database_import.unknown_columns",
            columns=_unknown_column_text(unknown),
        )
    pk_fields = [field for field in fields if field.primary_key]
    prepared: list[PreparedRow] = []
    seen: dict[tuple[str | int, ...], int] = {}
    for line, cells in enumerate(parsed[1:], start=2):
        if len(cells) > len(header_names):
            extra = cells[len(header_names) :]
            if any(cell != "" for cell in extra):
                raise CsvImportError("msg.webconsole.database_import.extra_cells", row=line)
            cells = cells[: len(header_names)]
        if cells and all(cell == "" for cell in cells):
            continue
        if not cells:
            continue
        values: dict[str, CellValue] = {}
        for name in header_names:
            field = field_by_name[name]
            position = header_index[name]
            raw = cells[position] if position < len(cells) else ""
            undone = _undo_excel_neutralize(raw)
            if field.primary_key and _is_blank(undone, field.col_type):
                if field.omit_when_empty:
                    continue
                raise CsvImportError(
                    "msg.webconsole.database_import.missing_pk",
                    row=line,
                    column=field.name,
                )
            if _is_blank(undone, field.col_type):
                values[name] = _coerce_blank(field, line)
                continue
            values[name] = _coerce_value(undone, field, line)
        pk, complete = _row_pk(pk_fields, header_index, values, line)
        if complete and pk is not None:
            if pk in seen:
                raise CsvImportError(
                    "msg.webconsole.database_import.duplicate_pk",
                    row=line,
                    pk=_pk_label(pk),
                )
            seen[pk] = line
        elif pk is None:
            missing = _missing_required(fields, values)
            if missing is not None:
                raise CsvImportError(
                    "msg.webconsole.database_import.missing_column",
                    row=line,
                    column=missing,
                )
        prepared.append(PreparedRow(line=line, values=values, pk=pk if complete else None))
        if len(prepared) > CSV_IMPORT_MAX_ROWS:
            raise CsvImportError("msg.webconsole.database_import.too_many_rows", limit=CSV_IMPORT_MAX_ROWS)
    if mode == "replace" and not prepared:
        raise CsvImportError("msg.webconsole.database_import.replace_empty")
    return prepared


def _pk_label(pk: tuple[str | int, ...]) -> str:
    return ",".join(str(part) for part in pk)


def _row_pk(
    pk_fields: list[ImportField],
    header_index: dict[str, int],
    values: dict[str, CellValue],
    line: int,
) -> tuple[tuple[str | int, ...] | None, bool]:
    if not pk_fields:
        return None, False
    filled: list[str | int] = []
    empty_count = 0
    for field in pk_fields:
        if field.name not in header_index or field.name not in values:
            if field.omit_when_empty or field.name not in header_index:
                empty_count += 1
                continue
            raise CsvImportError("msg.webconsole.database_import.missing_pk", row=line, column=field.name)
        filled.append(_pk_token(values[field.name], field, line))
    if empty_count and filled:
        raise CsvImportError("msg.webconsole.database_import.partial_pk", row=line)
    if not filled:
        return None, False
    return tuple(filled), True


def _is_int_column(column: Column[object]) -> bool:
    return "int" in type(column.type).__name__.lower()


def _field_from_sa(info: ColumnInfo, column: Column[object]) -> ImportField:
    autoincrement = column.autoincrement is True or (
        column.autoincrement == "auto" and column.primary_key and _is_int_column(column)
    )
    has_default = column.default is not None or column.server_default is not None
    nullable = bool(column.nullable)
    primary_key = bool(column.primary_key)
    return ImportField(
        name=info.name,
        col_type=info.col_type,
        nullable=nullable,
        primary_key=primary_key,
        has_default=has_default,
        omit_when_empty=primary_key and (autoincrement or has_default or nullable),
    )


def _field_from_info(info: ColumnInfo, pk_name: str) -> ImportField:
    primary_key = info.name == pk_name
    nullable = info.nullable
    return ImportField(
        name=info.name,
        col_type=info.col_type,
        nullable=nullable,
        primary_key=primary_key,
        has_default=info.default is not None,
        omit_when_empty=primary_key and nullable,
    )


def import_fields(table_info: DatabaseTableInfo) -> list[ImportField]:
    table = _sa_table(table_info.model_class)
    pk_name = "id"
    if table_info.columns and not any(column.name == "id" for column in table_info.columns):
        pk_name = table_info.columns[0].name
    fields: list[ImportField] = []
    for info in table_info.columns:
        if table is not None and info.name in table.c:
            column = table.c[info.name]
            if isinstance(column, Column):
                fields.append(_field_from_sa(info, column))
                continue
        fields.append(_field_from_info(info, pk_name))
    return fields


def _object_pk(item: SQLModel, pk_names: list[str]) -> tuple[str | int, ...] | None:
    data = item.model_dump()
    parts: list[str | int] = []
    for name in pk_names:
        if name not in data:
            return None
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, str | int):
            return None
        parts.append(value)
    return tuple(parts)


async def _count_rows(session: AsyncSession, model_class: type[SQLModel]) -> int:
    result = await session.execute(select(func.count()).select_from(model_class))
    raw: object = result.scalar()
    if type(raw) is int:
        return raw
    return 0


async def _load_existing(
    session: AsyncSession,
    model_class: type[SQLModel],
    table: Table,
    pk_names: list[str],
    keys: list[tuple[str | int, ...]],
) -> dict[tuple[str | int, ...], SQLModel]:
    if not keys or not pk_names:
        return {}
    if len(pk_names) == 1:
        column = table.c[pk_names[0]]
        stmt = select(model_class).where(column.in_([key[0] for key in keys]))
    else:
        clauses: list[ColumnElement[bool]] = []
        for key in keys:
            parts: list[ColumnElement[bool]] = []
            for index, name in enumerate(pk_names):
                column = table.c[name]
                parts.append(column == key[index])
            if len(parts) == 1:
                clauses.append(parts[0])
            else:
                clauses.append(and_(*parts))
        if len(clauses) == 1:
            stmt = select(model_class).where(clauses[0])
        else:
            stmt = select(model_class).where(or_(*clauses))
    result = await session.execute(stmt)
    found: dict[tuple[str | int, ...], SQLModel] = {}
    for item in result.scalars().all():
        pk = _object_pk(item, pk_names)
        if pk is not None:
            found[pk] = item
    return found


def _insert_record(model_class: type[SQLModel], values: dict[str, CellValue]) -> SQLModel:
    record = model_class.model_validate(values)
    if isinstance(record, SQLModel):
        return record
    raise CsvImportError("msg.webconsole.database_import.bad_mode")


async def apply_import_rows(
    session: AsyncSession,
    model_class: type[SQLModel],
    fields: list[ImportField],
    prepared: list[PreparedRow],
    mode: ImportMode,
) -> ImportCounts:
    pk_names = [field.name for field in fields if field.primary_key]
    inserted = 0
    updated = 0
    deleted = 0
    table = _sa_table(model_class)
    if mode == "replace":
        deleted = await _count_rows(session, model_class)
        await session.execute(delete_rows(model_class))
        session.expunge_all()
    for offset in range(0, len(prepared), CSV_IMPORT_BATCH):
        batch = prepared[offset : offset + CSV_IMPORT_BATCH]
        existing: dict[tuple[str | int, ...], SQLModel] = {}
        if mode == "merge" and pk_names and table is None:
            raise CsvImportError(
                "msg.webconsole.database_import.table_not_found",
                table=model_class.__name__,
            )
        if mode == "merge" and table is not None:
            keys = [row.pk for row in batch if row.pk is not None]
            existing = await _load_existing(session, model_class, table, pk_names, keys)
        for row in batch:
            if mode == "merge" and row.pk is not None and row.pk in existing:
                current = existing[row.pk]
                for name, value in row.values.items():
                    if name in pk_names:
                        continue
                    setattr(current, name, value)
                updated += 1
                continue
            missing = _missing_required(fields, row.values)
            if missing is not None:
                raise CsvImportError(
                    "msg.webconsole.database_import.missing_column",
                    row=row.line,
                    column=missing,
                )
            session.add(_insert_record(model_class, row.values))
            inserted += 1
        await session.flush()
        await asyncio.sleep(0)
    return ImportCounts(
        mode=mode,
        inserted=inserted,
        updated=updated,
        deleted=deleted,
        rows=len(prepared),
    )


def _server_engine() -> AsyncEngine | None:
    candidate: object = engine
    if isinstance(candidate, AsyncEngine):
        return candidate
    return None


async def _commit_in_session(
    model_class: type[SQLModel],
    fields: list[ImportField],
    prepared: list[PreparedRow],
    mode: ImportMode,
) -> ImportCounts:
    async with async_maker() as session:
        async with session.begin():
            return await apply_import_rows(session, model_class, fields, prepared, mode)


async def _commit_import(
    model_class: type[SQLModel],
    fields: list[ImportField],
    prepared: list[PreparedRow],
    mode: ImportMode,
) -> ImportCounts:
    if _db_type == "sqlite":
        async with sqlite_write_gate.hold(core=True):
            return await _commit_in_session(model_class, fields, prepared, mode)
    # 非 SQLite 引擎默认 AUTOCOMMIT，覆盖导入的删除和插入要进同一事务。
    server = _server_engine()
    if server is None:
        return await _commit_in_session(model_class, fields, prepared, mode)
    bound = server.execution_options(isolation_level="READ COMMITTED")
    async with bound.connect() as conn:
        async with conn.begin():
            session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                counts = await apply_import_rows(session, model_class, fields, prepared, mode)
                await session.flush()
                return counts
            finally:
                await session.close()


async def import_table_csv(table_name: str, raw: bytes, mode: str, confirm_table: str) -> ImportCounts:
    parsed_mode = _parse_mode(mode)
    if confirm_table != table_name:
        raise CsvImportError("msg.webconsole.database_import.confirm_mismatch")
    if len(raw) > CSV_IMPORT_MAX_BYTES:
        raise CsvImportError(
            "msg.webconsole.database_import.file_too_large",
            limit_mb=CSV_IMPORT_MAX_BYTES // (1024 * 1024),
        )
    table_info = get_table_info(table_name)
    if table_info is None:
        raise CsvImportError("msg.webconsole.database_import.table_not_found", table=table_name)
    text = decode_csv_bytes(raw)
    fields = import_fields(table_info)
    prepared = await asyncio.to_thread(prepare_import_rows, fields, text, parsed_mode)
    if not prepared:
        return ImportCounts(mode=parsed_mode, inserted=0, updated=0, deleted=0, rows=0)
    return await _commit_import(table_info.model_class, fields, prepared, parsed_mode)
