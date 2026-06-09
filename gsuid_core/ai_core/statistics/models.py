"""
AI Core Statistics 数据库模型

定义 AI 模块统计数据的数据模型，包括：
- Token 消耗统计（按模型、按维度）
- API 费用估算
- Session 内存占用
- Persona 排行榜
- 触发方式占比
- 用户/群组活跃榜
- 响应延迟统计
- 意图分布统计
- 失败率/错误码统计
- Heartbeat 决策统计
- RAG 知识库效果统计
"""

import time
from typing import Any, Optional
from datetime import datetime

from sqlmodel import Field, col, and_, select
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import BaseIDModel, with_session


class AIDailyStatistics(BaseIDModel, table=True):
    """
    每日 AI 统计数据表

    存储每日聚合的 AI 统计数据，包括 Token 消耗、费用、延迟等。

    Attributes:
        date: 统计日期 (YYYY-MM-DD)
        total_input_tokens: 总输入 Token 数
        total_output_tokens: 总输出 Token 数
        avg_latency: 平均响应延迟 (秒)
        p95_latency: P95 响应延迟 (秒)
        intent_chat_count: 闲聊意图次数
        intent_tool_count: 工具意图次数
        intent_qa_count: 问答意图次数
        api_timeout_count: API 超时次数
        api_rate_limit_count: Rate Limit 次数
        api_529_count: API 负载过高次数
        api_network_error_count: 网络错误次数
        active_session_count: 活跃 Session 数
        avg_messages_per_session: 平均每 Session 消息数
        trigger_mention_count: @机器人触发次数
        trigger_keyword_count: 关键词触发次数
        trigger_heartbeat_count: 主动巡检触发次数

        memory_observations: 记忆观察入队总数
        memory_ingestions: 摄入完成总数
        memory_ingestion_errors: 摄入失败总数
        memory_retrievals: 检索请求总数
        memory_entities_created: 新建 Entity 总数
        memory_edges_created: 新建 Edge 总数
        memory_episodes_created: 新建 Episode 总数
    """

    __table_args__ = {"extend_existing": True}

    date: str = Field(default="", title="统计日期")
    total_input_tokens: int = Field(default=0, title="总输入Token")
    total_output_tokens: int = Field(default=0, title="总输出Token")
    total_cache_read_tokens: int = Field(default=0, title="总缓存读取Token")
    total_cache_write_tokens: int = Field(default=0, title="总缓存写入Token")
    avg_latency: float = Field(default=0.0, title="平均延迟(秒)")
    p95_latency: float = Field(default=0.0, title="P95延迟(秒)")
    intent_chat_count: int = Field(default=0, title="闲聊次数")
    intent_tool_count: int = Field(default=0, title="工具次数")
    intent_qa_count: int = Field(default=0, title="问答次数")
    api_timeout_count: int = Field(default=0, title="API超时次数")
    api_rate_limit_count: int = Field(default=0, title="RateLimit次数")
    api_529_count: int = Field(default=0, title="API负载过高次数")
    api_network_error_count: int = Field(default=0, title="网络错误次数")
    api_usage_limit_count: int = Field(default=0, title="使用限制次数")
    api_agent_error_count: int = Field(default=0, title="Agent执行错误次数")
    active_session_count: int = Field(default=0, title="活跃Session数")
    avg_messages_per_session: float = Field(default=0.0, title="平均每Session消息数")
    trigger_mention_count: int = Field(default=0, title="@触发次数")
    trigger_keyword_count: int = Field(default=0, title="关键词触发次数")
    trigger_heartbeat_count: int = Field(default=0, title="主动巡检触发次数")
    trigger_scheduled_count: int = Field(default=0, title="定时任务触发次数")
    # 记忆系统统计
    memory_observations: int = Field(default=0, title="记忆观察入队数")
    memory_ingestions: int = Field(default=0, title="记忆摄入完成数")
    memory_ingestion_errors: int = Field(default=0, title="记忆摄入失败数")
    memory_extraction_errors: int = Field(default=0, title="记忆提取失败数")
    memory_retrievals: int = Field(default=0, title="记忆检索请求数")
    memory_entities_created: int = Field(default=0, title="新建Entity数")
    memory_edges_created: int = Field(default=0, title="新建Edge数")
    memory_episodes_created: int = Field(default=0, title="新建Episode数")
    created_at: int = Field(default=0, title="创建时间戳")
    updated_at: int = Field(default=0, title="更新时间戳")

    @classmethod
    def get_today_date(cls) -> str:
        """获取今天的日期字符串"""
        return datetime.now().strftime("%Y-%m-%d")

    @classmethod
    @with_session
    async def get_daily_stats(
        cls,
        session: AsyncSession,
        date: str,
    ) -> Optional["AIDailyStatistics"]:
        """获取指定日期的统计数据"""
        stmt = select(cls).where(and_(cls.date == date))
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def upsert_daily_stats(
        cls,
        session: AsyncSession,
        date: str,
        **kwargs,
    ) -> bool:
        """创建或更新每日统计数据"""
        try:
            existing = await cls.get_daily_stats(date)
            if existing:
                await cls.update_data_by_data(
                    select_data={"date": date},
                    update_data={**kwargs, "updated_at": int(time.time())},
                )
            else:
                await cls.full_insert_data(
                    date=date,
                    created_at=int(time.time()),
                    updated_at=int(time.time()),
                    **kwargs,
                )
            return True
        except Exception as e:
            logger.exception(f"📊 [AIDailyStatistics] 更新统计数据失败: {e}")
            return False


