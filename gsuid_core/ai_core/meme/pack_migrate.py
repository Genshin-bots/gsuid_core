"""表情包迁移。不建表，不调用 create_all，不删除表情行或图片。"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

from sqlmodel import col, select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.meme.library import get_memes_base_path
from gsuid_core.ai_core.meme.pack_model import (
    DEFAULT_PACK_ID,
    DEFAULT_PACK_NAME,
    AiMemePack,
    AiMemePackItem,
    AiPersonaMemeBind,
    AiMemePackMigration,
    pack_id_for_hint,
    pack_name_for_hint,
)
from gsuid_core.ai_core.persona.resource import list_available_personas
from gsuid_core.utils.database.base_models import with_session
from gsuid_core.ai_core.meme.database_model import AiMemeRecord


def _file_size(base: Path, relative: str) -> int:
    path = base / relative
    if not path.is_file():
        return -1
    return path.stat().st_size


class _Snap:
    def __init__(self, meme_id: str, status: str, size: int) -> None:
        self.meme_id = meme_id
        self.status = status
        self.size = size


class _PackSql:
    @classmethod
    @with_session
    async def run(cls, session: AsyncSession) -> tuple[int, int]:
        """version 1。返回 (快照张数, 迁移期间新插入张数)。已有 version 则返回 (0, 0) 且不改数据。"""
        if await session.get(AiMemePackMigration, 1) is not None:
            return 0, 0

        base = get_memes_base_path()
        rows = (await session.execute(select(AiMemeRecord))).scalars().all()
        snapshot = {row.meme_id: _Snap(row.meme_id, row.status, _file_size(base, row.file_path)) for row in rows}
        snapshot_ids = set(snapshot)

        now = datetime.now(timezone.utc)
        default = await session.get(AiMemePack, DEFAULT_PACK_ID)
        if default is None:
            session.add(AiMemePack(pack_id=DEFAULT_PACK_ID, name=DEFAULT_PACK_NAME, created_at=now, updated_at=now))
            await session.flush()

        taken: dict[str, str] = {}
        existing_packs = (await session.execute(select(AiMemePack))).scalars().all()
        for pack in existing_packs:
            taken[pack.name] = pack.pack_id

        for row in rows:
            if row.folder in ("inbox", "rejected"):
                continue
            hint = row.persona_hint or ""
            if hint in ("", "common"):
                pack_id = DEFAULT_PACK_ID
            else:
                pack_id = pack_id_for_hint(hint)
                if await session.get(AiMemePack, pack_id) is None:
                    name = pack_name_for_hint(hint, pack_id, taken)
                    now = datetime.now(timezone.utc)
                    session.add(AiMemePack(pack_id=pack_id, name=name, created_at=now, updated_at=now))
                    taken[name] = pack_id
                    await session.flush()
            if await session.get(AiMemePackItem, (pack_id, row.meme_id)) is None:
                session.add(AiMemePackItem(pack_id=pack_id, meme_id=row.meme_id))

        for persona_name in list_available_personas():
            if await session.get(AiPersonaMemeBind, (persona_name, DEFAULT_PACK_ID)) is None:
                session.add(AiPersonaMemeBind(persona_name=persona_name, pack_id=DEFAULT_PACK_ID, position=0))

        for row in rows:
            if row.status not in ("pending", "pending_manual"):
                continue
            if row.pending_pack_id != "":
                continue
            member = await session.execute(
                select(AiMemePackItem.meme_id).where(col(AiMemePackItem.meme_id) == row.meme_id)
            )
            if member.first() is not None:
                continue
            row.assign_default_pack = True
            session.add(row)

        later = (await session.execute(select(AiMemeRecord))).scalars().all()
        later_by_id = {row.meme_id: row for row in later}
        inserted = 0
        for row in later:
            if row.meme_id not in snapshot_ids:
                inserted += 1
        for meme_id, before in snapshot.items():
            if meme_id not in later_by_id:
                raise RuntimeError(meme_id)
            row = later_by_id[meme_id]
            if row.status == "rejected" and before.status != "rejected":
                raise RuntimeError(meme_id)
            if _file_size(base, row.file_path) != before.size:
                raise RuntimeError(meme_id)

        session.add(AiMemePackMigration(version=1))
        logger.info(t("log.meme.pack_migrate_sql", snapshot=len(snapshot), inserted=inserted))
        return len(snapshot), inserted


async def migrate_pack_sql() -> tuple[int, int]:
    return await _PackSql.run()


async def finish_pack_payload() -> None:
    """集合就绪之后回填。client 为空或 set_payload 失败时不写 version 2。"""
    if not await AiMemePackMigration.has_at_least(1):
        return
    if await AiMemePackMigration.has_at_least(2):
        return
    wrote = await _backfill_pack_payload()
    if wrote:
        await AiMemePackMigration.mark(2)


async def run_pack_migration() -> None:
    """测试与手工入口：SQL 与 payload 连着跑。启动路径把二者拆开。"""
    await migrate_pack_sql()
    await finish_pack_payload()


async def _backfill_pack_payload() -> bool:
    """点缺失只记日志。set_payload 抛错则不写 version 2。client 为空时返回 False。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return False
    from gsuid_core.ai_core.meme.library import write_member_payloads

    try:
        await write_member_payloads(strict=True)
    except Exception as e:
        logger.warning(t("log.meme.pack_payload_fail", meme_id="*", e=e))
        return False
    return True
