import enum
from typing import Any, Dict, List, Optional, TypedDict
from datetime import date as ymddate, datetime, timedelta

from sqlmodel import Field, Index, col, func, delete, select
from sqlalchemy import UniqueConstraint, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from .base_models import BaseIDModel, with_session


class CountVal(TypedDict):
    DAU: str
    MAU: str
    DAU_MAU: str
    NewUser: str
    OutUser: str
    DAG: str
    MAG: str
    DAG_MAG: str
    NewGroup: str
    OutGroup: str


class DataType(enum.Enum):
    GROUP = "group"
    USER = "user"


class CoreTraffic(BaseIDModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "date",
            name="record_traffic",
        ),
        {"extend_existing": True},
    )

    max_qps: int = Field(title="最大QPS", default=0)
    date: ymddate = Field(title="日期")


class CoreDataSummary(BaseIDModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "date",
            "bot_id",
            "bot_self_id",
            name="record_summary",
        ),
        {"extend_existing": True},
    )

    receive: int = Field(title="接收次数", default=0)
    send: int = Field(title="发送次数", default=0)
    command: int = Field(title="指令调用次数", default=0)
    image: int = Field(title="图片生成次数", default=0)
    user_count: int = Field(title="用户数量", default=0)
    group_count: int = Field(title="群聊数量", default=0)
    bot_id: str = Field(title="机器人平台", max_length=64)
    bot_self_id: str = Field(title="机器人自身ID", max_length=64)
    date: ymddate = Field(title="日期")

    @classmethod
    @with_session
    async def delete_outdate(
        cls,
        session: AsyncSession,
        days: int = 300,
    ):
        """
        删除过期数据。
        """
        today = datetime.now().date()
        days_ago = today - timedelta(days=days)
        query = delete(cls).where(cls.date < days_ago)  # type: ignore
        await session.execute(query)
        await session.commit()

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
        thirty_days_ago = today - timedelta(days=45)
        date_list = [thirty_days_ago + timedelta(days=i) for i in range(46)]

        # --- 2. 准备两次查询 ---

        # 查询1: 全平台汇总数据
        agg_query = (
            select(
                col(cls.date),
                func.sum(cls.receive).label("total_receive"),
                func.sum(cls.send).label("total_send"),
                func.sum(cls.user_count).label("total_user_count"),
                func.sum(cls.group_count).label(
                    "total_group_count",
                ),  # type: ignore
                func.sum(cls.image).label("total_image"),  # type: ignore
                func.sum(cls.command).label("total_command"),  # type: ignore
            )
            .where(cls.date >= thirty_days_ago)
            .where(cls.date < today)  # 使用 < today 更精确
            .group_by(col(cls.date))
            .order_by(col(cls.date))
        )

        agg_rows = (await session.execute(agg_query)).all()

        # 处理全平台汇总数据，填充缺失日期为0
        agg_map = {row[0]: row for row in agg_rows}
        all_bots_receive = []
        all_bots_send = []
        all_bots_user_count = []
        all_bots_group_count = []
        all_bots_image = []
        all_bots_command = []

        for d in date_list:
            row = agg_map.get(d)
            if row:
                all_bots_receive.append(row[1] or 0)
                all_bots_send.append(row[2] or 0)
                all_bots_user_count.append(row[3] or 0)
                all_bots_group_count.append(row[4] or 0)
                all_bots_image.append(row[5] or 0)
                all_bots_command.append(row[6] or 0)
            else:
                all_bots_receive.append(0)
                all_bots_send.append(0)
                all_bots_user_count.append(0)
                all_bots_group_count.append(0)
                all_bots_image.append(0)
                all_bots_command.append(0)

        result = {
            "all_bots_receive": all_bots_receive,
            "all_bots_send": all_bots_send,
            "all_bots_user_count": all_bots_user_count,
            "all_bots_group_count": all_bots_group_count,
            "all_bots_image": all_bots_image,
            "all_bots_command": all_bots_command,
        }

        if bot_id is None and bot_self_id is None:
            return result

        # 查询2: 指定机器人数据
        filtered_query = select(cls).where(cls.date >= thirty_days_ago).where(cls.date < today).order_by(col(cls.date))
        # 动态添加过滤条件
        if bot_id:
            filtered_query = filtered_query.where(cls.bot_id == bot_id)
        if bot_self_id:
            filtered_query = filtered_query.where(cls.bot_self_id == bot_self_id)

        filtered_rows = (await session.execute(filtered_query)).scalars().all()

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
            bot_group_count.append(getattr(row, "group_count", 0) if row else 0)

        result.update(
            {
                "bot_receive": bot_receive,
                "bot_send": bot_send,
                "bot_image": bot_image,
                "bot_command": bot_command,
                "bot_user_count": bot_user_count,
                "bot_group_count": bot_group_count,
            }
        )

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
        result = select(col(cls.date)).distinct().order_by(col(cls.date).desc())
        r = await session.execute(result)
        return r.scalars().all()


