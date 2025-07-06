import enum
from datetime import date as ymddate
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, Index, col, func, select
from sqlalchemy import (
    UniqueConstraint,
    distinct,
)

from .base_models import BaseIDModel, with_session


class DataType(enum.Enum):
    GROUP = 'group'
    USER = 'user'


class CoreDataSummary(BaseIDModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            'date',
            'bot_id',
            'bot_self_id',
            name='record_summary',
        ),
        {'extend_existing': True},
    )

    receive: int = Field(title='接收次数', default=0)
    send: int = Field(title='发送次数', default=0)
    command: int = Field(title='指令调用次数', default=0)
    image: int = Field(title='图片生成次数', default=0)
    user_count: int = Field(title='用户数量', default=0)
    group_count: int = Field(title='群聊数量', default=0)
    bot_id: str = Field(title='机器人平台', max_length=64)
    bot_self_id: str = Field(title='机器人自身ID', max_length=64)
    date: ymddate = Field(title='日期')

    @classmethod
    @with_session
    async def get_day_trends(
        cls,
        session: AsyncSession,
        bot_id: Optional[str] = None,
        bot_self_id: Optional[str] = None,
    ) -> Dict[str, List[int]]:
        """
        获取最近30天的数据趋势。

        返回一个字典，包含四份数据列表：
        1. all_bots_receive: 全平台所有机器人的每日接收数汇总列表。
        2. all_bots_send: 全平台所有机器人的每日发送数汇总列表。
        3. bot_receive: 指定机器人的每日接收数列表。
        4. bot_send: 指定机器人的每日发送数列表。
        """
        # 1. 定义时间范围
        today = datetime.now().date()
        thirty_days_ago = today - timedelta(days=30)
        date_list = [thirty_days_ago + timedelta(days=i) for i in range(30)]

        # --- 2. 准备两次查询 ---

        # 查询1: 全平台汇总数据
        agg_query = (
            select(
                col(cls.date),
                func.sum(cls.receive).label("total_receive"),
                func.sum(cls.send).label("total_send"),
                func.sum(cls.user_count).label("total_user_count"),
            )
            .where(cls.date >= thirty_days_ago)
            .where(cls.date < today)  # 使用 < today 更精确
            .group_by(col(cls.date))
            .order_by(col(cls.date))
        )

        # 查询2: 指定机器人数据
        filtered_query = (
            select(cls)
            .where(cls.date >= thirty_days_ago)
            .where(cls.date < today)
            .order_by(col(cls.date))
        )
        # 动态添加过滤条件
        if bot_id:
            filtered_query = filtered_query.where(cls.bot_id == bot_id)
        if bot_self_id:
            filtered_query = filtered_query.where(
                cls.bot_self_id == bot_self_id
            )

        agg_rows = (await session.execute(agg_query)).all()
        filtered_rows = (await session.execute(filtered_query)).scalars().all()

        # 处理全平台汇总数据，填充缺失日期为0
        agg_map = {row[0]: row for row in agg_rows}
        all_bots_receive = []
        all_bots_send = []
        all_bots_user_count = []
        for d in date_list:
            row = agg_map.get(d)
            if row:
                all_bots_receive.append(row[1] or 0)
                all_bots_send.append(row[2] or 0)
                all_bots_user_count.append(row[3] or 0)
            else:
                all_bots_receive.append(0)
                all_bots_send.append(0)
                all_bots_user_count.append(0)

        # 处理指定机器人数据，填充缺失日期为0
        filtered_map = {row.date: row for row in filtered_rows}
        bot_receive = []
        bot_send = []
        bot_image = []
        bot_command = []
        bot_user_count = []
        bot_group_count = []
        for d in date_list:
            row = filtered_map.get(d)
            bot_receive.append(getattr(row, "receive", 0) if row else 0)
            bot_send.append(getattr(row, "send", 0) if row else 0)
            bot_image.append(getattr(row, "image", 0) if row else 0)
            bot_command.append(getattr(row, "command", 0) if row else 0)
            bot_user_count.append(getattr(row, "user_count", 0) if row else 0)
            bot_group_count.append(
                getattr(row, "group_count", 0) if row else 0
            )

        result = {
            "all_bots_receive": all_bots_receive,
            "all_bots_send": all_bots_send,
            "all_bots_user_count": all_bots_user_count,
            "bot_receive": bot_receive,
            "bot_send": bot_send,
            "bot_image": bot_image,
            "bot_command": bot_command,
            "bot_user_count": bot_user_count,
            "bot_group_count": bot_group_count,
        }

        return result

    @classmethod
    @with_session
    async def get_recently_data(
        cls,
        session: AsyncSession,
        recently_day_ago: ymddate,
    ):
        result = select(cls).where(
            cls.date >= recently_day_ago,
        )
        r = await session.execute(result)
        return r.scalars().all()

    @classmethod
    @with_session
    async def get_yesterday_data(
        cls,
        session: AsyncSession,
        bot_id: Optional[str] = None,
        bot_self_id: Optional[str] = None,
    ):
        yesterday = datetime.now() - timedelta(days=1)
        result = select(cls).where(
            cls.date == yesterday,
        )
        if bot_id:
            result = result.where(cls.bot_id == bot_id)
        if bot_self_id:
            result = result.where(cls.bot_self_id == bot_self_id)

        r = await session.execute(result)
        return r.scalars().one_or_none()

    @classmethod
    @with_session
    async def get_distinct_date_data(
        cls,
        session: AsyncSession,
    ):
        result = (
            select(col(cls.date)).distinct().order_by(col(cls.date).desc())
        )
        r = await session.execute(result)
        return r.scalars().all()


