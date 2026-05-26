"""
通用持久状态存储 - 数据库模型

提供框架级别的键值存储能力，任何插件和任务场景都可以使用。
状态以 (scope, state_key) 作为唯一定位，value 存储任意 JSON 序列化数据。
"""

from typing import Optional
from datetime import datetime

from sqlmodel import Field, SQLModel, UniqueConstraint


class AIPersistentState(SQLModel, table=True):
    """
    通用持久状态表

    用于存储跨会话的结构化业务状态（如虚拟账户、任务进度、报名列表等）。
    与对话历史、记忆图谱不同，这里存放的是精确的、可被插件读写的键值数据。

    字段说明：
    - scope: 数据隔离范围，如 "user:123"、"group:456"、"private:xxx"、"global"
    - state_key: 业务键名，建议格式 "插件名:业务名"，如 "stock:portfolio"
    - value: JSON 序列化后的值（字符串、数字、列表、字典均可）
    - version: 乐观锁版本号，每次写入自增
    - expire_at: 可选的过期时间，为空表示永久保留
    """

    # (scope, state_key) 唯一约束：乐观锁依赖它保证并发首次写入只有一个成功，
    # 另一个收到 IntegrityError 后重试走 UPDATE 分支，避免产生重复行。
    __table_args__ = (
        UniqueConstraint("scope", "state_key", name="uq_state_scope_key"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True, title="序号")

    scope: str = Field(title="隔离范围", index=True)
    state_key: str = Field(title="业务键名", index=True)
    value: str = Field(title="JSON值", default="null")

    version: int = Field(title="版本号", default=1)

    created_at: datetime = Field(title="创建时间", default_factory=datetime.now)
    updated_at: datetime = Field(title="更新时间", default_factory=datetime.now)
    expire_at: Optional[datetime] = Field(title="过期时间", default=None, index=True)