class CoreDataAnalysis(BaseIDModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "date",
            "data_type",
            "target_id",
            "command_name",
            "bot_id",
            "bot_self_id",
            name="record_analysis",
        ),
        Index("ix_query_stats", "data_type", "bot_id", "bot_self_id", "date"),
        {"extend_existing": True},
    )

    data_type: DataType = Field(title="数据类型", default=DataType.USER, index=True, max_length=64)  # user or group
    target_id: str = Field(title="数据ID", index=True, max_length=64)
    command_name: str = Field(title="指令名称", max_length=100)
    command_count: int = Field(title="指令调用次数", default=0)
    date: ymddate = Field(title="日期", index=True)
    bot_id: str = Field(title="机器人平台", index=True, max_length=64)
    bot_self_id: str = Field(title="机器人自身ID", index=True, max_length=64)

    @classmethod
    @with_session
    async def delete_outdate(
        cls,
        session: AsyncSession,
        days: int = 300,
    ):
        """
        删除过期数据。
        """
        today = datetime.now().date()
        days_ago = today - timedelta(days=days)
        query = delete(cls).where(cls.date < days_ago)  # type: ignore
        await session.execute(query)
        await session.commit()

    # TODO
    # 几个版本之后删除
    @classmethod
    @with_session
    async def update_summary(
        cls,
        session: AsyncSession,
    ):
        # 查询最近6天内所有记录，统计唯一 target_id 数量
        recent_days = ymddate.today() - timedelta(days=6)
        result = (
            select(
                col(cls.date),
                col(cls.bot_id),
                col(cls.bot_self_id),
                col(cls.data_type),
                func.count(distinct(col(cls.target_id))).label("count"),
            )  # type: ignore
            .where(cls.date >= recent_days)
            .group_by(
                col(cls.date),
                col(cls.bot_id),
                col(cls.bot_self_id),
                col(cls.data_type),
            )
        )
        rows = (await session.execute(result)).all()

        # 聚合数据，按(date, bot_id, bot_self_id)分组
        summary_map = {}
        for date, bot_id, bot_self_id, data_type, count in rows:
            key = (date, bot_id, bot_self_id)
            if key not in summary_map:
                summary_map[key] = {
                    "date": date,
                    "bot_id": bot_id,
                    "bot_self_id": bot_self_id,
                    "user_count": 0,
                    "group_count": 0,
                }
            if data_type == DataType.USER:
                summary_map[key]["user_count"] = count
            elif data_type == DataType.GROUP:
                summary_map[key]["group_count"] = count

        # 构造 CoreDataSummary 实例并批量更新
        insert_summary = []
        for v in summary_map.values():
            insert_summary.append(
                CoreDataSummary(
                    date=v["date"],
                    bot_id=v["bot_id"],
                    bot_self_id=v["bot_self_id"],
                    user_count=v["user_count"],
                    group_count=v["group_count"],
                )
            )
        if insert_summary:
            await CoreDataSummary.batch_insert_data_with_update(
                insert_summary,
                ["user_count", "group_count"],
                ["date", "bot_id", "bot_self_id"],
            )

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
        stats["dau_dag"] = avg_result.scalar_one_or_none() or 0.0

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
            past_targets_query = past_targets_query.where(cls.bot_self_id == bot_self_id)

        new_targets_query = select(func.count(distinct(col(cls.target_id)))).where(
            cls.data_type == data_type,
            cls.date == today,
            col(cls.target_id).not_in(past_targets_query),  # type: ignore
        )
        if bot_id:
            new_targets_query = new_targets_query.where(cls.bot_id == bot_id)
        if bot_self_id:
            new_targets_query = new_targets_query.where(cls.bot_self_id == bot_self_id)

        new_targets_count_result = await session.execute(new_targets_query)
        stats["new"] = new_targets_count_result.scalar_one()

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
            recent_active_subquery = recent_active_subquery.where(cls.bot_id == bot_id)
        if bot_self_id:
            recent_active_subquery = recent_active_subquery.where(cls.bot_self_id == bot_self_id)

        out_targets_query = select(func.count(distinct(col(cls.target_id)))).where(
            cls.data_type == data_type,
            cls.date >= thirty_days_ago,
            cls.date < today,
            col(cls.target_id).not_in(recent_active_subquery),
        )

        if bot_id:
            out_targets_query = out_targets_query.where(cls.bot_id == bot_id)
        if bot_self_id:
            out_targets_query = out_targets_query.where(cls.bot_self_id == bot_self_id)

        out_targets_result = await session.execute(out_targets_query)
        out_targets_count = out_targets_result.scalar_one()

        # The denominator query for the rate calculation
        total_targets_in_30_days_query = select(func.count(distinct(col(cls.target_id)))).where(
            cls.data_type == data_type,
            cls.date >= thirty_days_ago,
            cls.date < today,
        )
        if bot_id:
            total_targets_in_30_days_query = total_targets_in_30_days_query.where(cls.bot_id == bot_id)
        if bot_self_id:
            total_targets_in_30_days_query = total_targets_in_30_days_query.where(cls.bot_self_id == bot_self_id)

        total_targets_result = await session.execute(total_targets_in_30_days_query)
        total_targets_count = total_targets_result.scalar_one()

        stats["mau"] = total_targets_count

        if total_targets_count > 0:
            stats["stickiness"] = (stats["dau_dag"] / total_targets_count) * 100
        else:
            stats["stickiness"] = 0.0

        # Calculate rate
        out_rate = (out_targets_count / total_targets_count * 100) if total_targets_count > 0 else 0
        stats["out_rate"] = out_rate

        return stats

    @classmethod
    async def calculate_dashboard_metrics(
        cls,
        bot_id: Optional[str] = None,
        bot_self_id: Optional[str] = None,
    ) -> CountVal:
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
        result_data: CountVal = {
            # 用户侧
            "DAU": f"{user_stats['dau_dag']:.2f}",
            "MAU": str(user_stats["mau"]),
            "DAU_MAU": f"{user_stats['stickiness']:.2f}%",
            "NewUser": str(user_stats["new"]),
            "OutUser": f"{user_stats['out_rate']:.2f}%",
            # 群组侧
            "DAG": f"{group_stats['dau_dag']:.2f}",
            "MAG": str(group_stats["mau"]),
            "DAG_MAG": f"{group_stats['stickiness']:.2f}%",
            "NewGroup": str(group_stats["new"]),
            "OutGroup": f"{group_stats['out_rate']:.2f}%",
        }

        return result_data
