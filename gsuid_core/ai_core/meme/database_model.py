"""表情包数据模型

AiMemeRecord 表情包主表，使用 SQLModel 直接定义。
使用 sha256(图片内容)[:16] 作为 meme_id 保证内容级去重。
不继承 BaseIDModel，因为 meme_id 本身就是主键。
"""

from typing import List, Optional, Sequence
from datetime import datetime, timezone

from sqlmodel import JSON, Field, Column, SQLModel, col, select
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import with_session


class AiMemeRecord(SQLModel, table=True):
    """表情包主表"""

    # 自定义 ID：sha256(图片内容)[:16] 保证内容级去重
    meme_id: str = Field(primary_key=True, max_length=16)

    # ── 文件信息 ──
    file_path: str = Field(index=True, max_length=512)
    file_size: int = Field(default=0)
    file_mime: str = Field(default="", max_length=64)
    width: int = Field(default=0)
    height: int = Field(default=0)

    # ── 来源信息 ──
    source_group: str = Field(default="", max_length=64)
    source_user: str = Field(default="", max_length=64)
    source_url: str = Field(default="", max_length=1024)

    # ── 分类信息 ──
    folder: str = Field(default="inbox", index=True, max_length=64)
    persona_hint: str = Field(default="common", index=True, max_length=64)

    # ── 标签（JSON 字段） ──
    emotion_tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    scene_tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    description: str = Field(default="", max_length=512)
    custom_tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))

    # ── 状态 ──
    status: str = Field(default="pending", index=True, max_length=32)
    # "pending"         → 已入库，等待 VLM 打标
    # "tagged"          → VLM 打标完成
    # "manual"          → 人工在 WebConsole 打标/编辑过
    # "pending_manual"  → VLM 打标失败，待人工处理
    # "rejected"        → NSFW 或质量不达标

    nsfw_score: float = Field(default=0.0)

    # ── 使用统计 ──
    use_count: int = Field(default=0)
    last_used_at: Optional[datetime] = Field(default=None)
    last_used_group: str = Field(default="", max_length=64)

    # ── 时间戳 ──
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tagged_at: Optional[datetime] = Field(default=None)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # ── Qdrant point id ──
    qdrant_id: str = Field(default="", max_length=36)

    @property
    def all_tags(self) -> List[str]:
        return self.emotion_tags + self.scene_tags + self.custom_tags

    # ── 数据库操作方法 ──

    @classmethod
    @with_session
    async def get_by_meme_id(
        cls,
        session: AsyncSession,
        meme_id: str,
    ) -> Optional["AiMemeRecord"]:
        """根据 meme_id 获取记录"""
        result = await session.execute(select(cls).where(cls.meme_id == meme_id))
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def exists_by_meme_id(
        cls,
        session: AsyncSession,
        meme_id: str,
    ) -> bool:
        """检查 meme_id 是否已存在"""
        result = await session.execute(select(cls.meme_id).where(cls.meme_id == meme_id))
        return result.scalar_one_or_none() is not None

    @classmethod
    @with_session
    async def insert_record(
        cls,
        session: AsyncSession,
        record: "AiMemeRecord",
    ) -> None:
        """插入一条记录"""
        session.add(record)

    @classmethod
    @with_session
    async def update_record(
        cls,
        session: AsyncSession,
        meme_id: str,
        update_data: dict,
    ) -> bool:
        """更新记录字段，返回是否成功"""
        result = await session.execute(select(cls).where(cls.meme_id == meme_id))
        record = result.scalar_one_or_none()
        if record is None:
            return False
        for key, value in update_data.items():
            setattr(record, key, value)
        record.updated_at = datetime.now(timezone.utc)
        session.add(record)
        return True

    @classmethod
    @with_session
    async def delete_by_meme_id(
        cls,
        session: AsyncSession,
        meme_id: str,
    ) -> bool:
        """删除记录，返回是否成功"""
        result = await session.execute(select(cls).where(cls.meme_id == meme_id))
        record = result.scalar_one_or_none()
        if record is None:
            return False
        await session.delete(record)
        return True

    @classmethod
    @with_session
    async def get_by_folder(
        cls,
        session: AsyncSession,
        folder: str,
        status: Optional[str] = None,
        sort: str = "created_at_desc",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence["AiMemeRecord"], int]:
        """按文件夹获取记录列表，返回 (记录列表, 总数)"""
        stmt = select(cls).where(cls.folder == folder)
        if status:
            stmt = stmt.where(cls.status == status)

        # 计算总数
        count_stmt = select(func.count()).select_from(cls).where(cls.folder == folder)
        if status:
            count_stmt = select(func.count()).select_from(cls).where(cls.folder == folder, cls.status == status)
        total_result = await session.execute(count_stmt)
        total = total_result.scalar() or 0

        # 排序
        if sort == "use_count_desc":
            stmt = stmt.order_by(col(cls.use_count).desc())
        elif sort == "use_count_asc":
            stmt = stmt.order_by(col(cls.use_count).asc())
        else:
            stmt = stmt.order_by(col(cls.created_at).desc())

        # 分页
        offset = (page - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)

        result = await session.execute(stmt)
        records = result.scalars().all()
        return records, total

    @classmethod
    @with_session
    async def get_all_records(
        cls,
        session: AsyncSession,
        status: Optional[str] = None,
        sort: str = "created_at_desc",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence["AiMemeRecord"], int]:
        """获取所有记录列表（不过滤文件夹），返回 (记录列表, 总数)"""
        if status:
            count_stmt = select(func.count()).select_from(cls).where(cls.status == status)
            stmt = select(cls).where(cls.status == status)
        else:
            count_stmt = select(func.count()).select_from(cls)
            stmt = select(cls)

        total_result = await session.execute(count_stmt)
        total = total_result.scalar() or 0

        if sort == "use_count_desc":
            stmt = stmt.order_by(col(cls.use_count).desc())
        elif sort == "use_count_asc":
            stmt = stmt.order_by(col(cls.use_count).asc())
        else:
            stmt = stmt.order_by(col(cls.created_at).desc())

        offset = (page - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)

        result = await session.execute(stmt)
        records = result.scalars().all()
        return records, total

    @classmethod
    @with_session
    async def get_by_meme_ids(
        cls,
        session: AsyncSession,
        meme_ids: List[str],
    ) -> Sequence["AiMemeRecord"]:
        """根据 meme_id 列表批量获取记录"""
        if not meme_ids:
            return []
        result = await session.execute(select(cls).where(col(cls.meme_id).in_(meme_ids)))
        return result.scalars().all()

    @classmethod
    @with_session
    async def get_pending_records(
        cls,
        session: AsyncSession,
        limit: int = 10,
    ) -> Sequence["AiMemeRecord"]:
        """获取待打标的记录"""
        result = await session.execute(
            select(cls).where(cls.status == "pending").order_by(col(cls.created_at).asc()).limit(limit)
        )
        return result.scalars().all()

    @classmethod
    @with_session
    async def get_all_by_status(
        cls,
        session: AsyncSession,
        status: str,
    ) -> Sequence["AiMemeRecord"]:
        """获取指定状态的所有记录（不分页）"""
        result = await session.execute(select(cls).where(cls.status == status).order_by(col(cls.created_at).desc()))
        return result.scalars().all()

    @classmethod
    @with_session
    async def get_all_by_folder(
        cls,
        session: AsyncSession,
        folder: str,
        status: Optional[str] = None,
    ) -> Sequence["AiMemeRecord"]:
        """获取指定文件夹的所有记录（不分页）"""
        stmt = select(cls).where(cls.folder == folder)
        if status:
            stmt = stmt.where(cls.status == status)
        stmt = stmt.order_by(col(cls.created_at).desc())
        result = await session.execute(stmt)
        return result.scalars().all()

    @classmethod
    @with_session
    async def get_all_records_no_page(
        cls,
        session: AsyncSession,
        status: Optional[str] = None,
    ) -> Sequence["AiMemeRecord"]:
        """获取所有记录（不分页）"""
        if status:
            stmt = select(cls).where(cls.status == status)
        else:
            stmt = select(cls)
        stmt = stmt.order_by(col(cls.created_at).desc())
        result = await session.execute(stmt)
        return result.scalars().all()

    @classmethod
    @with_session
    async def random_pick(
        cls,
        session: AsyncSession,
        folder: str,
        exclude_ids: Optional[List[str]] = None,
    ) -> Optional["AiMemeRecord"]:
        """从指定文件夹随机选取一张已打标的表情包"""
        stmt = select(cls).where(
            cls.folder == folder,
            col(cls.status).in_(["tagged", "manual"]),
        )
        if exclude_ids:
            stmt = stmt.where(col(cls.meme_id).not_in(exclude_ids))

        stmt = stmt.order_by(func.random()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def least_used_pick(
        cls,
        session: AsyncSession,
        folder: str,
        exclude_ids: Optional[List[str]] = None,
    ) -> Optional["AiMemeRecord"]:
        """从指定文件夹选取最久未使用的已打标表情包"""
        stmt = select(cls).where(
            cls.folder == folder,
            col(cls.status).in_(["tagged", "manual"]),
        )
        if exclude_ids:
            stmt = stmt.where(col(cls.meme_id).not_in(exclude_ids))

        stmt = stmt.order_by(
            col(cls.last_used_at).asc().nullsfirst(),
            col(cls.use_count).asc(),
        ).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def record_usage(
        cls,
        session: AsyncSession,
        meme_id: str,
        group_id: str,
    ) -> None:
        """记录表情包使用"""
        result = await session.execute(select(cls).where(cls.meme_id == meme_id))
        record = result.scalar_one_or_none()
        if record is None:
            return
        record.use_count += 1
        record.last_used_at = datetime.now(timezone.utc)
        record.last_used_group = group_id
        record.updated_at = datetime.now(timezone.utc)
        session.add(record)

    @classmethod
    @with_session
    async def get_stats(
        cls,
        session: AsyncSession,
    ) -> dict:
        """获取统计概览"""
        # 总数
        total_result = await session.execute(select(func.count()).select_from(cls))
        total = total_result.scalar() or 0

        # 各状态数量
        status_counts: dict[str, int] = {}
        for s in ["pending", "tagged", "manual", "pending_manual", "rejected"]:
            r = await session.execute(select(func.count()).select_from(cls).where(cls.status == s))
            status_counts[s] = r.scalar() or 0

        # 各文件夹数量
        folder_result = await session.execute(select(cls.folder, func.count()).group_by(cls.folder))
        folder_counts = {row[0]: row[1] for row in folder_result.all()}

        # 总使用次数
        usage_result = await session.execute(select(func.coalesce(func.sum(cls.use_count), 0)))
        total_usage = usage_result.scalar() or 0

        # Top 10 最常用
        top_result = await session.execute(
            select(cls).where(col(cls.use_count) > 0).order_by(col(cls.use_count).desc()).limit(10)
        )
        top_memes = [
            {
                "meme_id": r.meme_id,
                "description": r.description,
                "use_count": r.use_count,
                "file_path": r.file_path,
            }
            for r in top_result.scalars().all()
        ]

        return {
            "total": total,
            "status_counts": status_counts,
            "folder_counts": folder_counts,
            "total_usage": total_usage,
            "top_memes": top_memes,
        }

    @classmethod
    @with_session
    async def count_daily_by_group(
        cls,
        session: AsyncSession,
        group_id: str,
        date_str: str,
    ) -> int:
        """统计某群某日的自动采集数量"""
        result = await session.execute(
            select(func.count())
            .select_from(cls)
            .where(
                cls.source_group == group_id,
                func.date(cls.created_at) == date_str,
            )
        )
        return result.scalar() or 0

    @classmethod
    @with_session
    async def search_by_ids(
        cls,
        session: AsyncSession,
        meme_ids: List[str],
    ) -> Sequence["AiMemeRecord"]:
        """根据 meme_id 列表搜索记录（用于 Qdrant 向量检索后补全元数据）"""
        if not meme_ids:
            return []
        result = await session.execute(select(cls).where(col(cls.meme_id).in_(meme_ids)))
        return result.scalars().all()
