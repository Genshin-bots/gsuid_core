"""表情包、成员、人格绑定。

发送范围只看绑定行。没有绑定行就是不发。
"""

from __future__ import annotations

import uuid
import hashlib
from typing import Sequence
from datetime import datetime, timezone
from dataclasses import dataclass

from sqlmodel import Field, SQLModel, col, func, delete, select
from sqlalchemy import UniqueConstraint
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import with_session, with_read_session

_MEME_PACK_NS = uuid.UUID("a3c1e7b2-4f58-4d09-8e16-2b7c9d0e4f11")
DEFAULT_PACK_ID = str(uuid.uuid5(_MEME_PACK_NS, "hint:common"))
DEFAULT_PACK_NAME = "通用表情包"
MAX_PACKS_PER_PERSONA = 16
_HINT_PACK_PREFIX = "hint:"


def pack_id_for_hint(hint: str) -> str:
    """同一 hint 多次迁移得到同一个 pack_id。"""
    return str(uuid.uuid5(_MEME_PACK_NS, _HINT_PACK_PREFIX + hint))


def pack_name_for_hint(hint: str, pack_id: str, taken: dict[str, str]) -> str:
    """迁移用的包名。taken 是名字到 pack_id。名字不是路径。"""
    if hint == DEFAULT_PACK_NAME:
        name = "通用表情包·标签"
    elif len(hint) <= 64:
        name = hint
    else:
        digest = hashlib.sha256(hint.encode("utf-8")).hexdigest()[:8]
        name = hint[:55] + "_" + digest
    if name not in taken or taken[name] == pack_id:
        return name
    trimmed = hint[:55] if len(hint) >= 55 else name[:55]
    return trimmed + "_" + pack_id[:8]


@dataclass(frozen=True)
class TagMembershipInput:
    pending_pack_id: str
    pending_pack_exists: bool
    has_members: bool
    assign_default_pack: bool


@dataclass(frozen=True)
class TagMembershipAction:
    clear_pending: bool
    assign_default_pack: bool
    join_pack_id: str


def decide_tag_membership(row: TagMembershipInput) -> TagMembershipAction:
    """打标成功后的成员顺序。join_pack_id 为空表示不插入。"""
    clear = False
    if row.pending_pack_id:
        if row.pending_pack_exists:
            return TagMembershipAction(True, False, row.pending_pack_id)
        clear = True
    if row.has_members:
        return TagMembershipAction(clear, False, "")
    if row.assign_default_pack:
        return TagMembershipAction(clear, False, DEFAULT_PACK_ID)
    return TagMembershipAction(clear, row.assign_default_pack, "")


def next_assign_default(
    status: str,
    has_members: bool,
    assign_default_pack: bool,
    pending_pack_id: str,
) -> bool:
    """重打标把状态改回 pending 之前，这一行的标志应写成的值。"""
    if status in ("tagged", "manual") and has_members:
        return False
    if not assign_default_pack:
        return False
    if not has_members and pending_pack_id == "":
        return True
    return assign_default_pack


