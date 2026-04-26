"""
AI Core 数据库模型模块

定义 AI Agent 相关的数据模型，包括用户好感度等。
复用 gsuid_core 的数据库基础设施。
"""

import time
from typing import List, Optional

from sqlmodel import Field, col, and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import BaseModel, with_session


class UserFavorability(BaseModel, table=True):
    """
    用户好感度表

    存储用户与 AI 角色之间的人际关系数据。
    好感度范围通常为 -100 到 100+，影响角色的对话风格和行为。

    Attributes:
        user_id: 用户ID
        bot_id: 机器人ID
        user_name: 用户名
        favorability: 好感度值 (范围约 -100 到 100+)
        interaction_count: 交互次数
        last_interaction_time: 最后交互时间戳
        memory_count: 记忆条数
        tags: 用户标签，JSON格式存储
    """

    __table_args__ = {"extend_existing": True}

    user_name: Optional[str] = Field(default="", title="用户名")
    favorability: int = Field(default=0, title="好感度")
    interaction_count: int = Field(default=0, title="交互次数")
    last_interaction_time: int = Field(default=0, title="最后交互时间戳")
    memory_count: int = Field(default=0, title="记忆条数")
    tags: Optional[str] = Field(default="[]", title="用户标签")

    @property
    def relationship_level(self) -> str:
        """
        根据好感度返回关系等级描述

        Returns:
            关系等级字符串
        """
        if self.favorability < -50:
            return "厌恶"
        elif self.favorability < -10:
            return "冷淡"
        elif self.favorability < 10:
            return "陌生"
        elif self.favorability < 50:
            return "认识"
        elif self.favorability < 80:
            return "熟人"
        elif self.favorability < 100:
            return "朋友"
        else:
            return "挚友"

    @classmethod
    @with_session
    async def get_user_favorability(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
    ) -> Optional["UserFavorability"]:
        """
        获取用户好感度信息

        Args:
            session: 数据库会话
            user_id: 用户ID
            bot_id: 机器人ID

        Returns:
            UserFavorability 对象，如果不存在则返回 None
        """
        stmt = select(cls).where(and_(cls.user_id == user_id, cls.bot_id == bot_id))
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def create_user_favorability(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        user_name: str = "",
    ) -> int:
        """
        创建用户好感度记录

        Args:
            session: 数据库会话
            user_id: 用户ID
            bot_id: 机器人ID
            user_name: 用户名

        Returns:
            插入成功的行数
        """
        try:
            await session.merge(
                cls(
                    user_id=user_id,
                    bot_id=bot_id,
                    user_name=user_name or user_id,
                    favorability=0,
                    interaction_count=0,
                    last_interaction_time=int(time.time()),
                )
            )
            await session.commit()
            logger.info(f"🧠 [UserFavorability] 创建用户好感度记录: {user_id}")
            return 1
        except Exception as e:
            logger.exception(f"🧠 [UserFavorability] 创建用户好感度记录失败: {e}")
            return 0

    @classmethod
    async def get_or_create_user_favorability(
        cls,
        user_id: str,
        bot_id: str,
        user_name: str = "",
    ) -> "UserFavorability":
        """
        获取或创建用户好感度记录

        Args:
            user_id: 用户ID
            bot_id: 机器人ID
            user_name: 用户名

        Returns:
            UserFavorability 对象
        """
        existing = await cls.get_user_favorability(user_id, bot_id)
        if existing:
            return existing

        # 创建新记录
        await cls.create_user_favorability(user_id, bot_id, user_name)
        result = await cls.get_user_favorability(user_id, bot_id)
        if result is None:
            raise ValueError(f"Failed to create user favorability record for {user_id}")
        return result

    @classmethod
    async def update_favorability(
        cls,
        user_id: str,
        bot_id: str,
        delta: int,
        user_name: str = "",
    ) -> bool:
        """
        更新用户好感度（增量）

        Args:
            user_id: 用户ID
            bot_id: 机器人ID
            delta: 好感度变化值（可为负数）
            user_name: 用户名（可选）

        Returns:
            是否更新成功
        """
        try:
            # 确保记录存在
            record = await cls.get_or_create_user_favorability(user_id, bot_id, user_name)

            new_value = record.favorability + delta
            stmt = (
                update(cls)
                .where(
                    and_(
                        cls.user_id == user_id,
                        cls.bot_id == bot_id,
                    )
                )
                .values(
                    favorability=new_value,
                    interaction_count=record.interaction_count + 1,
                    last_interaction_time=int(time.time()),
                )
            )

            from gsuid_core.utils.database.base_models import async_maker

            async with async_maker() as session:
                await session.execute(stmt)
                await session.commit()

            logger.info(f"🧠 [UserFavorability] 更新用户 {user_id} 好感度: {record.favorability} -> {new_value}")
            return True
        except Exception as e:
            logger.exception(f"🧠 [UserFavorability] 更新好感度失败: {e}")
            return False

    @classmethod
    async def set_favorability(
        cls,
        user_id: str,
        bot_id: str,
        value: int,
        user_name: str = "",
    ) -> bool:
        """
        设置用户好感度（绝对值）

        Args:
            user_id: 用户ID
            bot_id: 机器人ID
            value: 好感度目标值
            user_name: 用户名（可选）

        Returns:
            是否设置成功
        """
        try:
            # 确保记录存在
            record = await cls.get_or_create_user_favorability(user_id, bot_id, user_name)

            stmt = (
                update(cls)
                .where(
                    and_(
                        cls.user_id == user_id,
                        cls.bot_id == bot_id,
                    )
                )
                .values(
                    favorability=value,
                    interaction_count=record.interaction_count + 1,
                    last_interaction_time=int(time.time()),
                )
            )

            from gsuid_core.utils.database.base_models import async_maker

            async with async_maker() as session:
                await session.execute(stmt)
                await session.commit()

            logger.info(f"🧠 [UserFavorability] 设置用户 {user_id} 好感度: {value}")
            return True
        except Exception as e:
            logger.exception(f"🧠 [UserFavorability] 设置好感度失败: {e}")
            return False

    @classmethod
    @with_session
    async def update_interaction(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
    ) -> bool:
        """
        更新用户交互次数

        Args:
            session: 数据库会话
            user_id: 用户ID
            bot_id: 机器人ID

        Returns:
            是否更新成功
        """
        try:
            stmt = select(cls).where(and_(cls.user_id == user_id, cls.bot_id == bot_id))
            result = await session.execute(stmt)
            record = result.scalars().first()

            if not record:
                return False

            stmt = (
                update(cls)
                .where(
                    and_(
                        cls.user_id == user_id,
                        cls.bot_id == bot_id,
                    )
                )
                .values(
                    interaction_count=record.interaction_count + 1,
                    last_interaction_time=int(time.time()),
                )
            )
            await session.execute(stmt)
            await session.commit()

            return True
        except Exception as e:
            logger.exception(f"🧠 [UserFavorability] 更新交互次数失败: {e}")
            return False

    @classmethod
    @with_session
    async def update_memory_count(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        count: int,
    ) -> bool:
        """
        更新用户记忆条数

        Args:
            session: 数据库会话
            user_id: 用户ID
            bot_id: 机器人ID
            count: 记忆条数

        Returns:
            是否更新成功
        """
        try:
            stmt = (
                update(cls)
                .where(
                    and_(
                        cls.user_id == user_id,
                        cls.bot_id == bot_id,
                    )
                )
                .values(memory_count=count)
            )
            await session.execute(stmt)
            await session.commit()

            return True
        except Exception as e:
            logger.exception(f"🧠 [UserFavorability] 更新记忆条数失败: {e}")
            return False

    @classmethod
    async def increment_memory_count(
        cls,
        user_id: str,
        bot_id: str,
    ) -> bool:
        """
        增加用户记忆条数

        Args:
            user_id: 用户ID
            bot_id: 机器人ID

        Returns:
            是否更新成功
        """
        try:
            record = await cls.get_user_favorability(user_id, bot_id)
            if not record:
                return False

            return await cls.update_memory_count(user_id, bot_id, record.memory_count + 1)
        except Exception as e:
            logger.exception(f"🧠 [UserFavorability] 增加记忆条数失败: {e}")
            return False

    @classmethod
    @with_session
    async def get_all_user_favorability(
        cls,
        session: AsyncSession,
        bot_id: str,
    ) -> List["UserFavorability"]:
        """
        获取所有用户的好感度信息

        Args:
            session: 数据库会话
            bot_id: 机器人ID

        Returns:
            用户好感度列表
        """
        try:
            stmt = select(cls).where(cls.bot_id == bot_id)
            result = await session.execute(stmt)
            records = result.scalars().all()
            return list(records)
        except Exception as e:
            logger.exception(f"🧠 [UserFavorability] 获取所有用户好感度失败: {e}")
            return []

    @classmethod
    @with_session
    async def get_top_favorability_users(
        cls,
        session: AsyncSession,
        bot_id: str,
        limit: int = 10,
    ) -> List["UserFavorability"]:
        """
        获取好感度最高的用户列表

        Args:
            session: 数据库会话
            bot_id: 机器人ID
            limit: 返回数量限制

        Returns:
            好感度最高的用户列表
        """
        try:
            stmt = select(cls).where(cls.bot_id == bot_id).order_by(col(cls.favorability).desc()).limit(limit)
            result = await session.execute(stmt)
            records = result.scalars().all()
            return list(records)
        except Exception as e:
            logger.exception(f"🧠 [UserFavorability] 获取高好感度用户失败: {e}")
            return []
