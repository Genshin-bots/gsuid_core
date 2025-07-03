from datetime import date as ymddate

from sqlmodel import Field, select
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from .base_models import BaseIDModel, with_session


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
    bot_id: str = Field(title='机器人平台')
    bot_self_id: str = Field(title='机器人自身ID')
    date: ymddate = Field(title='日期')

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
        return r.all()


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
        {'extend_existing': True},
    )

    data_type: str = Field(
        title='数据类型', default='unknown'
    )  # user or group
    target_id: str = Field(title='数据ID')
    command_name: str = Field(title='指令名称')
    command_count: int = Field(title='指令调用次数', default=0)
    date: ymddate = Field(title='日期')
    bot_id: str = Field(title='机器人平台')
    bot_self_id: str = Field(title='机器人自身ID')

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
        return r.all()