class AiMemePack(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("name", name="ux_aimemepack_name"),)

    pack_id: str = Field(primary_key=True, max_length=36)
    name: str = Field(max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AiMemePackItem(SQLModel, table=True):
    pack_id: str = Field(primary_key=True, max_length=36)
    meme_id: str = Field(primary_key=True, max_length=16)


class AiPersonaMemeBind(SQLModel, table=True):
    persona_name: str = Field(primary_key=True, max_length=128)
    pack_id: str = Field(primary_key=True, max_length=36)
    position: int = Field(default=0)


class AiMemePackMigration(SQLModel, table=True):
    version: int = Field(primary_key=True)

    @classmethod
    @with_read_session
    async def has_at_least(cls, session: AsyncSession, version: int) -> bool:
        result = await session.execute(select(cls.version).where(col(cls.version) >= version))
        return result.first() is not None

    @classmethod
    @with_session
    async def mark(cls, session: AsyncSession, version: int) -> None:
        existing = await session.get(cls, version)
        if existing is None:
            session.add(cls(version=version))


@dataclass(frozen=True)
class PackRef:
    pack_id: str
    name: str


@dataclass(frozen=True)
class MemePackSummary:
    pack_id: str
    name: str
    member_count: int
    tagged_count: int
    bound_personas: list[str]


@dataclass(frozen=True)
class PackDeleteResult:
    lost_personas: list[str]
    meme_ids: list[str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _ensure_default_pack(session: AsyncSession) -> None:
    row = await session.get(AiMemePack, DEFAULT_PACK_ID)
    if row is None:
        session.add(
            AiMemePack(
                pack_id=DEFAULT_PACK_ID,
                name=DEFAULT_PACK_NAME,
                created_at=_now(),
                updated_at=_now(),
            )
        )


async def _member_exists(session: AsyncSession, pack_id: str, meme_id: str) -> bool:
    row = await session.get(AiMemePackItem, (pack_id, meme_id))
    return row is not None


async def _pack_ids_of(session: AsyncSession, meme_id: str) -> list[str]:
    result = await session.execute(select(AiMemePackItem.pack_id).where(col(AiMemePackItem.meme_id) == meme_id))
    ids = [str(row[0]) for row in result.all()]
    ids.sort()
    return ids


class PackStore:
    """包、成员、绑定的写入与读取。"""

    @classmethod
    @with_session
    async def ensure_default_pack(cls, session: AsyncSession) -> None:
        await _ensure_default_pack(session)

    @classmethod
    @with_session
    async def mount_default_pack(cls, session: AsyncSession, persona_name: str) -> None:
        await _ensure_default_pack(session)
        row = await session.get(AiPersonaMemeBind, (persona_name, DEFAULT_PACK_ID))
        if row is None:
            session.add(
                AiPersonaMemeBind(
                    persona_name=persona_name,
                    pack_id=DEFAULT_PACK_ID,
                    position=0,
                )
            )

    @classmethod
    @with_session
    async def delete_binds_for_persona(cls, session: AsyncSession, persona_name: str) -> int:
        result = await session.execute(
            delete(AiPersonaMemeBind).where(col(AiPersonaMemeBind.persona_name) == persona_name)
        )
        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_read_session
    async def pack_ids_for_persona(cls, session: AsyncSession, persona_name: str) -> list[str]:
        result = await session.execute(
            select(AiPersonaMemeBind.pack_id, AiPersonaMemeBind.position)
            .where(col(AiPersonaMemeBind.persona_name) == persona_name)
            .order_by(col(AiPersonaMemeBind.position).asc(), col(AiPersonaMemeBind.pack_id).asc())
        )
        return [str(row[0]) for row in result.all()]

    @classmethod
    @with_read_session
    async def pack_ids_of_meme(cls, session: AsyncSession, meme_id: str) -> list[str]:
        return await _pack_ids_of(session, meme_id)

    @classmethod
    @with_read_session
    async def pack_exists(cls, session: AsyncSession, pack_id: str) -> bool:
        return await session.get(AiMemePack, pack_id) is not None

    @classmethod
    @with_read_session
    async def get_pack(cls, session: AsyncSession, pack_id: str) -> PackRef | None:
        row = await session.get(AiMemePack, pack_id)
        if row is None:
            return None
        return PackRef(pack_id=row.pack_id, name=row.name)

    @classmethod
    @with_read_session
    async def get_by_name(cls, session: AsyncSession, name: str) -> PackRef | None:
        result = await session.execute(select(AiMemePack).where(col(AiMemePack.name) == name))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return PackRef(pack_id=row.pack_id, name=row.name)

    @classmethod
    @with_session
    async def create_pack(cls, session: AsyncSession, name: str, pack_id: str | None = None) -> PackRef:
        chosen = pack_id if pack_id is not None else str(uuid.uuid4())
        session.add(AiMemePack(pack_id=chosen, name=name, created_at=_now(), updated_at=_now()))
        await session.flush()
        return PackRef(pack_id=chosen, name=name)

    @classmethod
    @with_session
    async def rename_pack(cls, session: AsyncSession, pack_id: str, name: str) -> bool:
        row = await session.get(AiMemePack, pack_id)
        if row is None:
            return False
        row.name = name
        row.updated_at = _now()
        session.add(row)
        return True

    @classmethod
    @with_session
    async def ensure_named_pack(cls, session: AsyncSession, name: str) -> PackRef:
        """按名字找到包；没有则新建。导入整包时用。"""
        result = await session.execute(select(AiMemePack).where(col(AiMemePack.name) == name))
        found = result.scalar_one_or_none()
        if found is not None:
            return PackRef(pack_id=found.pack_id, name=found.name)
        chosen = str(uuid.uuid4())
        session.add(AiMemePack(pack_id=chosen, name=name, created_at=_now(), updated_at=_now()))
        await session.flush()
        return PackRef(pack_id=chosen, name=name)

    @classmethod
    @with_read_session
    async def list_summaries(cls, session: AsyncSession) -> list[MemePackSummary]:
        packs = (await session.execute(select(AiMemePack).order_by(col(AiMemePack.name).asc()))).scalars().all()
        member_rows = (
            await session.execute(select(AiMemePackItem.pack_id, func.count()).group_by(AiMemePackItem.pack_id))
        ).all()
        member_counts = {str(row[0]): int(row[1]) for row in member_rows}
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        tagged_rows = (
            await session.execute(
                select(AiMemePackItem.pack_id, func.count())
                .join(AiMemeRecord, col(AiMemeRecord.meme_id) == col(AiMemePackItem.meme_id))
                .where(col(AiMemeRecord.status).in_(["tagged", "manual"]))
                .group_by(AiMemePackItem.pack_id)
            )
        ).all()
        tagged_counts = {str(row[0]): int(row[1]) for row in tagged_rows}
        bind_rows = (
            await session.execute(
                select(AiPersonaMemeBind.pack_id, AiPersonaMemeBind.persona_name, AiPersonaMemeBind.position).order_by(
                    col(AiPersonaMemeBind.position).asc()
                )
            )
        ).all()
        bound: dict[str, list[str]] = {}
        for row in bind_rows:
            pack_id = str(row[0])
            if pack_id not in bound:
                bound[pack_id] = []
            bound[pack_id].append(str(row[1]))
        summaries: list[MemePackSummary] = []
        for pack in packs:
            count = member_counts[pack.pack_id] if pack.pack_id in member_counts else 0
            tagged = tagged_counts[pack.pack_id] if pack.pack_id in tagged_counts else 0
            personas = bound[pack.pack_id] if pack.pack_id in bound else []
            summaries.append(
                MemePackSummary(
                    pack_id=pack.pack_id,
                    name=pack.name,
                    member_count=count,
                    tagged_count=tagged,
                    bound_personas=personas,
                )
            )
        return summaries

    @classmethod
    @with_session
    async def replace_binds(cls, session: AsyncSession, persona_name: str, pack_ids: Sequence[str]) -> None:
        await session.execute(delete(AiPersonaMemeBind).where(col(AiPersonaMemeBind.persona_name) == persona_name))
        for index, pack_id in enumerate(pack_ids):
            session.add(AiPersonaMemeBind(persona_name=persona_name, pack_id=pack_id, position=index))

    @classmethod
    @with_session
    async def add_bind_if_missing(cls, session: AsyncSession, persona_name: str, pack_id: str) -> bool:
        """已有则不动。返回是否新插入。position 放在当前末尾。"""
        existing = await session.get(AiPersonaMemeBind, (persona_name, pack_id))
        if existing is not None:
            return False
        result = await session.execute(
            select(func.count())
            .select_from(AiPersonaMemeBind)
            .where(col(AiPersonaMemeBind.persona_name) == persona_name)
        )
        count = int(result.scalar() or 0)
        session.add(AiPersonaMemeBind(persona_name=persona_name, pack_id=pack_id, position=count))
        return True

    @classmethod
    @with_read_session
    async def bind_count(cls, session: AsyncSession, persona_name: str) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(AiPersonaMemeBind)
            .where(col(AiPersonaMemeBind.persona_name) == persona_name)
        )
        return int(result.scalar() or 0)

    @classmethod
    @with_session
    async def add_member(cls, session: AsyncSession, pack_id: str, meme_id: str) -> bool:
        if await _member_exists(session, pack_id, meme_id):
            return False
        session.add(AiMemePackItem(pack_id=pack_id, meme_id=meme_id))
        return True

    @classmethod
    @with_session
    async def remove_members(cls, session: AsyncSession, pack_id: str, meme_ids: Sequence[str]) -> list[str]:
        removed: list[str] = []
        for meme_id in meme_ids:
            row = await session.get(AiMemePackItem, (pack_id, meme_id))
            if row is None:
                continue
            await session.delete(row)
            removed.append(meme_id)
        return removed

    @classmethod
    @with_session
    async def delete_members_of_meme(cls, session: AsyncSession, meme_id: str) -> None:
        await session.execute(delete(AiMemePackItem).where(col(AiMemePackItem.meme_id) == meme_id))

    @classmethod
    @with_session
    async def delete_members_of_memes(cls, session: AsyncSession, meme_ids: Sequence[str]) -> None:
        if not meme_ids:
            return
        await session.execute(delete(AiMemePackItem).where(col(AiMemePackItem.meme_id).in_(list(meme_ids))))

    @classmethod
    @with_read_session
    async def member_meme_ids(cls, session: AsyncSession, pack_id: str) -> list[str]:
        result = await session.execute(select(AiMemePackItem.meme_id).where(col(AiMemePackItem.pack_id) == pack_id))
        return [str(row[0]) for row in result.all()]

    @classmethod
    @with_session
    async def apply_successful_tag(cls, session: AsyncSession, meme_id: str) -> str:
        """按四步写入成员。返回加入的 pack_id，空字符串表示没有加入。"""
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        record = await session.get(AiMemeRecord, meme_id)
        if record is None:
            return ""
        pending = record.pending_pack_id
        exists = False
        if pending:
            exists = await session.get(AiMemePack, pending) is not None
        has_members = bool(await _pack_ids_of(session, meme_id))
        action = decide_tag_membership(
            TagMembershipInput(
                pending_pack_id=pending,
                pending_pack_exists=exists,
                has_members=has_members,
                assign_default_pack=record.assign_default_pack,
            )
        )
        if action.join_pack_id == DEFAULT_PACK_ID:
            await _ensure_default_pack(session)
        if action.join_pack_id and not await _member_exists(session, action.join_pack_id, meme_id):
            pack = await session.get(AiMemePack, action.join_pack_id)
            if pack is not None:
                session.add(AiMemePackItem(pack_id=action.join_pack_id, meme_id=meme_id))
        if action.clear_pending:
            record.pending_pack_id = ""
        record.assign_default_pack = action.assign_default_pack
        record.updated_at = _now()
        session.add(record)
        return action.join_pack_id

    @classmethod
    @with_session
    async def prepare_retag(cls, session: AsyncSession, meme_id: str) -> None:
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        record = await session.get(AiMemeRecord, meme_id)
        if record is None:
            return
        has_members = bool(await _pack_ids_of(session, meme_id))
        flag = next_assign_default(record.status, has_members, record.assign_default_pack, record.pending_pack_id)
        if flag != record.assign_default_pack:
            record.assign_default_pack = flag
            record.updated_at = _now()
            session.add(record)

    @classmethod
    @with_session
    async def clear_pending_pack(cls, session: AsyncSession, meme_id: str) -> None:
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        record = await session.get(AiMemeRecord, meme_id)
        if record is None or record.pending_pack_id == "":
            return
        record.pending_pack_id = ""
        record.updated_at = _now()
        session.add(record)

    @classmethod
    @with_session
    async def reject_meme(cls, session: AsyncSession, meme_id: str) -> None:
        """丢掉成员，清空待定包，标志写成假。不删表情行。"""
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        await session.execute(delete(AiMemePackItem).where(col(AiMemePackItem.meme_id) == meme_id))
        record = await session.get(AiMemeRecord, meme_id)
        if record is None:
            return
        record.pending_pack_id = ""
        record.assign_default_pack = False
        record.updated_at = _now()
        session.add(record)

    @classmethod
    @with_session
    async def note_pending_pack(cls, session: AsyncSession, meme_id: str, pack_id: str) -> None:
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        record = await session.get(AiMemeRecord, meme_id)
        if record is None:
            return
        record.pending_pack_id = pack_id
        record.assign_default_pack = False
        record.updated_at = _now()
        session.add(record)

    @classmethod
    @with_session
    async def delete_pack(cls, session: AsyncSession, pack_id: str) -> PackDeleteResult:
        """删除包、成员、绑定，并清空指向它的 pending_pack_id。"""
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        bind_rows = (
            (await session.execute(select(AiPersonaMemeBind).where(col(AiPersonaMemeBind.pack_id) == pack_id)))
            .scalars()
            .all()
        )
        lost: list[str] = []
        for bind in bind_rows:
            result = await session.execute(
                select(func.count())
                .select_from(AiPersonaMemeBind)
                .where(col(AiPersonaMemeBind.persona_name) == bind.persona_name)
            )
            if int(result.scalar() or 0) == 1:
                lost.append(bind.persona_name)
        member_rows = (
            await session.execute(select(AiMemePackItem.meme_id).where(col(AiMemePackItem.pack_id) == pack_id))
        ).all()
        touched = [str(row[0]) for row in member_rows]
        await session.execute(delete(AiMemePackItem).where(col(AiMemePackItem.pack_id) == pack_id))
        await session.execute(delete(AiPersonaMemeBind).where(col(AiPersonaMemeBind.pack_id) == pack_id))
        await session.execute(delete(AiMemePack).where(col(AiMemePack.pack_id) == pack_id))
        pending_rows = (
            (await session.execute(select(AiMemeRecord).where(col(AiMemeRecord.pending_pack_id) == pack_id)))
            .scalars()
            .all()
        )
        for record in pending_rows:
            record.pending_pack_id = ""
            record.updated_at = _now()
            session.add(record)
        return PackDeleteResult(lost_personas=lost, meme_ids=touched)


async def mount_default_pack(persona_name: str) -> None:
    await PackStore.mount_default_pack(persona_name)
