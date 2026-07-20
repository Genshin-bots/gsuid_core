import enum
from typing import Any, Dict, List, Optional, TypedDict
from datetime import date as ymddate, datetime, timedelta

from sqlmodel import Field, Index, col, func, delete, select
from sqlalchemy import UniqueConstraint, distinct
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Select

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


class BotTraffic(TypedDict):
    req: int
    max_qps: float
    total_count: int
    total_time: float
    max_time: float
    max_runtime: float
    max_wait_time: float
    max_runtime_func: str


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

    max_qps: float = Field(title="最大QPS", default=0)
    total_count: int = Field(title="总请求次数", default=0)
    total_time: float = Field(title="总耗时", default=0.0)
    max_time: float = Field(title="最大耗时", default=0.0)
    max_runtime: float = Field(title="最大运行耗时", default=0.0)
    max_wait_time: float = Field(title="最大等待耗时", default=0.0)
    max_runtime_func: str = Field(title="最大运行耗时函数", default="")

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
        获取最近45天的数据趋势。

        返回一个字典，包含：
        - 全平台 6 列 (receive/send/user_count/group_count/image/command)
        - 指定机器人 6 列（仅当传入了 bot_id 或 bot_self_id）

        优化：原实现跑两次查询（全平台聚合 + 指定机器人全量行），
        合并为一次按 (date, bot_id, bot_self_id) 分组的聚合查询，
        DB 往返从 2 次降到 1 次。
        """
        today = datetime.now().date()
        thirty_days_ago = today - timedelta(days=45)
        date_list = [thirty_days_ago + timedelta(days=i) for i in range(46)]

        # 单次查询：按 (date, bot_id, bot_self_id) 聚合
        # SQLAlchemy 2.0 select() 静态只给 1..10 列 overload, 9 列恰好匹配;
        # 实际 pyright 报 col/Mapped 类型不匹配 overload 形参；分两步构造更稳:
        #   1. select() 只接 3 个分组维度列, 拿到 Select[Tuple[date, str, str]];
        #   2. add_columns() 链式追加 6 个汇总列, 走增量化 overload。
        query: Select = (
            select(
                col(cls.date),
                col(cls.bot_id),
                col(cls.bot_self_id),
            )
            .add_columns(
                func.coalesce(func.sum(cls.receive), 0),
                func.coalesce(func.sum(cls.send), 0),
                func.coalesce(func.sum(cls.user_count), 0),
                func.coalesce(func.sum(cls.group_count), 0),
                func.coalesce(func.sum(cls.image), 0),
                func.coalesce(func.sum(cls.command), 0),
            )
            .where(col(cls.date) >= thirty_days_ago)
            .where(col(cls.date) < today)
            .group_by(col(cls.date), col(cls.bot_id), col(cls.bot_self_id))
            .order_by(col(cls.date))
        )
        rows = (await session.execute(query)).all()

        # 在 Python 里按日期分桶：全平台累加 + 单 bot 各自保留
        total_by_date: Dict[Any, List[int]] = {}
        bot_by_date: Dict[Any, List[int]] = {}

        for date_val, row_bot_id, row_bot_self_id, recv, send, ucnt, gcnt, img, cmd in rows:
            # 全平台汇总
            bucket = total_by_date.setdefault(
                date_val,
                [0, 0, 0, 0, 0, 0],
            )
            bucket[0] += recv or 0
            bucket[1] += send or 0
            bucket[2] += ucnt or 0
            bucket[3] += gcnt or 0
            bucket[4] += img or 0
            bucket[5] += cmd or 0

            # 指定机器人命中
            if (bot_id is None or row_bot_id == bot_id) and (bot_self_id is None or row_bot_self_id == bot_self_id):
                bot_by_date[date_val] = [
                    recv or 0,
                    send or 0,
                    ucnt or 0,
                    gcnt or 0,
                    img or 0,
                    cmd or 0,
                ]

        def fill(values_by_date: Dict[Any, List[int]], idx: int) -> List[int]:
            return [(values_by_date[d][idx] if d in values_by_date else 0) for d in date_list]

        result = {
            "all_bots_receive": fill(total_by_date, 0),
            "all_bots_send": fill(total_by_date, 1),
            "all_bots_user_count": fill(total_by_date, 2),
            "all_bots_group_count": fill(total_by_date, 3),
            "all_bots_image": fill(total_by_date, 4),
            "all_bots_command": fill(total_by_date, 5),
        }

        if bot_id is None and bot_self_id is None:
            return result

        result.update(
            {
                "bot_receive": fill(bot_by_date, 0),
                "bot_send": fill(bot_by_date, 1),
                "bot_user_count": fill(bot_by_date, 2),
                "bot_group_count": fill(bot_by_date, 3),
                "bot_image": fill(bot_by_date, 4),
                "bot_command": fill(bot_by_date, 5),
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

    @classmethod
    @with_session
    async def get_all_bots(
        cls,
        session: AsyncSession,
    ):
        """
        获取数据库中所有独立的bot（bot_id - bot_self_id对）
        返回格式: [{"bot_id": "xxx", "bot_self_id": "yyy"}, ...]
        """
        result = (
            select(col(cls.bot_id), col(cls.bot_self_id)).distinct().order_by(col(cls.bot_id), col(cls.bot_self_id))
        )
        r = await session.execute(result)
        rows = r.all()
        return [{"bot_id": row[0], "bot_self_id": row[1]} for row in rows]


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
