"""
AIScheduledTask 数据库模型

存储定时 AI 任务的持久化模型，确保任务在系统重启后不丢失。

参考 Subscribe 模型的设计，使用 Event 字段来管理发送所需的上下文信息。
"""

from typing import Optional
from datetime import datetime

from sqlmodel import Field

from gsuid_core.utils.database.base_models import BaseBotIDModel


class AIScheduledTask(BaseBotIDModel, table=True):
    """
    定时 AI 任务模型

    用于存储用户预约的 AI 任务，当时间到达时由子 Agent 执行。

    字段说明：
    - task_id: 唯一任务ID，用于 APScheduler 关联
    - trigger_time: 触发时间
    - task_prompt: 任务描述，包含需要查询的实体和返回格式
    - status: 任务状态，pending=待执行, executed=已执行, failed=执行失败
    - created_at: 创建时间
    - executed_at: 执行时间（完成后记录）
    - result: 执行结果（完成后记录）
    - error_message: 错误信息（失败时记录）

    Event 相关字段（用于执行时发送消息）：
    - user_id: 用户ID
    - group_id: 群ID（私聊则为空）
    - bot_self_id: 机器人自身ID
    - user_type: 用户类型 (group/direct)
    - WS_BOT_ID: WS机器人ID

    Persona 相关字段（用于执行时加载 persona）：
    - persona_name: 当时记录的 persona 名称
    - session_id: 当时记录的 session_id
    """

    task_id: str = Field(title="任务ID", index=True)

    # Event 相关字段
    user_id: str = Field(title="用户ID", index=True)
    group_id: Optional[str] = Field(title="群ID", default=None, index=True)
    bot_self_id: str = Field(title="机器人自身ID", default="")
    user_type: str = Field(title="用户类型", default="direct")
    WS_BOT_ID: Optional[str] = Field(title="WS机器人ID", default=None)

    # Persona 相关字段
    persona_name: Optional[str] = Field(title="Persona名称", default=None)
    session_id: str = Field(title="Session ID", default="")

    # 任务相关字段
    trigger_time: datetime = Field(title="触发时间", index=True)
    task_prompt: str = Field(title="任务描述")

    status: str = Field(
        title="状态",
        default="pending",
        index=True,
    )  # pending / executed / failed

    created_at: datetime = Field(title="创建时间", default_factory=datetime.now)
    executed_at: Optional[datetime] = Field(title="执行时间", default=None)

    result: Optional[str] = Field(title="执行结果", default=None)
    error_message: Optional[str] = Field(title="错误信息", default=None)

    class Config:
        json_schema_extra = {
            "example": {
                "task_id": "scheduled_task_abc123",
                "user_id": "user_001",
                "group_id": None,
                "bot_self_id": "123456",
                "user_type": "direct",
                "WS_BOT_ID": None,
                "persona_name": "default",
                "session_id": "onebot%%%private%%%user_001",
                "trigger_time": "2024-05-15 06:30:00",
                "task_prompt": "查询英伟达(NVDA)的实时股价和最新新闻并总结",
                "status": "pending",
                "created_at": "2024-05-14 22:00:00",
            }
        }
