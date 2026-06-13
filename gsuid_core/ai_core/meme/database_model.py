"""表情包数据模型

AiMemeRecord 表情包主表，使用 SQLModel 直接定义。
使用 sha256(图片内容)[:16] 作为 meme_id 保证内容级去重。
不继承 BaseIDModel，因为 meme_id 本身就是主键。
"""

import json
from typing import List, Optional, Sequence
from datetime import datetime, timezone

from sqlmodel import JSON, Field, Column, SQLModel, col, select
from sqlalchemy import String, or_, cast, func
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import with_session


def _escape_like(text: str) -> str:
    """转义 LIKE 模式中的特殊字符（配合 escape='\\\\' 使用）"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _keyword_like_patterns(keyword: str) -> List[str]:
    """生成关键词在 JSON 列文本中可能出现的 LIKE 模式

    JSON 列的存储形态取决于序列化配置：默认 json.dumps(ensure_ascii=True)
    会把中文存成 \\uXXXX 转义；部分后端（如 PostgreSQL jsonb）存原文。
    两种形态都生成模式，保证预过滤不漏。
    """
    patterns = [f"%{_escape_like(keyword)}%"]
    escaped = json.dumps(keyword, ensure_ascii=True)[1:-1]
    if escaped != keyword:
        patterns.append(f"%{_escape_like(escaped)}%")
    return patterns


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

        # 各 persona_hint 数量
        # persona_hint 是 folder 的语义对应（common / persona_{name} 之外还可能存历史空字符串）
        persona_result = await session.execute(select(cls.persona_hint, func.count()).group_by(cls.persona_hint))
        persona_counts = {row[0]: row[1] for row in persona_result.all()}

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
            "persona_counts": persona_counts,
            "total_usage": total_usage,
            "top_memes": top_memes,
        }

    @classmethod
    @with_session
    async def get_distinct_personas(
        cls,
        session: AsyncSession,
    ) -> List[dict]:
        """返回当前库内出现过的 persona_hint 分类及其表情包数量

        返回项按数量降序、数量一致时按 persona_hint 升序：
        [
            {"persona_hint": "common", "count": 300, "folder": "common"},
            {"persona_hint": "早柚",   "count": 100, "folder": "persona_早柚"},
            ...
        ]

        幂等容错：会将空字符串归一为 "common"（与 folder ↔ persona_hint
        双向一致规则一致），以便前端按 persona 分类做入口时不需要额外判断。
        """
        result = await session.execute(select(cls.persona_hint, func.count()).group_by(cls.persona_hint))
        raw = {row[0] or "common": row[1] for row in result.all()}

        items: List[dict] = []
        for persona_hint, count in raw.items():
            folder = "common" if persona_hint == "common" else f"persona_{persona_hint}"
            items.append(
                {
                    "persona_hint": persona_hint,
                    "count": count,
                    "folder": folder,
                }
            )
        items.sort(key=lambda x: (-x["count"], x["persona_hint"]))
        return items

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
    ) -> List["AiMemeRecord"]:
        """根据 meme_id 列表搜索记录（用于 Qdrant 向量检索后补全元数据）

        保持 meme_ids 的原始顺序（即 Qdrant 相似度排序）。
        """
        if not meme_ids:
            return []
        result = await session.execute(select(cls).where(col(cls.meme_id).in_(meme_ids)))
        record_map = {r.meme_id: r for r in result.scalars().all()}
        # 保持 Qdrant 返回的相似度排序
        return [record_map[mid] for mid in meme_ids if mid in record_map]

    @classmethod
    @with_session
    async def get_by_persona(
        cls,
        session: AsyncSession,
        persona_hint: str,
        status: Optional[str] = None,
        sort: str = "created_at_desc",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence["AiMemeRecord"], int]:
        """按 persona_hint 获取记录列表，返回 (记录列表, 总数)

        Args:
            persona_hint: 归属人格标识（如 "common"、某 persona 名）。
                与 folder 的对应关系：
                - "common" → folder="common"
                - 其余值   → folder=f"persona_{persona_hint}"
                空字符串视为 "common"。
            status: 可选的状态过滤
            sort: 排序方式
            page: 页码
            page_size: 每页数量
        """
        persona_hint = persona_hint or "common"
        if persona_hint == "common":
            folder_value: Optional[str] = "common"
        else:
            folder_value = f"persona_{persona_hint}"

        # 一次会话内构造两条语句，复用计算
        stmt = select(cls).where(cls.folder == folder_value)
        if status:
            stmt = stmt.where(cls.status == status)
        count_stmt = select(func.count()).select_from(cls).where(cls.folder == folder_value)
        if status:
            count_stmt = count_stmt.where(cls.status == status)

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
    async def search_by_tags(
        cls,
        session: AsyncSession,
        tags: List[str],
        folder: Optional[str] = None,
        limit: int = 5,
    ) -> List["AiMemeRecord"]:
        """根据标签 / 描述精确匹配搜索表情包

        匹配规则（任意一个命中即返回）：
        1. emotion_tags / scene_tags / custom_tags 中包含任一关键词
        2. description 中包含任一关键词

        先用 SQL LIKE 做粗过滤（避免全表加载到内存），
        再在 Python 端做精确校验，兼容所有数据库后端。

        Args:
            tags: 要匹配的关键词列表
            folder: 可选的文件夹过滤
            limit: 返回数量

        Returns:
            匹配的 AiMemeRecord 列表（按使用次数降序）
        """
        if not tags:
            return []

        tag_set = set(tags)

        stmt = select(cls).where(col(cls.status).in_(["tagged", "manual"]))
        if folder:
            stmt = stmt.where(cls.folder == folder)

        # SQL 端 LIKE 粗过滤：description 直接匹配原文；
        # JSON 标签列匹配原文与 \uXXXX 转义两种存储形态
        like_conditions = []
        for kw in tags:
            raw_pattern = f"%{_escape_like(kw)}%"
            like_conditions.append(col(cls.description).like(raw_pattern, escape="\\"))
            for tag_col in (cls.emotion_tags, cls.scene_tags, cls.custom_tags):
                for pattern in _keyword_like_patterns(kw):
                    like_conditions.append(cast(tag_col, String).like(pattern, escape="\\"))
        stmt = stmt.where(or_(*like_conditions))
        stmt = stmt.order_by(col(cls.use_count).desc()).limit(max(limit * 20, 200))

        result = await session.execute(stmt)
        records = result.scalars().all()

        # Python 端精确校验：标签精确命中 或 description 包含关键词
        matched: List["AiMemeRecord"] = []
        for record in records:
            all_tags = set(record.emotion_tags + record.scene_tags + record.custom_tags)
            if tag_set & all_tags:
                matched.append(record)
            elif record.description and any(kw in record.description for kw in tags):
                matched.append(record)

        # 按使用次数降序排序
        matched.sort(key=lambda r: r.use_count, reverse=True)
        return matched[:limit]
