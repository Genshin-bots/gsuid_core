"""
AI Core Statistics Manager
AI 模块统计管理器

负责收集、聚合和持久化 AI 模块的各类统计数据。
支持每日数据持久化（启动/关闭/零点重置）。
"""

import asyncio
from typing import Any, Dict, List, Optional
from datetime import datetime
from collections import Counter, defaultdict

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.ai_core.statistics.models import (
    AIDailyStatistics,
    AIHeartbeatMetrics,
    AITokenUsageByType,
    AIHourlyPerformance,
    AIRAGMissStatistics,
    AITokenUsageByModel,
    AIRAGDocumentStatistics,
    AIGroupUserActivityStats,
)

from .dataclass_models import BotState, LatencyStats, HourlyPerformanceEntry

# 小时级性能统计的内存缓冲 key: (date, hour, provider, model_name)
HourlyPerfKey = tuple[str, int, str, str]


class StatisticsManager:
    """
    AI 模块统计管理器
    负责收集、聚合和持久化 AI 模块的各类统计数据。
    """

    _instance: Optional["StatisticsManager"] = None
    INTENT_MAP = {"闲聊": "chat", "工具": "tool", "问答": "qa"}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._bot_state: BotState = BotState()  # 全局统计状态
        self._today: str = datetime.now().strftime("%Y-%m-%d")
        self._rag: Dict[str, Any] = {"hit": 0, "miss": 0, "documents": {}}
        # Why: 启动期 _persist_loop (cron */30 + misfire补偿) 可能在 _load_today_data_from_db
        #      完成前抢先 fire, 把空的 _bot_state 写回 DB, 抹掉当日 AIDailyStatistics 的 total_*,
        #      却保留 by_model/by_type 表 (空 dict 走 batch_insert 直接 return), 造成
        #      total < sum(by_model) = sum(by_type) 的永久偏差。用 _loaded 闸门确保
        #      "未加载完毕一律不许 persist"。
        self._loaded: bool = False
        # Why: _persist_loop 与日切 _scheduled_ai_core_reset 在 00:00 会同时触发;
        #      reset 内 "持久化 → 清空 → 切日期" 三步若被并发 persist 切片, 可能把空状态
        #      回写到当日 Day N 行。用同一把锁串行化 persist / 日切, 保证原子性。
        self._persist_lock: asyncio.Lock = asyncio.Lock()
        # 小时级性能统计（内存缓冲, 只保留尚未持久化的增量）
        # key: (date, hour, provider, model_name)
        self._hourly_perf: Dict[HourlyPerfKey, HourlyPerformanceEntry] = defaultdict(HourlyPerformanceEntry)

    def _reset_daily_counters(self):
        """重置每日计数器"""
        self._bot_state = BotState()
        # _hourly_perf 不在此清空: key 自带日期维度, persist 成功即出队,
        # 此刻仍残留的只可能是落库失败的增量, 保留到下一轮 persist 重试即可。

    def record_hourly_performance(
        self,
        provider: str,
        model_name: str,
        ttft_ms: float = 0.0,
        tps: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        tool_call_count: int = 0,
    ) -> None:
        """记录一次模型请求的小时级性能统计

        Args:
            provider: 模型提供商标识（如 openai、anthropic）
            model_name: 模型名称
            ttft_ms: 首字延迟（毫秒），0 表示本次无有效样本
            tps: 每秒生成 Token 数，0 表示本次无有效样本
            input_tokens: 输入 Token 数
            output_tokens: 输出 Token 数
            cache_read_tokens: 缓存读取 Token 数
            cache_write_tokens: 缓存写入 Token 数
            tool_call_count: 本次请求中的工具调用次数
        """
        now = datetime.now()
        key: HourlyPerfKey = (now.strftime("%Y-%m-%d"), now.hour, provider, model_name)
        self._hourly_perf[key].update(
            ttft_ms=ttft_ms,
            tps=tps,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            tool_call_count=tool_call_count,
        )

    @staticmethod
    def _hourly_entry_to_dict(provider: str, model: str, entry: HourlyPerformanceEntry) -> Dict[str, Any]:
        """将一条小时级聚合转换为 API 返回的 provider 维度字典"""
        return {
            "provider": provider,
            "model": model,
            "request_count": entry.request_count,
            "ttft_min_ms": entry.ttft_ms_min,
            "ttft_max_ms": entry.ttft_ms_max,
            "ttft_avg_ms": entry.ttft_ms_avg,
            "tps_min": entry.tps_min,
            "tps_max": entry.tps_max,
            "tps_avg": entry.tps_avg,
            "input_tokens": entry.input_tokens,
            "output_tokens": entry.output_tokens,
            "cache_read_tokens": entry.cache_read_tokens,
            "cache_write_tokens": entry.cache_write_tokens,
            "tool_call_count": entry.tool_call_count,
        }

    @staticmethod
    def _hourly_row_to_entry(row: AIHourlyPerformance) -> HourlyPerformanceEntry:
        """将 DB 行还原为内存聚合结构，便于与内存增量复用同一套合并逻辑"""
        return HourlyPerformanceEntry(
            request_count=row.request_count,
            ttft_ms_min=row.ttft_min_ms,
            ttft_ms_max=row.ttft_max_ms,
            ttft_ms_sum=row.ttft_sum_ms,
            ttft_sample_count=row.ttft_sample_count,
            tps_min=row.tps_min,
            tps_max=row.tps_max,
            tps_sum=row.tps_sum,
            tps_sample_count=row.tps_sample_count,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cache_read_tokens=row.cache_read_tokens,
            cache_write_tokens=row.cache_write_tokens,
            tool_call_count=row.tool_call_count,
        )

    def record_token_usage(
        self,
        model_name: str,
        chat_type: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ):
        """记录 Token 使用量"""
        self._bot_state.token_by_model[model_name.lower()].add(
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
        self._bot_state.token_by_type[chat_type.lower()].add(
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
        self._bot_state.total_tokens.update(
            input=input_tokens,
            output=output_tokens,
            cache_read=cache_read_tokens,
            cache_write=cache_write_tokens,
        )

    def record_latency(self, latency: float):
        """记录响应延迟"""
        self._bot_state.latencies.add(latency)

    def record_intent(self, intent: str):
        """记录意图"""
        mapped_intent = self.INTENT_MAP.get(intent, intent)
        self._bot_state.intents[mapped_intent] += 1

    def record_error(self, error_type: str):
        """记录错误"""
        self._bot_state.errors[error_type] += 1

    def record_trigger(self, trigger_type: str):
        """记录触发方式"""
        self._bot_state.triggers[trigger_type] += 1

    def record_heartbeat_decision(self, group_id: str, should_speak: bool):
        """记录 Heartbeat 决策"""
        key = "should_speak_true" if should_speak else "should_speak_false"
        self._bot_state.heartbeats[group_id][key] += 1

    def record_activity(
        self,
        group_id: str,
        user_id: Optional[str],
        ai_interaction_count: int,
        message_count: int,
    ):
        """记录用户活动"""
        key = f"{group_id}:{user_id}"
        self._bot_state.activities[key].update(ai_interaction=ai_interaction_count, message=message_count)

    def record_rag_hit(self, document_id: str, document_name: str):
        """记录 RAG 命中"""
        self._rag["documents"][document_id] = document_name
        self._rag["hit"] += 1

    def record_rag_miss(self):
        """记录 RAG 未命中"""
        self._rag["miss"] += 1

    # ==================== 记忆系统统计 ====================

    def record_memory_observation(self, count: int = 1):
        """记录记忆观察入队"""
        self._bot_state.memory_observations += count

    def record_memory_ingestion(self, count: int = 1):
        """记录记忆摄入完成"""
        self._bot_state.memory_ingestions += count

    def record_memory_ingestion_error(self, count: int = 1):
        """记录记忆摄入失败"""
        self._bot_state.memory_ingestion_errors += count

    def record_memory_retrieval(self, count: int = 1):
        """记录记忆检索请求"""
        self._bot_state.memory_retrievals += count

    def record_memory_entity_created(self, count: int = 1):
        """记录新建 Entity"""
        self._bot_state.memory_entities_created += count

    def record_memory_edge_created(self, count: int = 1):
        """记录新建 Edge"""
        self._bot_state.memory_edges_created += count

    def record_memory_episode_created(self, count: int = 1):
        """记录新建 Episode"""
        self._bot_state.memory_episodes_created += count

    def record_memory_extraction_error(self, count: int = 1):
        """记录记忆提取失败"""
        self._bot_state.memory_extraction_errors += count

    # ==================== 数据查询与聚合 ====================

    def get_summary(self) -> Dict[str, Any]:
        """获取统计摘要"""
        b = self._bot_state

        agg_tokens = b.total_tokens
        agg_intents = b.intents
        agg_errors = b.errors
        agg_triggers = b.triggers

        # 聚合嵌套字典数据
        agg_heartbeats = Counter()
        agg_activities: Dict[str, Counter] = defaultdict(Counter)
        all_latencies = b.latencies.latencies

        for h in b.heartbeats.values():
            agg_heartbeats.update(h)
        for u_key, u_val in b.activities.items():
            agg_activities[u_key].update(u_val)

        total_intents = sum(agg_intents.values()) or 1
        total_triggers = sum(agg_triggers.values()) or 1
        hb_true, hb_false = agg_heartbeats["should_speak_true"], agg_heartbeats["should_speak_false"]

        active_users = sorted(
            [
                {
                    "group_id": k.split(":")[0],
                    "user_id": k.split(":")[1],
                    "ai_interaction": v["ai_interaction"],
                    "message_count": v["message"],
                }
                for k, v in agg_activities.items()
            ],
            key=lambda x: x["ai_interaction"],
            reverse=True,
        )[:20]

        return {
            "date": self._today,
            "token_usage": {
                "total_input_tokens": agg_tokens["input"],
                "total_output_tokens": agg_tokens["output"],
                "total_cache_read_tokens": agg_tokens["cache_read"],
                "total_cache_write_tokens": agg_tokens["cache_write"],
                "by_model": [
                    {
                        "model": m,
                        "input_tokens": u.input_tokens,
                        "output_tokens": u.output_tokens,
                        "cache_read_tokens": u.cache_read_tokens,
                        "cache_write_tokens": u.cache_write_tokens,
                    }
                    for m, u in b.token_by_model.items()
                ],
                "by_type": [
                    {
                        "type": t,
                        "input_tokens": u.input_tokens,
                        "output_tokens": u.output_tokens,
                        "cache_read_tokens": u.cache_read_tokens,
                        "cache_write_tokens": u.cache_write_tokens,
                    }
                    for t, u in b.token_by_type.items()
                ],
            },
            "latency": {"avg": LatencyStats(all_latencies).avg, "p95": LatencyStats(all_latencies).p95},
            "intent_distribution": {
                k: {"count": agg_intents[k], "percentage": agg_intents[k] / total_intents * 100}
                for k in ["chat", "tool", "qa"]
            },
            "errors": {
                "timeout": agg_errors.get("timeout", 0),
                "rate_limit": agg_errors.get("rate_limit", 0),
                "network_error": agg_errors.get("network_error", 0),
                "usage_limit": agg_errors.get("usage_limit", 0),
                "agent_error": agg_errors.get("agent_error", 0),
                "api_529_error": agg_errors.get("api_529_error", 0),
                "total": sum(agg_errors.values()),
            },
            "heartbeat": {
                "should_speak_true": hb_true,
                "should_speak_false": hb_false,
                "conversion_rate": hb_true / (hb_true + hb_false) * 100 if (hb_true + hb_false) > 0 else 0,
            },
            "trigger_distribution": {
                k: {"count": agg_triggers.get(k, 0), "percentage": agg_triggers.get(k, 0) / total_triggers * 100}
                for k in ["mention", "keyword", "followup", "heartbeat", "scheduled"]
            },
            "rag": {
                "hit_count": self._rag["hit"],
                "miss_count": self._rag["miss"],
                "hit_rate": self._rag["hit"] / (self._rag["hit"] + self._rag["miss"] or 1) * 100,
            },
            "memory": {
                "observations": b.memory_observations,
                "ingestions": b.memory_ingestions,
                "ingestion_errors": b.memory_ingestion_errors,
                "extraction_errors": b.memory_extraction_errors,
                "retrievals": b.memory_retrievals,
                "entities_created": b.memory_entities_created,
                "edges_created": b.memory_edges_created,
                "episodes_created": b.memory_episodes_created,
            },
            "active_users": active_users,
        }

    # ==================== 数据库持久化 ====================

    async def _load_today_data_from_db(self):
        """从数据库加载今日数据"""
        try:
            today = self._today
            logger.info(f"📊 [StatisticsManager] 正在从数据库加载 {today} 的统计数据")

            # 1. 加载 AIDailyStatistics
            stats = await AIDailyStatistics.get_daily_stats(today)
            if stats:
                s = self._bot_state
                s.total_tokens.update(
                    input=stats.total_input_tokens or 0,
                    output=stats.total_output_tokens or 0,
                    cache_read=stats.total_cache_read_tokens or 0,
                    cache_write=stats.total_cache_write_tokens or 0,
                )
                if stats.avg_latency:
                    s.latencies.add(stats.avg_latency)
                s.intents.update(
                    chat=stats.intent_chat_count or 0, tool=stats.intent_tool_count or 0, qa=stats.intent_qa_count or 0
                )
                s.errors.update(
                    timeout=stats.api_timeout_count or 0,
                    rate_limit=stats.api_rate_limit_count or 0,
                    api_529_error=stats.api_529_count or 0,
                    network_error=stats.api_network_error_count or 0,
                    usage_limit=stats.api_usage_limit_count or 0,
                    agent_error=stats.api_agent_error_count or 0,
                )
                s.triggers.update(
                    mention=stats.trigger_mention_count or 0,
                    keyword=stats.trigger_keyword_count or 0,
                    heartbeat=stats.trigger_heartbeat_count or 0,
                    scheduled=stats.trigger_scheduled_count or 0,
                )

                # 加载记忆系统统计
                s.memory_observations = stats.memory_observations or 0
                s.memory_ingestions = stats.memory_ingestions or 0
                s.memory_ingestion_errors = stats.memory_ingestion_errors or 0
                s.memory_extraction_errors = stats.memory_extraction_errors or 0
                s.memory_retrievals = stats.memory_retrievals or 0
                s.memory_entities_created = stats.memory_entities_created or 0
                s.memory_edges_created = stats.memory_edges_created or 0
                s.memory_episodes_created = stats.memory_episodes_created or 0

            # 2. 加载 AITokenUsageByModel
            all_token_use = await AITokenUsageByModel.get_daily_data(date=today)
            if all_token_use:
                for stats in all_token_use:
                    self._bot_state.token_by_model[stats.model_name.lower()].add(
                        stats.input_tokens or 0,
                        stats.output_tokens or 0,
                        stats.cache_read_tokens or 0,
                        stats.cache_write_tokens or 0,
                    )

            all_token_use_type = await AITokenUsageByType.get_daily_data(date=today)
            if all_token_use_type:
                for stats in all_token_use_type:
                    self._bot_state.token_by_type[stats.chat_type.lower()].add(
                        stats.input_tokens or 0,
                        stats.output_tokens or 0,
                        stats.cache_read_tokens or 0,
                        stats.cache_write_tokens or 0,
                    )

            # 3. 加载 AIHeartbeatMetrics
            all_heartbeat = await AIHeartbeatMetrics.get_daily_data(date=today)
            if all_heartbeat:
                for stats in all_heartbeat:
                    self._bot_state.heartbeats[stats.group_id].update(
                        should_speak_true=stats.should_speak_count or 0,
                        should_speak_false=stats.should_not_speak_count or 0,
                    )

            # 4. 加载 AIGroupUserActivityStats
            all_activity = await AIGroupUserActivityStats.get_daily_data(date=today)
            if all_activity:
                for stats in all_activity:
                    self._bot_state.activities[f"{stats.group_id}:{stats.user_id}"].update(
                        ai_interaction=stats.ai_interaction_count or 0, message=stats.message_count or 0
                    )

            # 5. 加载 RAG 统计数据
            rag_data = await AIRAGMissStatistics.get_daily_data(today)
            if rag_data:
                self._rag["hit"] = rag_data.hit_count or 0
                self._rag["miss"] = rag_data.miss_count or 0

            # 所有维度都加载完毕才开闸放行 persist; 任一步抛异常会跳过本行,
            # _loaded 保持 False, 让后续 _persist_loop 主动跳过以保护历史数据。
            self._loaded = True
            logger.info("📊 [StatisticsManager] 成功加载今日统计数据")
        except Exception as e:
            logger.exception(f"📊 [StatisticsManager] 加载今日数据失败: {e}")

    async def _persist_all_stats_to_db(self):
        """将所有统计数据持久化到数据库。

        - 未完成首次加载前直接返回, 防止空状态覆盖 DB。
        - 与日切 reset 共用 _persist_lock, 保证原子性。
        """
        if not self._loaded:
            logger.warning("📊 [StatisticsManager] 尚未完成今日数据加载, 跳过本次持久化以防覆盖历史数据")
            return
        async with self._persist_lock:
            await self._persist_stats()
            # 持久化 RAG 统计（全局数据，只持久化一次）
            await self._persist_rag_stats()

    async def persist_and_reset_daily(self):
        """日切原子操作: 持久化今日 → 重置内存 → 切换日期。

        Why: _persist_loop 与本方法同时受 _persist_lock 保护, 避免在 reset 已清空
             内存但 _today 尚未推进的窗口里, 让 cron persist 把空状态回写到 Day N 行。
        """
        async with self._persist_lock:
            if self._loaded:
                await self._persist_stats()
                await self._persist_rag_stats()
            else:
                logger.warning("📊 [StatisticsManager] 日切时尚未完成加载, 跳过当日持久化")
            self._reset_daily_counters()
            self._today = datetime.now().strftime("%Y-%m-%d")

    async def _persist_rag_stats(self):
        """持久化 RAG 统计数据到数据库"""
        try:
            today = self._today
            hit = self._rag.get("hit", 0)
            miss = self._rag.get("miss", 0)
            await AIRAGMissStatistics.upsert_rag_stats(today, hit, miss)

            # 持久化文档命中统计
            documents = self._rag.get("documents", {})
            doc_counter: Dict[str, int] = Counter()
            for doc_name in documents.values():
                doc_counter[doc_name] += 1
            for doc_name, count in doc_counter.items():
                await AIRAGDocumentStatistics.upsert_rag_hit_count(doc_name, count)
        except Exception as e:
            logger.exception(f"📊 [StatisticsManager] 持久化 RAG 统计失败: {e}")

    def get_rag_document_stats(self) -> List[Dict[str, Any]]:
        """获取 RAG 文档命中统计"""
        documents = self._rag.get("documents", {})
        doc_counter: Dict[str, int] = Counter()
        for doc_name in documents.values():
            doc_counter[doc_name] += 1
        return [
            {"document_name": name, "hit_count": count}
            for name, count in sorted(doc_counter.items(), key=lambda x: x[1], reverse=True)
        ]

    async def _persist_stats(self):
        """持久化全局统计数据"""
        try:
            s = self._bot_state
            today = self._today

            # 基础统计
            await AIDailyStatistics.upsert_daily_stats(
                date=today,
                total_input_tokens=s.total_tokens["input"],
                total_output_tokens=s.total_tokens["output"],
                total_cache_read_tokens=s.total_tokens["cache_read"],
                total_cache_write_tokens=s.total_tokens["cache_write"],
                avg_latency=s.latencies.avg,
                p95_latency=s.latencies.p95,
                intent_chat_count=s.intents["chat"],
                intent_tool_count=s.intents["tool"],
                intent_qa_count=s.intents["qa"],
                api_timeout_count=s.errors["timeout"],
                api_rate_limit_count=s.errors["rate_limit"],
                api_529_count=s.errors["api_529_error"],
                api_network_error_count=s.errors["network_error"],
                api_usage_limit_count=s.errors["usage_limit"],
                api_agent_error_count=s.errors["agent_error"],
                active_session_count=0,
                avg_messages_per_session=0.0,
                trigger_mention_count=s.triggers["mention"],
                trigger_keyword_count=s.triggers["keyword"],
                trigger_heartbeat_count=s.triggers["heartbeat"],
                trigger_scheduled_count=s.triggers.get("scheduled", 0),
                memory_observations=s.memory_observations,
                memory_ingestions=s.memory_ingestions,
                memory_ingestion_errors=s.memory_ingestion_errors,
                memory_extraction_errors=s.memory_extraction_errors,
                memory_retrievals=s.memory_retrievals,
                memory_entities_created=s.memory_entities_created,
                memory_edges_created=s.memory_edges_created,
                memory_episodes_created=s.memory_episodes_created,
            )

            # Token 按模型统计 - 批量插入
            token_data = [
                AITokenUsageByModel(
                    date=today,
                    model_name=model_name,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                )
                for model_name, usage in s.token_by_model.items()
            ]
            if token_data:
                await AITokenUsageByModel.batch_insert_data_with_update(
                    datas=token_data,
                    update_key=["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"],
                    index_elements=["date", "model_name"],
                )

            token_type_data = [
                AITokenUsageByType(
                    date=today,
                    chat_type=chat_type,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                )
                for chat_type, usage in s.token_by_type.items()
            ]
            if token_type_data:
                await AITokenUsageByType.batch_insert_data_with_update(
                    datas=token_type_data,
                    update_key=["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"],
                    index_elements=["date", "chat_type"],
                )

            # 活跃统计 - 批量插入
            activity_data = [
                AIGroupUserActivityStats(
                    date=today,
                    group_id=key.split(":", 1)[0] if ":" in key else "",
                    user_id=key.split(":", 1)[1] if ":" in key else key,
                    ai_interaction_count=astats["ai_interaction"],
                    message_count=astats["message"],
                )
                for key, astats in s.activities.items()
            ]
            if activity_data:
                await AIGroupUserActivityStats.batch_insert_data_with_update(
                    datas=activity_data,
                    update_key=["ai_interaction_count", "message_count"],
                    index_elements=["date", "group_id", "user_id"],
                )

            # Heartbeat 指标 - 批量插入
            heartbeat_data = [
                AIHeartbeatMetrics(
                    date=today,
                    group_id=group_id,
                    should_speak_count=hstats.get("should_speak_true", 0),
                    should_not_speak_count=hstats.get("should_speak_false", 0),
                )
                for group_id, hstats in s.heartbeats.items()
                if hstats.get("should_speak_true") or hstats.get("should_speak_false")
            ]
            if heartbeat_data:
                await AIHeartbeatMetrics.batch_insert_data_with_update(
                    datas=heartbeat_data,
                    update_key=["should_speak_count", "should_not_speak_count"],
                    index_elements=["date", "group_id"],
                )

            # 小时级性能统计持久化
            await self._persist_hourly_performance()

        except Exception as e:
            logger.exception(f"📊 [StatisticsManager] 持久化统计数据失败: {e}")

    async def _persist_hourly_performance(self):
        """持久化小时级性能统计到数据库

        upsert 是增量累加语义, 因此成功落库的 key 必须立即出队, 否则下一轮
        定时持久化会把同一份累计值重复累加进 DB（数据成倍膨胀）。
        先 pop 再 await: upsert 期间新到的请求会写入新建的 entry, 不会丢增量;
        落库失败则把增量 merge 回缓冲, 等下一轮重试。
        本方法仅在 _persist_lock 内被调用, 不存在并发的出队/清空。
        """
        for key in list(self._hourly_perf.keys()):
            entry = self._hourly_perf.pop(key)
            date, hour, provider, model_name = key
            ok = await AIHourlyPerformance.upsert_performance(
                date=date,
                hour=hour,
                provider=provider,
                model_name=model_name,
                request_count=entry.request_count,
                ttft_min_ms=entry.ttft_ms_min,
                ttft_max_ms=entry.ttft_ms_max,
                ttft_sum_ms=entry.ttft_ms_sum,
                ttft_sample_count=entry.ttft_sample_count,
                tps_min=entry.tps_min,
                tps_max=entry.tps_max,
                tps_sum=entry.tps_sum,
                tps_sample_count=entry.tps_sample_count,
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                cache_read_tokens=entry.cache_read_tokens,
                cache_write_tokens=entry.cache_write_tokens,
                tool_call_count=entry.tool_call_count,
            )
            if not ok:
                self._hourly_perf[key].merge(entry)
                logger.warning(f"📊 [StatisticsManager] 小时性能统计落库失败, 增量已回滚至缓冲: {key}")

    async def get_hourly_performance_by_date(self, date: str) -> List[Dict[str, Any]]:
        """获取指定日期的小时级性能统计（DB 基线 + 内存未持久化增量合并）"""
        try:
            merged: Dict[tuple[int, str, str], HourlyPerformanceEntry] = {}
            for row in await AIHourlyPerformance.get_daily_data(date):
                merged[(row.hour, row.provider, row.model_name)] = self._hourly_row_to_entry(row)
            # 内存缓冲只保留尚未持久化的增量, 叠加到 DB 基线上,
            # 既覆盖"今日未到持久化周期"也覆盖"重启后当日 DB 数据"两种场景
            for (d, hour, provider, model), entry in list(self._hourly_perf.items()):
                if d != date:
                    continue
                base = merged.get((hour, provider, model))
                if base is None:
                    base = HourlyPerformanceEntry()
                    merged[(hour, provider, model)] = base
                base.merge(entry)

            result: Dict[int, Dict[str, Any]] = {}
            for (hour, provider, model), entry in merged.items():
                if hour not in result:
                    result[hour] = {"hour": hour, "providers": []}
                result[hour]["providers"].append(self._hourly_entry_to_dict(provider, model, entry))
            for item in result.values():
                item["providers"].sort(key=lambda p: (p["provider"], p["model"]))
            return sorted(result.values(), key=lambda x: x["hour"])
        except Exception as e:
            logger.warning(f"📊 [StatisticsManager] 查询小时性能统计失败: {e}")
            return []

    async def get_summary_by_date(self, date: str) -> Optional[Dict[str, Any]]:
        """从数据库获取指定日期的统计摘要"""
        try:
            # 获取 RAG 统计数据
            rag_data = await AIRAGMissStatistics.get_daily_data(date)
            hit_count = rag_data.hit_count if rag_data else 0
            miss_count = rag_data.miss_count if rag_data else 0
            total = hit_count + miss_count
            hit_rate = (hit_count / total * 100) if total > 0 else 0.0

            stats = await AIDailyStatistics.get_daily_stats(date)
            if not stats:
                return None

            # 获取按模型分组的 Token 消耗数据
            token_by_model_data = await AITokenUsageByModel.get_daily_data(date)
            by_model = [
                {
                    "model": t.model_name,
                    "input_tokens": t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "cache_read_tokens": t.cache_read_tokens or 0,
                    "cache_write_tokens": t.cache_write_tokens or 0,
                }
                for t in token_by_model_data
            ]

            # 获取按类型分组的 Token 消耗数据
            # 注意: 字段名统一为 `type`, 与 get_summary() 中今日内存数据的 by_type 保持一致,
            #       避免前端 AIStatisticsPage.tsx 的 TokenByType 接口拿到 `chat_type` 时回落成 "Unknown"。
            token_by_type_data = await AITokenUsageByType.get_daily_data(date)
            by_type = [
                {
                    "type": t.chat_type,
                    "input_tokens": t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "cache_read_tokens": t.cache_read_tokens or 0,
                    "cache_write_tokens": t.cache_write_tokens or 0,
                }
                for t in token_by_type_data
            ]

            # 获取活跃用户数据
            activity_data = await AIGroupUserActivityStats.get_daily_data(date)
            active_users = [
                {
                    "group_id": a.group_id,
                    "user_id": a.user_id,
                    "ai_interaction": a.ai_interaction_count,
                    "message_count": a.message_count,
                }
                for a in activity_data
            ]

            # 获取 Heartbeat 数据
            heartbeat_data = await AIHeartbeatMetrics.get_daily_data(date)
            hb_true = sum(h.should_speak_count or 0 for h in heartbeat_data)
            hb_false = sum(h.should_not_speak_count or 0 for h in heartbeat_data)
            heartbeat = {
                "should_speak_true": hb_true,
                "should_speak_false": hb_false,
                "conversion_rate": hb_true / (hb_true + hb_false) * 100 if (hb_true + hb_false) > 0 else 0,
            }

            return self._daily_stats_to_dict(
                stats,
                hit_count,
                miss_count,
                hit_rate,
                by_model,
                by_type,
                active_users,
                heartbeat,
            )
        except Exception as e:
            logger.warning(f"📊 [StatisticsManager] 查询历史统计失败: {e}")
            return None

    def _daily_stats_to_dict(
        self,
        stats: AIDailyStatistics,
        rag_hit: int = 0,
        rag_miss: int = 0,
        rag_hit_rate: float = 0.0,
        by_model: Optional[List[Dict[str, Any]]] = None,
        by_type: Optional[List[Dict[str, Any]]] = None,
        active_users: Optional[List[Dict[str, Any]]] = None,
        heartbeat: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """将 AIDailyStatistics 转换为字典格式"""
        t_intent = (stats.intent_chat_count or 0) + (stats.intent_tool_count or 0) + (stats.intent_qa_count or 0) or 1
        total_triggers = (stats.trigger_mention_count or 0) + (stats.trigger_keyword_count or 0) + (
            stats.trigger_heartbeat_count or 0
        ) + (stats.trigger_scheduled_count or 0) or 1
        return {
            "date": stats.date,
            "token_usage": {
                "total_input_tokens": stats.total_input_tokens or 0,
                "total_output_tokens": stats.total_output_tokens or 0,
                "total_cache_read_tokens": stats.total_cache_read_tokens or 0,
                "total_cache_write_tokens": stats.total_cache_write_tokens or 0,
                "by_model": by_model or [],
                "by_type": by_type or [],
            },
            "latency": {"avg": stats.avg_latency or 0.0, "p95": stats.p95_latency or 0.0},
            "intent_distribution": {
                "chat": {
                    "count": stats.intent_chat_count or 0,
                    "percentage": (stats.intent_chat_count or 0) / t_intent * 100,
                },
                "tool": {
                    "count": stats.intent_tool_count or 0,
                    "percentage": (stats.intent_tool_count or 0) / t_intent * 100,
                },
                "qa": {
                    "count": stats.intent_qa_count or 0,
                    "percentage": (stats.intent_qa_count or 0) / t_intent * 100,
                },
            },
            "errors": {
                "timeout": stats.api_timeout_count or 0,
                "rate_limit": stats.api_rate_limit_count or 0,
                "network_error": stats.api_network_error_count or 0,
                "usage_limit": stats.api_usage_limit_count or 0,
                "agent_error": stats.api_agent_error_count or 0,
                "api_529_error": stats.api_529_count or 0,
                "total": (stats.api_timeout_count or 0)
                + (stats.api_rate_limit_count or 0)
                + (stats.api_network_error_count or 0)
                + (stats.api_usage_limit_count or 0)
                + (stats.api_agent_error_count or 0)
                + (stats.api_529_count or 0),
            },
            "trigger_distribution": {
                "mention": {
                    "count": stats.trigger_mention_count or 0,
                    "percentage": (stats.trigger_mention_count or 0) / total_triggers * 100,
                },
                "keyword": {
                    "count": stats.trigger_keyword_count or 0,
                    "percentage": (stats.trigger_keyword_count or 0) / total_triggers * 100,
                },
                "heartbeat": {
                    "count": stats.trigger_heartbeat_count or 0,
                    "percentage": (stats.trigger_heartbeat_count or 0) / total_triggers * 100,
                },
                "scheduled": {
                    "count": stats.trigger_scheduled_count or 0,
                    "percentage": (stats.trigger_scheduled_count or 0) / total_triggers * 100,
                },
            },
            "rag": {"hit_count": rag_hit, "miss_count": rag_miss, "hit_rate": rag_hit_rate},
            "memory": {
                "observations": stats.memory_observations or 0,
                "ingestions": stats.memory_ingestions or 0,
                "ingestion_errors": stats.memory_ingestion_errors or 0,
                "extraction_errors": stats.memory_extraction_errors or 0,
                "retrievals": stats.memory_retrievals or 0,
                "entities_created": stats.memory_entities_created or 0,
                "edges_created": stats.memory_edges_created or 0,
                "episodes_created": stats.memory_episodes_created or 0,
            },
            "heartbeat": heartbeat or {"should_speak_true": 0, "should_speak_false": 0, "conversion_rate": 0},
            "active_users": active_users or [],
        }


# 全局单例
_statistics_manager: Optional[StatisticsManager] = None


def get_statistics_manager() -> StatisticsManager:
    global _statistics_manager
    if _statistics_manager is None:
        _statistics_manager = StatisticsManager()
    return _statistics_manager


statistics_manager = get_statistics_manager()


@scheduler.scheduled_job("cron", minute="*/30")
async def _persist_loop():
    """每30分钟将 AI 统计数据持久化"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return

    await statistics_manager._persist_all_stats_to_db()
    logger.info("📊 [StatisticsManager] 每30分钟定时持久化完成")