class CoreDataAnalysis(BaseIDModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            'date',
            'data_type',
            'target_id',
            'command_name',
            'bot_id',
            'bot_self_id',
            name='record_analysis',
        ),
        Index('ix_query_stats', 'data_type', 'bot_id', 'bot_self_id', 'date'),
        {'extend_existing': True},
    )

    data_type: DataType = Field(
        title='数据类型', default=DataType.USER, index=True, max_length=64
    )  # user or group
    target_id: str = Field(title='数据ID', index=True, max_length=64)
    command_name: str = Field(title='指令名称', max_length=100)
    command_count: int = Field(title='指令调用次数', default=0)
    date: ymddate = Field(title='日期', index=True)
    bot_id: str = Field(title='机器人平台', index=True, max_length=64)
    bot_self_id: str = Field(title='机器人自身ID', index=True, max_length=64)

    @classmethod
    @with_session
    async def get_recently_data(
        cls,
        session: AsyncSession,
        recently_day_ago: ymddate,
    ):
        result = select(cls).where(
            cls.date >= recently_day_ago,
        )
        r = await session.execute(result)
        return r.scalars().all()

    @classmethod
    @with_session
    async def get_sp_data(
        cls,
        session: AsyncSession,
        recently_day_ago: ymddate,
        bot_id: Optional[str] = None,
        bot_self_id: Optional[str] = None,
    ):
        result = select(cls).where(
            cls.date == recently_day_ago,
        )
        if bot_id:
            result = result.where(cls.bot_id == bot_id)
        if bot_self_id:
            result = result.where(cls.bot_self_id == bot_self_id)

        r = await session.execute(result)
        return r.scalars().all()

    @classmethod
    @with_session
    async def _get_stats_for_type(
        cls,
        session: AsyncSession,
        data_type: DataType,
        today: ymddate,
        thirty_days_ago: ymddate,
        seven_days_ago: ymddate,
        bot_id: Optional[str] = None,
        bot_self_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        stats = {}

        # --- DAU/DAG Calculation (This part was correct) ---
        dau_query = select(
            col(cls.date),
            func.count(distinct(col(cls.target_id))).label("daily_count"),
        )
        dau_query = dau_query.where(
            cls.data_type == data_type,
            cls.date >= thirty_days_ago,
            cls.date < today,
        )
        if bot_id:
            dau_query = dau_query.where(cls.bot_id == bot_id)
        if bot_self_id:
            dau_query = dau_query.where(cls.bot_self_id == bot_self_id)

        daily_active_subquery = dau_query.group_by(col(cls.date)).subquery()
        avg_daily_query = select(func.avg(daily_active_subquery.c.daily_count))
        avg_result = await session.execute(avg_daily_query)
        stats['dau_dag'] = avg_result.scalar_one_or_none() or 0.0

        twenty_nine_days_ago = today - timedelta(days=29)

        past_targets_query = (
            select(cls.target_id)
            .where(
                cls.data_type == data_type,
                cls.date >= twenty_nine_days_ago,
                cls.date < today,
            )
            .distinct()
        )
        if bot_id:
            past_targets_query = past_targets_query.where(
                cls.bot_id == bot_id,
            )
        if bot_self_id:
            past_targets_query = past_targets_query.where(
                cls.bot_self_id == bot_self_id
            )

        new_targets_query = select(
            func.count(distinct(col(cls.target_id)))
        ).where(
            cls.data_type == data_type,
            cls.date == today,
            col(cls.target_id).not_in(past_targets_query),  # type: ignore
        )
        if bot_id:
            new_targets_query = new_targets_query.where(cls.bot_id == bot_id)
        if bot_self_id:
            new_targets_query = new_targets_query.where(
                cls.bot_self_id == bot_self_id
            )

        new_targets_count_result = await session.execute(new_targets_query)
        stats['new'] = new_targets_count_result.scalar_one()

        # --- OU/OG Calculation (Corrected Logic) ---
        recent_active_subquery = (
            select(col(cls.target_id))
            .where(
                cls.data_type == data_type,
                cls.date >= seven_days_ago,
                cls.date < today,
            )
            .distinct()
        )

        if bot_id:
            recent_active_subquery = recent_active_subquery.where(
                cls.bot_id == bot_id
            )
        if bot_self_id:
            recent_active_subquery = recent_active_subquery.where(
                cls.bot_self_id == bot_self_id
            )

        out_targets_query = select(
            func.count(distinct(col(cls.target_id)))
        ).where(
            cls.data_type == data_type,
            cls.date >= thirty_days_ago,
            cls.date < today,
            col(cls.target_id).not_in(recent_active_subquery),
        )

        if bot_id:
            out_targets_query = out_targets_query.where(cls.bot_id == bot_id)
        if bot_self_id:
            out_targets_query = out_targets_query.where(
                cls.bot_self_id == bot_self_id
            )

        out_targets_result = await session.execute(out_targets_query)
        out_targets_count = out_targets_result.scalar_one()

        # The denominator query for the rate calculation
        total_targets_in_30_days_query = select(
            func.count(distinct(col(cls.target_id)))
        ).where(
            cls.data_type == data_type,
            cls.date >= thirty_days_ago,
            cls.date < today,
        )
        if bot_id:
            total_targets_in_30_days_query = (
                total_targets_in_30_days_query.where(cls.bot_id == bot_id)
            )
        if bot_self_id:
            total_targets_in_30_days_query = (
                total_targets_in_30_days_query.where(
                    cls.bot_self_id == bot_self_id
                )
            )

        total_targets_result = await session.execute(
            total_targets_in_30_days_query
        )
        total_targets_count = total_targets_result.scalar_one()

        # Calculate rate
        out_rate = (
            (out_targets_count / total_targets_count * 100)
            if total_targets_count > 0
            else 0
        )
        stats['out_rate'] = out_rate

        return stats

    @classmethod
    async def calculate_dashboard_metrics(
        cls,
        bot_id: Optional[str] = None,
        bot_self_id: Optional[str] = None,
    ) -> Dict[str, str]:
        today = ymddate.today()
        thirty_days_ago = today - timedelta(days=30)
        seven_days_ago = today - timedelta(days=7)

        user_stats = await cls._get_stats_for_type(
            DataType.USER,
            today,
            thirty_days_ago,
            seven_days_ago,
            bot_id,
            bot_self_id,
        )

        # 计算群组相关指标
        group_stats = await cls._get_stats_for_type(
            DataType.GROUP,
            today,
            thirty_days_ago,
            seven_days_ago,
            bot_id,
            bot_self_id,
        )

        # 格式化并返回最终结果
        result_data = {
            'DAU': f"{user_stats['dau_dag']:.2f}",
            'DAG': f"{group_stats['dau_dag']:.2f}",
            'NU': str(user_stats["new"]),
            'OU': f"{user_stats['out_rate']:.2f}%",
            'NG': str(group_stats["new"]),
            'OG': f"{group_stats['out_rate']:.2f}%",
        }

        return result_data
