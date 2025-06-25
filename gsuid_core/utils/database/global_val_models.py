from datetime import datetime

from sqlmodel import Field
from sqlalchemy import UniqueConstraint

from .base_models import BaseIDModel


class CoreDataSummary(BaseIDModel):
    __table_args__ = (
        UniqueConstraint(
            'date',
            name='record_summary',
        ),
        {'extend_existing': True},
    )

    receive: int = Field(title='接收次数', default=0)
    send: int = Field(title='发送次数', default=0)
    command: int = Field(title='指令调用次数', default=0)
    image: int = Field(title='图片生成次数', default=0)
    date: datetime = Field(title='日期')


# class CoreDataAnalysis(BaseIDModel, table=True):
class CoreDataAnalysis(BaseIDModel):
    __table_args__ = (
        UniqueConstraint(
            'date',
            'data_type',
            'user_id',
            'group_id',
            'command_name',
            name='record_analysis',
        ),
        {'extend_existing': True},
    )

    data_type: str = Field(title='数据类型')  # user or group
    user_id: int = Field(title='用户ID')
    group_id: int = Field(title='群ID')
    command_name: str = Field(title='指令名称')
    command_count: int = Field(title='指令调用次数', default=0)
    date: datetime = Field(title='日期')