class AITokenUsageByType(BaseIDModel, table=True):
    """
    按使用类型分组的 Token 消耗统计
    """

    __table_args__ = (
        UniqueConstraint("date", "chat_type", name="aitokenusagebytype_date"),
        {"extend_existing": True},
    )

    date: str = Field(default="", title="统计日期")
    chat_type: str = Field(default="", title="消耗类型")
    input_tokens: int = Field(default=0, title="输入Token")
    output_tokens: int = Field(default=0, title="输出Token")
    cache_read_tokens: int = Field(default=0, title="缓存读取Token")
    cache_write_tokens: int = Field(default=0, title="缓存写入Token")

    @classmethod
    @with_session
    async def get_daily_data(
        cls,
        session: AsyncSession,
        date: str,
    ) -> list["AITokenUsageByType"]:
        """获取指定日期的统计数据"""
        stmt = select(cls).where(and_(cls.date == date))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def get_by_type(
        cls,
        session: AsyncSession,
        date: str,
        chat_type: str,
    ) -> Optional["AITokenUsageByType"]:
        """获取指定类型在某日的统计"""
        stmt = select(cls).where(
            and_(
                cls.date == date,
                cls.chat_type == chat_type,
            )
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def upsert_token_usage(
        cls,
        session: AsyncSession,
        date: str,
        chat_type: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> bool:
        """创建或更新 Token 使用统计"""
        try:
            existing = await cls.get_by_type(date, chat_type)
            if existing:
                await cls.update_data_by_data(
                    select_data={"date": date, "chat_type": chat_type},
                    update_data={
                        "input_tokens": existing.input_tokens + input_tokens,
                        "output_tokens": existing.output_tokens + output_tokens,
                        "cache_read_tokens": existing.cache_read_tokens + cache_read_tokens,
                        "cache_write_tokens": existing.cache_write_tokens + cache_write_tokens,
                    },
                )
            else:
                await cls.full_insert_data(
                    date=date,
                    chat_type=chat_type,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                )
            return True
        except Exception as e:
            logger.exception(f"📊 [AITokenUsageByType] 更新Token消耗失败: {e}")
            return False


class AITokenUsageByModel(BaseIDModel, table=True):
    """
    按模型分组的 Token 消耗统计
    """

    __table_args__ = (
        UniqueConstraint("date", "model_name", name="aitokenusagebydate_model"),
        {"extend_existing": True},
    )

    date: str = Field(default="", title="统计日期")
    model_name: str = Field(default="", title="模型名称")
    input_tokens: int = Field(default=0, title="输入Token")
    output_tokens: int = Field(default=0, title="输出Token")
    cache_read_tokens: int = Field(default=0, title="缓存读取Token")
    cache_write_tokens: int = Field(default=0, title="缓存写入Token")

    @classmethod
    @with_session
    async def get_daily_data(
        cls,
        session: AsyncSession,
        date: str,
    ) -> list["AITokenUsageByModel"]:
        """获取指定日期的统计数据"""
        stmt = select(cls).where(and_(cls.date == date))
        result = await session.execute(stmt)
        return list(result.scalars().all()) or []

    @classmethod
    @with_session
    async def get_by_model(
        cls,
        session: AsyncSession,
        date: str,
        model_name: str,
    ) -> Optional["AITokenUsageByModel"]:
        """获取指定模型在某日的统计"""
        stmt = select(cls).where(
            and_(
                cls.date == date,
                cls.model_name == model_name,
            )
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def upsert_token_usage(
        cls,
        session: AsyncSession,
        date: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> bool:
        """创建或更新 Token 使用统计"""
        try:
            existing = await cls.get_by_model(date, model_name)
            if existing:
                await cls.update_data_by_data(
                    select_data={"date": date, "model_name": model_name},
                    update_data={
                        "input_tokens": existing.input_tokens + input_tokens,
                        "output_tokens": existing.output_tokens + output_tokens,
                        "cache_read_tokens": existing.cache_read_tokens + cache_read_tokens,
                        "cache_write_tokens": existing.cache_write_tokens + cache_write_tokens,
                    },
                )
            else:
                await cls.full_insert_data(
                    date=date,
                    model_name=model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                )
            return True
        except Exception as e:
            logger.exception(f"📊 [AITokenUsageByModel] 更新Token消耗失败: {e}")
            return False


class AIGroupUserActivityStats(BaseIDModel, table=True):
    """
    群组/用户活跃统计
    """

    __table_args__ = (
        UniqueConstraint("date", "group_id", "user_id", name="aiactivitybydate_group_user"),
        {"extend_existing": True},
    )

    date: str = Field(default="", title="统计日期")
    group_id: str = Field(default="", title="群组ID")
    user_id: str = Field(default="", title="用户ID")
    ai_interaction_count: int = Field(default=0, title="AI互动次数")
    message_count: int = Field(default=0, title="消息总数")

    @classmethod
    @with_session
    async def get_by_user(
        cls,
        session: AsyncSession,
        date: str,
        group_id: str,
        user_id: str,
    ) -> Optional["AIGroupUserActivityStats"]:
        """获取指定用户在某日的统计"""
        stmt = select(cls).where(
            and_(
                cls.date == date,
                cls.group_id == group_id,
                cls.user_id == user_id,
            )
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def get_daily_data(
        cls,
        session: AsyncSession,
        date: str,
    ) -> list["AIGroupUserActivityStats"]:
        """获取指定日期的所有活跃统计数据"""
        stmt = select(cls).where(and_(cls.date == date))
        result = await session.execute(stmt)
        return list(result.scalars().all()) or []

    @classmethod
    @with_session
    async def upsert_activity(
        cls,
        session: AsyncSession,
        date: str,
        group_id: str,
        user_id: str,
        ai_interaction_count: int = 0,
        message_count: int = 0,
    ) -> bool:
        """创建或更新活跃统计"""
        try:
            existing = await cls.get_by_user(date, group_id, user_id)
            if existing:
                await cls.update_data_by_data(
                    select_data={"date": date, "group_id": group_id, "user_id": user_id},
                    update_data={
                        "ai_interaction_count": existing.ai_interaction_count + ai_interaction_count,
                        "message_count": existing.message_count + message_count,
                    },
                )
            else:
                await cls.full_insert_data(
                    date=date,
                    group_id=group_id,
                    user_id=user_id,
                    ai_interaction_count=ai_interaction_count,
                    message_count=message_count,
                )
            return True
        except Exception as e:
            logger.exception(f"📊 [AIGroupUserActivityStats] 更新活跃统计失败: {e}")
            return False


class AIHeartbeatMetrics(BaseIDModel, table=True):
    """
    Heartbeat 巡检详细指标
    """

    __table_args__ = (
        UniqueConstraint("date", "group_id", name="aiheartbeatbydate_group"),
        {"extend_existing": True},
    )

    date: str = Field(default="", title="统计日期")
    group_id: str = Field(default="", title="群组ID")
    should_speak_count: int = Field(default=0, title="应该发言次数")
    should_not_speak_count: int = Field(default=0, title="不应该发言次数")

    @classmethod
    @with_session
    async def get_by_group(
        cls,
        session: AsyncSession,
        date: str,
        group_id: str,
    ) -> Optional["AIHeartbeatMetrics"]:
        """获取指定群组在某日的统计"""
        stmt = select(cls).where(
            and_(
                cls.date == date,
                cls.group_id == group_id,
            )
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def get_daily_data(
        cls,
        session: AsyncSession,
        date: str,
    ) -> list["AIHeartbeatMetrics"]:
        """获取指定日期的所有 Heartbeat 统计数据"""
        stmt = select(cls).where(and_(cls.date == date))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def upsert_heartbeat_decision(
        cls,
        session: AsyncSession,
        date: str,
        group_id: str,
        should_speak: bool,
    ) -> bool:
        """创建或更新 Heartbeat 决策统计"""
        try:
            existing = await cls.get_by_group(date, group_id)
            if existing:
                update_data = {}
                if should_speak:
                    update_data["should_speak_count"] = existing.should_speak_count + 1
                else:
                    update_data["should_not_speak_count"] = existing.should_not_speak_count + 1
                await cls.update_data_by_data(
                    select_data={"date": date, "group_id": group_id},
                    update_data=update_data,
                )
            else:
                await cls.full_insert_data(
                    date=date,
                    group_id=group_id,
                    should_speak_count=1 if should_speak else 0,
                    should_not_speak_count=1 if not should_speak else 0,
                )
            return True
        except Exception as e:
            logger.exception(f"📊 [AIHeartbeatMetrics] 更新Heartbeat决策失败: {e}")
            return False


class AIRAGMissStatistics(BaseIDModel, table=True):
    """
    RAG 未命中统计（简单计数）
    """

    __table_args__ = {"extend_existing": True}

    date: str = Field(default="", title="统计日期")
    hit_count: int = Field(default=0, title="命中次数")
    miss_count: int = Field(default=0, title="未命中次数")

    @classmethod
    @with_session
    async def get_daily_data(cls, session: AsyncSession, date: str) -> Optional["AIRAGMissStatistics"]:
        """获取指定日期的统计"""
        stmt = select(cls).where(and_(cls.date == date))
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def upsert_rag_miss(cls, session: AsyncSession, date: str) -> bool:
        """创建或更新 RAG 未命中统计（仅增加 miss 计数）"""
        try:
            existing = await cls.get_daily_data(date)
            if existing:
                await cls.update_data_by_data(
                    select_data={"date": date},
                    update_data={"miss_count": existing.miss_count + 1},
                )
            else:
                await cls.full_insert_data(date=date, miss_count=1)
            return True
        except Exception as e:
            logger.exception(f"📊 [AIRAGMissStatistics] 更新RAG未命中统计失败: {e}")
            return False

    @classmethod
    @with_session
    async def upsert_rag_stats(cls, session: AsyncSession, date: str, hit_count: int, miss_count: int) -> bool:
        """创建或更新 RAG 统计数据（设置绝对值）"""
        try:
            existing = await cls.get_daily_data(date)
            if existing:
                await cls.update_data_by_data(
                    select_data={"date": date},
                    update_data={"hit_count": hit_count, "miss_count": miss_count},
                )
            else:
                await cls.full_insert_data(date=date, hit_count=hit_count, miss_count=miss_count)
            return True
        except Exception as e:
            logger.exception(f"📊 [AIRAGMissStatistics] 更新RAG统计失败: {e}")
            return False


class AIRAGDocumentStatistics(BaseIDModel, table=True):
    """
    RAG 文档命中统计（按文档名统计）
    """

    __table_args__ = {"extend_existing": True}

    document_name: str = Field(default="", title="文档名称")
    hit_count: int = Field(default=0, title="命中次数")

    @classmethod
    @with_session
    async def get_by_document(cls, session: AsyncSession, document_name: str) -> Optional["AIRAGDocumentStatistics"]:
        """获取指定文档的统计"""
        stmt = select(cls).where(cls.document_name == document_name)
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def get_all_data(cls, session: AsyncSession) -> list["AIRAGDocumentStatistics"]:
        """获取所有文档统计"""
        stmt = select(cls)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def upsert_rag_hit(cls, session: AsyncSession, document_name: str) -> bool:
        """创建或更新 RAG 命中统计（仅增加计数）"""
        try:
            existing = await cls.get_by_document(document_name)
            if existing:
                await cls.update_data_by_data(
                    select_data={"document_name": document_name},
                    update_data={"hit_count": existing.hit_count + 1},
                )
            else:
                await cls.full_insert_data(document_name=document_name, hit_count=1)
            return True
        except Exception as e:
            logger.exception(f"📊 [AIRAGDocumentStatistics] 更新RAG命中统计失败: {e}")
            return False

    @classmethod
    @with_session
    async def upsert_rag_hit_count(cls, session: AsyncSession, document_name: str, hit_count: int) -> bool:
        """创建或更新 RAG 命中统计（设置绝对值）"""
        try:
            existing = await cls.get_by_document(document_name)
            if existing:
                await cls.update_data_by_data(
                    select_data={"document_name": document_name},
                    update_data={"hit_count": hit_count},
                )
            else:
                await cls.full_insert_data(document_name=document_name, hit_count=hit_count)
            return True
        except Exception as e:
            logger.exception(f"📊 [AIRAGDocumentStatistics] 更新RAG命中统计失败: {e}")
            return False


class AIHourlyPerformance(BaseIDModel, table=True):
    """
    按小时分组的 AI 性能与 Token 消耗统计
    用于流式请求下的 TTFT、TPS、Token 消耗、工具调用次数等小时级聚合。
    """

    __table_args__ = (
        UniqueConstraint("date", "hour", "provider", "model_name", name="aihourlyperf_date_hour_provider_model"),
        {"extend_existing": True},
    )

    date: str = Field(default="", title="统计日期")
    hour: int = Field(default=0, title="小时(0-23)")
    provider: str = Field(default="", title="模型提供商")
    model_name: str = Field(default="", title="模型名称")

    # TTFT/TPS 统计（极值 + 总和/有效样本数，便于跨批次合并后仍能算均值）
    # sample_count 只统计 >0 的有效样本；min/max 以 sample_count == 0 作为未赋值判据
    request_count: int = Field(default=0, title="请求次数")
    ttft_min_ms: float = Field(default=0.0, title="TTFT最小值(ms)")
    ttft_max_ms: float = Field(default=0.0, title="TTFT最大值(ms)")
    ttft_sum_ms: float = Field(default=0.0, title="TTFT总和(ms)")
    ttft_sample_count: int = Field(default=0, title="TTFT有效样本数")
    tps_min: float = Field(default=0.0, title="TPS最小值(tokens/s)")
    tps_max: float = Field(default=0.0, title="TPS最大值(tokens/s)")
    tps_sum: float = Field(default=0.0, title="TPS总和(tokens/s)")
    tps_sample_count: int = Field(default=0, title="TPS有效样本数")

    # Token 消耗
    input_tokens: int = Field(default=0, title="输入Token")
    output_tokens: int = Field(default=0, title="输出Token")
    cache_read_tokens: int = Field(default=0, title="缓存读取Token")
    cache_write_tokens: int = Field(default=0, title="缓存写入Token")

    # 工具调用
    tool_call_count: int = Field(default=0, title="工具调用次数")

    created_at: int = Field(default=0, title="创建时间戳")
    updated_at: int = Field(default=0, title="更新时间戳")

    @classmethod
    @with_session
    async def get_hourly_data(
        cls,
        session: AsyncSession,
        date: str,
        hour: int,
        provider: str,
        model_name: str,
    ) -> Optional["AIHourlyPerformance"]:
        """获取指定日期-小时-提供商-模型的统计"""
        stmt = select(cls).where(
            and_(
                cls.date == date,
                cls.hour == hour,
                cls.provider == provider,
                cls.model_name == model_name,
            )
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def get_daily_data(
        cls,
        session: AsyncSession,
        date: str,
    ) -> list["AIHourlyPerformance"]:
        """获取指定日期的所有小时统计数据"""
        stmt = select(cls).where(cls.date == date).order_by(col(cls.hour).asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def get_range_data(
        cls,
        session: AsyncSession,
        start_date: str,
        end_date: str,
    ) -> list["AIHourlyPerformance"]:
        """获取指定日期范围的所有小时统计数据"""
        stmt = (
            select(cls)
            .where(
                and_(
                    cls.date >= start_date,
                    cls.date <= end_date,
                )
            )
            .order_by(col(cls.date).asc(), col(cls.hour).asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def upsert_performance(
        cls,
        session: AsyncSession,
        date: str,
        hour: int,
        provider: str,
        model_name: str,
        request_count: int = 0,
        ttft_min_ms: float = 0.0,
        ttft_max_ms: float = 0.0,
        ttft_sum_ms: float = 0.0,
        ttft_sample_count: int = 0,
        tps_min: float = 0.0,
        tps_max: float = 0.0,
        tps_sum: float = 0.0,
        tps_sample_count: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        tool_call_count: int = 0,
    ) -> bool:
        """创建或更新小时性能统计

        入参为一批增量聚合（来自内存缓冲），与已有行做合并：
        计数与总和累加，min/max 取两侧极值。调用方负责在成功后清空对应增量，
        否则增量语义会重复累加。
        """
        try:
            existing = await cls.get_hourly_data(date, hour, provider, model_name)
            if existing:
                update_data: dict[str, Any] = {
                    "request_count": existing.request_count + request_count,
                    "input_tokens": existing.input_tokens + input_tokens,
                    "output_tokens": existing.output_tokens + output_tokens,
                    "cache_read_tokens": existing.cache_read_tokens + cache_read_tokens,
                    "cache_write_tokens": existing.cache_write_tokens + cache_write_tokens,
                    "tool_call_count": existing.tool_call_count + tool_call_count,
                    "updated_at": int(time.time()),
                }
                # TTFT 合并：以 sample_count == 0 判定已有行是否有有效样本
                if ttft_sample_count > 0:
                    update_data["ttft_min_ms"] = (
                        ttft_min_ms if existing.ttft_sample_count == 0 else min(existing.ttft_min_ms, ttft_min_ms)
                    )
                    update_data["ttft_max_ms"] = max(existing.ttft_max_ms, ttft_max_ms)
                    update_data["ttft_sum_ms"] = existing.ttft_sum_ms + ttft_sum_ms
                    update_data["ttft_sample_count"] = existing.ttft_sample_count + ttft_sample_count
                # TPS 合并
                if tps_sample_count > 0:
                    update_data["tps_min"] = (
                        tps_min if existing.tps_sample_count == 0 else min(existing.tps_min, tps_min)
                    )
                    update_data["tps_max"] = max(existing.tps_max, tps_max)
                    update_data["tps_sum"] = existing.tps_sum + tps_sum
                    update_data["tps_sample_count"] = existing.tps_sample_count + tps_sample_count

                await cls.update_data_by_data(
                    select_data={
                        "date": date,
                        "hour": hour,
                        "provider": provider,
                        "model_name": model_name,
                    },
                    update_data=update_data,
                )
            else:
                now = int(time.time())
                await cls.full_insert_data(
                    date=date,
                    hour=hour,
                    provider=provider,
                    model_name=model_name,
                    request_count=request_count,
                    ttft_min_ms=ttft_min_ms,
                    ttft_max_ms=ttft_max_ms,
                    ttft_sum_ms=ttft_sum_ms,
                    ttft_sample_count=ttft_sample_count,
                    tps_min=tps_min,
                    tps_max=tps_max,
                    tps_sum=tps_sum,
                    tps_sample_count=tps_sample_count,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    tool_call_count=tool_call_count,
                    created_at=now,
                    updated_at=now,
                )
            return True
        except Exception as e:
            logger.exception(f"📊 [AIHourlyPerformance] 更新小时性能统计失败: {e}")
            return False
