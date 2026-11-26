"""
AI Core Statistics Manager
AI 模块统计管理器

负责收集、聚合和持久化 AI 模块的各类统计数据。
支持每日数据持久化（启动/关闭/零点重置）。
"""

import asyncio
from typing import Any, Dict, List, Optional, Sequence, TypedDict, NotRequired
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from dataclasses import dataclass

from gsuid_core.aps import scheduler
from gsuid_core.i18n import t as i18n_t
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

from .price import calculate_model_cost
from .dataclass_models import BotState, TokenUsage, HourlyPerformanceEntry

# 小时级性能统计的内存缓冲 key: (date, hour, provider, model_name)
HourlyPerfKey = tuple[str, int, str, str]
_TokenSliceRow = tuple[str, int, int, int, int]
_TypeSliceRow = tuple[str, int, int, int, int, int]
_ERROR_KEYS: tuple[str, ...] = (
    "timeout",
    "rate_limit",
    "network_error",
    "usage_limit",
    "agent_error",
    "api_529_error",
)


class _ModelUsageRow(TypedDict):
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: NotRequired[int]
    cost: float


class _TypeUsageRow(TypedDict):
    type: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    request_count: int
    total_tokens: NotRequired[int]


@dataclass(frozen=True, slots=True)
class _DayStatSlice:
    """单日聚合切片，统一承载内存实时态（今日）与数据库历史行。"""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    user_turn_count: int
    agent_run_count: int
    user_turn_agent_run_count: int
    user_turn_input_tokens: int
    user_turn_output_tokens: int
    models: tuple[_TokenSliceRow, ...]
    types: tuple[_TypeSliceRow, ...]
    latency_avg: float
    latency_p95: float
    latency_samples: int
    errors: dict[str, int]


def _safe_avg(numerator: float, denominator: int) -> float:
    return round(float(numerator) / float(denominator), 2) if denominator > 0 else 0.0


def _errors_from_state(state: BotState) -> dict[str, int]:
    return {key: state.errors[key] for key in _ERROR_KEYS}


def _errors_from_daily_stats(stats: Optional[AIDailyStatistics]) -> dict[str, int]:
    if stats is None:
        return {key: 0 for key in _ERROR_KEYS}
    return {
        "timeout": stats.api_timeout_count or 0,
        "rate_limit": stats.api_rate_limit_count or 0,
        "network_error": stats.api_network_error_count or 0,
        "usage_limit": stats.api_usage_limit_count or 0,
        "agent_error": stats.api_agent_error_count or 0,
        "api_529_error": stats.api_529_count or 0,
    }


def _heartbeat_dict(hb_true: int, hb_false: int) -> dict[str, int | float]:
    total = hb_true + hb_false
    return {
        "should_speak_true": hb_true,
        "should_speak_false": hb_false,
        "conversion_rate": hb_true / total * 100 if total > 0 else 0,
    }


def _memory_stats_dict(src: BotState | AIDailyStatistics) -> dict[str, int]:
    return {
        "observations": src.memory_observations or 0,
        "ingestions": src.memory_ingestions or 0,
        "ingestion_errors": src.memory_ingestion_errors or 0,
        "extraction_errors": src.memory_extraction_errors or 0,
        "retrievals": src.memory_retrievals or 0,
        "entities_created": src.memory_entities_created or 0,
        "edges_created": src.memory_edges_created or 0,
        "episodes_created": src.memory_episodes_created or 0,
    }


def _distribution_dict(
    counts: Sequence[tuple[str, int]],
    *,
    total: Optional[int] = None,
) -> dict[str, dict[str, int | float]]:
    denom = (total if total is not None else sum(v for _, v in counts)) or 1
    return {k: {"count": v, "percentage": v / denom * 100} for k, v in counts}


def _model_usage_row(
    model: str,
    inp: int,
    outp: int,
    cr: int,
    cw: int,
    *,
    include_total: bool = False,
) -> _ModelUsageRow:
    row: _ModelUsageRow = {
        "model": model,
        "input_tokens": inp,
        "output_tokens": outp,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "cost": calculate_model_cost(model, inp, outp, cr, cw),
    }
    if include_total:
        row["total_tokens"] = inp + outp
    return row


def _type_usage_row(
    chat_type: str,
    inp: int,
    outp: int,
    cr: int,
    cw: int,
    req_count: int = 0,
    *,
    include_total: bool = False,
) -> _TypeUsageRow:
    row: _TypeUsageRow = {
        "type": chat_type,
        "input_tokens": inp,
        "output_tokens": outp,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "request_count": req_count,
    }
    if include_total:
        row["total_tokens"] = inp + outp
    return row


def _efficiency_dict(src: BotState | AIDailyStatistics) -> dict[str, int | float]:
    if isinstance(src, BotState):
        ut_in = int(src.user_turn_tokens.input_tokens or 0)
        ut_out = int(src.user_turn_tokens.output_tokens or 0)
        ut_cr = int(src.user_turn_tokens.cache_read_tokens or 0)
        ut_cw = int(src.user_turn_tokens.cache_write_tokens or 0)
        total_in = int(src.total_tokens["input"] or 0)
        total_out = int(src.total_tokens["output"] or 0)
    else:
        ut_in = int(src.user_turn_input_tokens or 0)
        ut_out = int(src.user_turn_output_tokens or 0)
        ut_cr = int(src.user_turn_cache_read_tokens or 0)
        ut_cw = int(src.user_turn_cache_write_tokens or 0)
        total_in = int(src.total_input_tokens or 0)
        total_out = int(src.total_output_tokens or 0)
    ut = int(src.user_turn_count or 0)
    ar = int(src.agent_run_count or 0)
    ut_ar = int(src.user_turn_agent_run_count or 0)
    # 回合均耗：用户回合树内 input+output（缓存另计，避免与 input 双重叠加）
    return {
        "user_turn_count": ut,
        "agent_run_count": ar,
        "root_agent_run_count": int(src.root_agent_run_count or 0),
        "nested_agent_run_count": int(src.nested_agent_run_count or 0),
        "user_turn_agent_run_count": ut_ar,
        "user_turn_input_tokens": ut_in,
        "user_turn_output_tokens": ut_out,
        "user_turn_cache_read_tokens": ut_cr,
        "user_turn_cache_write_tokens": ut_cw,
        "avg_tokens_per_user_turn": _safe_avg(ut_in + ut_out, ut),
        "avg_input_tokens_per_user_turn": _safe_avg(ut_in, ut),
        "avg_output_tokens_per_user_turn": _safe_avg(ut_out, ut),
        "avg_tokens_per_agent_run": _safe_avg(total_in + total_out, ar),
        "avg_input_tokens_per_agent_run": _safe_avg(total_in, ar),
        "avg_output_tokens_per_agent_run": _safe_avg(total_out, ar),
        "avg_agent_runs_per_user_turn": _safe_avg(ut_ar, ut),
    }


def _slice_from_live_state(b: BotState) -> _DayStatSlice:
    return _DayStatSlice(
        input_tokens=b.total_tokens["input"],
        output_tokens=b.total_tokens["output"],
        cache_read_tokens=b.total_tokens["cache_read"],
        cache_write_tokens=b.total_tokens["cache_write"],
        user_turn_count=int(b.user_turn_count or 0),
        agent_run_count=int(b.agent_run_count or 0),
        user_turn_agent_run_count=int(b.user_turn_agent_run_count or 0),
        user_turn_input_tokens=int(b.user_turn_tokens.input_tokens or 0),
        user_turn_output_tokens=int(b.user_turn_tokens.output_tokens or 0),
        models=tuple(
            (m, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens)
            for m, u in b.token_by_model.items()
        ),
        types=tuple(
            (t, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.request_count)
            for t, u in b.token_by_type.items()
        ),
        latency_avg=b.latencies.avg,
        latency_p95=b.latencies.p95,
        latency_samples=len(b.latencies.latencies),
        errors=_errors_from_state(b),
    )


def _slice_from_db_rows(
    stats: Optional[AIDailyStatistics],
    model_rows: Sequence[AITokenUsageByModel],
    type_rows: Sequence[AITokenUsageByType],
) -> _DayStatSlice:
    return _DayStatSlice(
        input_tokens=(stats.total_input_tokens or 0) if stats else 0,
        output_tokens=(stats.total_output_tokens or 0) if stats else 0,
        cache_read_tokens=(stats.total_cache_read_tokens or 0) if stats else 0,
        cache_write_tokens=(stats.total_cache_write_tokens or 0) if stats else 0,
        user_turn_count=int(stats.user_turn_count or 0) if stats else 0,
        agent_run_count=int(stats.agent_run_count or 0) if stats else 0,
        user_turn_agent_run_count=int(stats.user_turn_agent_run_count or 0) if stats else 0,
        user_turn_input_tokens=int(stats.user_turn_input_tokens or 0) if stats else 0,
        user_turn_output_tokens=int(stats.user_turn_output_tokens or 0) if stats else 0,
        models=tuple(
            (
                row.model_name,
                row.input_tokens or 0,
                row.output_tokens or 0,
                row.cache_read_tokens or 0,
                row.cache_write_tokens or 0,
            )
            for row in model_rows
        ),
        types=tuple(
            (
                row.chat_type,
                row.input_tokens or 0,
                row.output_tokens or 0,
                row.cache_read_tokens or 0,
                row.cache_write_tokens or 0,
                row.request_count or 0,
            )
            for row in type_rows
        ),
        latency_avg=(stats.avg_latency or 0.0) if stats else 0.0,
        latency_p95=(stats.p95_latency or 0.0) if stats else 0.0,
        latency_samples=int(stats.latency_sample_count or 0) if stats else 0,
        errors=_errors_from_daily_stats(stats),
    )


def _day_slice_row(date_str: str, sl: _DayStatSlice, *, include_cache: bool) -> dict[str, object]:
    d_cost = round(sum(calculate_model_cost(m, inp, outp, cr, cw) for m, inp, outp, cr, cw in sl.models), 6)
    row: dict[str, object] = {
        "date": date_str,
        "input_tokens": sl.input_tokens,
        "output_tokens": sl.output_tokens,
    }
    if include_cache:
        row["cache_read_tokens"] = sl.cache_read_tokens
        row["cache_write_tokens"] = sl.cache_write_tokens
    row.update(
        {
            "total_tokens": sl.input_tokens + sl.output_tokens,
            "total_cost": d_cost,
            "user_turn_count": sl.user_turn_count,
            "agent_run_count": sl.agent_run_count,
            "user_turn_agent_run_count": sl.user_turn_agent_run_count,
            "avg_tokens_per_user_turn": _safe_avg(
                sl.user_turn_input_tokens + sl.user_turn_output_tokens, sl.user_turn_count
            ),
            "avg_tokens_per_agent_run": _safe_avg(sl.input_tokens + sl.output_tokens, sl.agent_run_count),
            "avg_agent_runs_per_user_turn": _safe_avg(sl.user_turn_agent_run_count, sl.user_turn_count),
        }
    )
    return row


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
        # 完成前抢先 fire, 把空的 _bot_state 写回 DB, 抹掉当日 AIDailyStatistics 的 total_*,
        self._loaded: bool = False
        # Why: _persist_loop 与日切 _scheduled_ai_core_reset 在 00:00 会同时触发;
        # reset 内 "持久化 → 清空 → 切日期" 三步若被并发 persist 切片, 可能把空状态
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
        request_count: int = 1,
    ) -> None:
        """记录 Token 使用量与真实模型请求数"""
        req_n = max(0, request_count)
        self._bot_state.token_by_model[model_name.lower()].add(
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, req_n
        )
        self._bot_state.token_by_type[chat_type.lower()].add(
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, req_n
        )
        self._bot_state.total_tokens.update(
            input=input_tokens,
            output=output_tokens,
            cache_read=cache_read_tokens,
            cache_write=cache_write_tokens,
        )

    def record_agent_run(
        self,
        *,
        owns_user_turn: bool,
        under_user_turn: bool,
        is_nested: bool,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """记录一次 Agent Run 的效率计数，并按需归入 User Turn 树。

        Args:
            owns_user_turn: 本 run 是否为用户回合 root（主人格交互新建 user_turn_id）
            under_user_turn: 是否挂在某用户回合树下（含 root 自身与同步嵌套）
            is_nested: 是否为嵌套 run（subagent / capability 等）
            input_tokens / output_tokens / cache_*: 本 run 自身 usage（不含子 run）
        """
        s = self._bot_state
        s.agent_run_count += 1
        if is_nested:
            s.nested_agent_run_count += 1
        else:
            s.root_agent_run_count += 1
        if owns_user_turn:
            s.user_turn_count += 1
        if under_user_turn:
            s.user_turn_agent_run_count += 1
            s.user_turn_tokens.add(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)

    @staticmethod
    def _safe_avg(numerator: float, denominator: int) -> float:
        return _safe_avg(numerator, denominator)

    def _efficiency_from_state(self, s: BotState) -> Dict[str, Any]:
        """从内存 BotState 组装 efficiency 块。"""
        return _efficiency_dict(s)

    def _efficiency_from_daily_stats(self, stats: AIDailyStatistics) -> Dict[str, Any]:
        """从日聚合行组装 efficiency 块。"""
        return _efficiency_dict(stats)

    def record_latency(self, latency: float):
        """记录响应延迟"""
        self._bot_state.latencies.add(latency)

    def record_intent(self, intent: str):
        """记录意图"""
        mapped_intent = self.INTENT_MAP.get(intent, intent)
        self._bot_state.intents[mapped_intent] += 1

    def record_error(self, error_type: str, message: str = ""):
        """记录错误。message 只进按日事件文件，计数器仍只按类型累加。"""
        self._bot_state.errors[error_type] += 1
        try:
            from gsuid_core.ai_core.statistics.error_events import append_error_event

            append_error_event(self._today, error_type, message)
        except Exception as e:
            logger.exception(i18n_t("log.ai.statisticsmanager_append_error_event_fail", e=e))

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
        agg_heartbeats: Counter[str] = Counter()
        for h in b.heartbeats.values():
            agg_heartbeats.update(h)

        active_users = sorted(
            [
                {
                    "group_id": k.split(":")[0],
                    "user_id": k.split(":")[1],
                    "ai_interaction": v["ai_interaction"],
                    "message_count": v["message"],
                }
                for k, v in b.activities.items()
            ],
            key=lambda x: x["ai_interaction"],
            reverse=True,
        )[:20]

        by_model = [
            _model_usage_row(m, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens)
            for m, u in b.token_by_model.items()
        ]
        by_type = [
            _type_usage_row(
                t,
                u.input_tokens,
                u.output_tokens,
                u.cache_read_tokens,
                u.cache_write_tokens,
                u.request_count,
            )
            for t, u in b.token_by_type.items()
        ]
        rag_hit = int(self._rag["hit"])
        rag_miss = int(self._rag["miss"])

        return {
            "date": self._today,
            "token_usage": {
                "total_input_tokens": b.total_tokens["input"],
                "total_output_tokens": b.total_tokens["output"],
                "total_cache_read_tokens": b.total_tokens["cache_read"],
                "total_cache_write_tokens": b.total_tokens["cache_write"],
                "total_cost": round(sum(item["cost"] for item in by_model), 6),
                "by_model": by_model,
                "by_type": by_type,
            },
            "latency": {"avg": b.latencies.avg, "p95": b.latencies.p95},
            "intent_distribution": _distribution_dict(
                [(k, b.intents[k]) for k in ("chat", "tool", "qa")],
                total=sum(b.intents.values()),
            ),
            "errors": {**_errors_from_state(b), "total": sum(b.errors.values())},
            "heartbeat": _heartbeat_dict(agg_heartbeats["should_speak_true"], agg_heartbeats["should_speak_false"]),
            "trigger_distribution": _distribution_dict(
                sorted(b.triggers.items(), key=lambda kv: kv[1], reverse=True),
            ),
            "rag": {
                "hit_count": rag_hit,
                "miss_count": rag_miss,
                "hit_rate": rag_hit / (rag_hit + rag_miss or 1) * 100,
            },
            "memory": _memory_stats_dict(b),
            "efficiency": self._efficiency_from_state(b),
            "active_users": active_users,
        }

    # ==================== 数据库持久化 ====================

    async def _load_today_data_from_db(self):
        """从数据库加载今日数据"""
        try:
            today = self._today
            logger.info(i18n_t("log.ai.statisticsmanager_statistics_today_database_load", today=today))

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

                # 效率：User Turn / Agent Run
                s.user_turn_count = stats.user_turn_count or 0
                s.agent_run_count = stats.agent_run_count or 0
                s.root_agent_run_count = stats.root_agent_run_count or 0
                s.nested_agent_run_count = stats.nested_agent_run_count or 0
                s.user_turn_agent_run_count = stats.user_turn_agent_run_count or 0
                s.user_turn_tokens.add(
                    stats.user_turn_input_tokens or 0,
                    stats.user_turn_output_tokens or 0,
                    stats.user_turn_cache_read_tokens or 0,
                    stats.user_turn_cache_write_tokens or 0,
                )

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
                        stats.request_count or 0,
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
            logger.info(i18n_t("log.ai.statisticsmanager_today_statistics"))
        except Exception as e:
            logger.exception(i18n_t("log.ai.statisticsmanager_load_today_data", e=e))

    async def _persist_all_stats_to_db(self):
        """将所有统计数据持久化到数据库。

        - 未完成首次加载前直接返回, 防止空状态覆盖 DB。
        - 与日切 reset 共用 _persist_lock, 保证原子性。
        """
        if not self._loaded:
            logger.warning(i18n_t("log.ai.statisticsmanager_today_data_skipping_ok"))
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
                logger.warning(i18n_t("log.ai.statisticsmanager_day_rollover_skipping_ok"))
            self._reset_daily_counters()
            self._today = datetime.now().strftime("%Y-%m-%d")

    def _rag_doc_counter(self) -> Counter[str]:
        documents = self._rag["documents"] if "documents" in self._rag else {}
        return Counter(documents.values())

    async def _persist_rag_stats(self):
        """持久化 RAG 统计数据到数据库"""
        try:
            today = self._today
            hit = self._rag["hit"] if "hit" in self._rag else 0
            miss = self._rag["miss"] if "miss" in self._rag else 0
            await AIRAGMissStatistics.upsert_rag_stats(today, hit, miss)

            for doc_name, count in self._rag_doc_counter().items():
                await AIRAGDocumentStatistics.upsert_rag_hit_count(doc_name, count)
        except Exception as e:
            logger.exception(i18n_t("log.ai.statisticsmanager_persist_rag_statistics", e=e))

    def get_rag_document_stats(self) -> List[Dict[str, Any]]:
        """获取 RAG 文档命中统计"""
        return [{"document_name": name, "hit_count": count} for name, count in self._rag_doc_counter().most_common()]

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
                # 样本数一起落库：区间平均延迟按它加权，否则只能拿每日均值取算术平均
                latency_sample_count=len(s.latencies.latencies),
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
                user_turn_count=s.user_turn_count,
                agent_run_count=s.agent_run_count,
                root_agent_run_count=s.root_agent_run_count,
                nested_agent_run_count=s.nested_agent_run_count,
                user_turn_agent_run_count=s.user_turn_agent_run_count,
                user_turn_input_tokens=s.user_turn_tokens.input_tokens,
                user_turn_output_tokens=s.user_turn_tokens.output_tokens,
                user_turn_cache_read_tokens=s.user_turn_tokens.cache_read_tokens,
                user_turn_cache_write_tokens=s.user_turn_tokens.cache_write_tokens,
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
                    request_count=usage.request_count,
                )
                for chat_type, usage in s.token_by_type.items()
            ]
            if token_type_data:
                await AITokenUsageByType.batch_insert_data_with_update(
                    datas=token_type_data,
                    update_key=[
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "cache_write_tokens",
                        "request_count",
                    ],
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
            logger.exception(i18n_t("log.ai.statisticsmanager_persist_statistics", e=e))

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
                logger.warning(i18n_t("log.ai.statisticsmanager_write_hourly_performance_fail", key=key))

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
            logger.warning(i18n_t("log.ai.statisticsmanager_query_hourly_performance_fail", e=e))
            return []

    async def get_daily_token_counts(self, days: int = 60) -> List[Dict[str, Any]]:
        """近 N 天每日 input/output token 汇总，供日历选择器展示。

        返回按日期升序的每日汇总序列，无数据的日期补 0；今日优先读内存实时值。
        """
        days = max(1, min(int(days or 60), 366))
        today = datetime.now().date()
        start = today - timedelta(days=days - 1)
        start_s = start.strftime("%Y-%m-%d")
        end_s = today.strftime("%Y-%m-%d")

        stats_by_date: Dict[str, AIDailyStatistics] = {}
        model_by_date: Dict[str, List[AITokenUsageByModel]] = defaultdict(list)
        try:
            daily_rows, model_rows = await asyncio.gather(
                AIDailyStatistics.get_stats_between(start_s, end_s),
                AITokenUsageByModel.get_data_between(start_s, end_s),
            )
            for row in daily_rows:
                stats_by_date[str(row.date)[:10]] = row
            for m_row in model_rows:
                model_by_date[str(m_row.date)[:10]].append(m_row)
        except Exception:
            # DB 不可用时仍返回连续 0 序列
            pass

        result: List[Dict[str, Any]] = []
        for offset in range(days - 1, -1, -1):
            key = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            sl = (
                _slice_from_live_state(self._bot_state)
                if self._today and key == self._today
                else _slice_from_db_rows(
                    stats_by_date[key] if key in stats_by_date else None,
                    model_by_date[key],
                    (),
                )
            )
            result.append(_day_slice_row(key, sl, include_cache=False))
        return result

    async def get_summary_by_date(self, date: str) -> Optional[Dict[str, Any]]:
        """从数据库获取指定日期的统计摘要"""
        try:
            rag_data = await AIRAGMissStatistics.get_daily_data(date)
            hit_count = rag_data.hit_count if rag_data else 0
            miss_count = rag_data.miss_count if rag_data else 0
            total = hit_count + miss_count
            hit_rate = (hit_count / total * 100) if total > 0 else 0.0

            stats = await AIDailyStatistics.get_daily_stats(date)
            if not stats:
                return None

            token_by_model_data = await AITokenUsageByModel.get_daily_data(date)
            by_model: List[_ModelUsageRow] = [
                _model_usage_row(
                    t.model_name,
                    t.input_tokens,
                    t.output_tokens,
                    t.cache_read_tokens or 0,
                    t.cache_write_tokens or 0,
                )
                for t in token_by_model_data
            ]

            # 避免前端 AIStatisticsPage.tsx 的 TokenByType 接口拿到 `chat_type` 时回落成 "Unknown"。
            token_by_type_data = await AITokenUsageByType.get_daily_data(date)
            by_type: List[_TypeUsageRow] = [
                _type_usage_row(
                    t.chat_type,
                    t.input_tokens,
                    t.output_tokens,
                    t.cache_read_tokens or 0,
                    t.cache_write_tokens or 0,
                    t.request_count or 0,
                )
                for t in token_by_type_data
            ]

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

            heartbeat_data = await AIHeartbeatMetrics.get_daily_data(date)
            heartbeat = _heartbeat_dict(
                sum(h.should_speak_count or 0 for h in heartbeat_data),
                sum(h.should_not_speak_count or 0 for h in heartbeat_data),
            )

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
            logger.warning(i18n_t("log.ai.statisticsmanager_query_historical_statistics_fa", e=e))
            return None

    async def get_token_usage_by_range(
        self,
        start_date: str,
        end_date: str,
        max_days: int = 366,
    ) -> Dict[str, Any]:
        """获取指定时间段（闭区间）的 Token 消耗、效率、延迟与错误统计。

        返回字段：``start_date`` / ``end_date`` / ``days`` / ``total`` / ``efficiency`` /
        ``daily`` / ``by_model`` / ``by_type`` / ``latency`` / ``errors``。
        今日走内存实时态、历史走 DB、无数据日补 0；``start_date > end_date`` 自动交换，
        超过 ``max_days`` 保留靠近 ``end_date`` 的天数；日期格式非法抛 ``ValueError``。

        Why 一次性扫区间：三张表各发一次 BETWEEN（走 date 索引），往返数恒为 3，
        避免逐日查导致 90 天档 2N 次往返卡死。延迟 ``avg`` 按 ``latency_sample_count``
        加权（迁移前全 0 样本退回日均值算术平均），``p95`` 取区间内各日峰值。
        """
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        if start > end:
            start, end = end, start
        if (end - start).days + 1 > max_days:
            start = end - timedelta(days=max_days - 1)

        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")

        daily_stats_rows, model_rows, type_rows = await asyncio.gather(
            AIDailyStatistics.get_stats_between(start_str, end_str),
            AITokenUsageByModel.get_data_between(start_str, end_str),
            AITokenUsageByType.get_data_between(start_str, end_str),
        )

        stats_by_date = {row.date: row for row in daily_stats_rows}
        model_by_date: Dict[str, List[AITokenUsageByModel]] = defaultdict(list)
        for row in model_rows:
            model_by_date[row.date].append(row)
        type_by_date: Dict[str, List[AITokenUsageByType]] = defaultdict(list)
        for row in type_rows:
            type_by_date[row.date].append(row)

        daily: List[Dict[str, Any]] = []
        model_acc: Dict[str, TokenUsage] = defaultdict(TokenUsage)
        type_acc: Dict[str, TokenUsage] = defaultdict(TokenUsage)
        total = TokenUsage()
        sum_user_turns = 0
        sum_agent_runs = 0
        sum_ut_agent_runs = 0
        sum_ut_in = 0
        sum_ut_out = 0
        lat_weight_sum = 0.0
        lat_weight_n = 0
        lat_peak = 0.0
        lat_day_avg_sum = 0.0
        lat_day_count = 0
        err_acc: Dict[str, int] = {key: 0 for key in _ERROR_KEYS}

        cur = start
        while cur <= end:
            date_str = cur.strftime("%Y-%m-%d")
            cur += timedelta(days=1)

            sl = (
                _slice_from_live_state(self._bot_state)
                if date_str == self._today
                else _slice_from_db_rows(
                    stats_by_date[date_str] if date_str in stats_by_date else None,
                    model_by_date[date_str],
                    type_by_date[date_str],
                )
            )
            daily.append(_day_slice_row(date_str, sl, include_cache=True))
            total.add(sl.input_tokens, sl.output_tokens, sl.cache_read_tokens, sl.cache_write_tokens)
            sum_user_turns += sl.user_turn_count
            sum_agent_runs += sl.agent_run_count
            sum_ut_agent_runs += sl.user_turn_agent_run_count
            sum_ut_in += sl.user_turn_input_tokens
            sum_ut_out += sl.user_turn_output_tokens
            if sl.latency_samples > 0:
                lat_weight_sum += sl.latency_avg * sl.latency_samples
                lat_weight_n += sl.latency_samples
            if sl.latency_avg > 0:
                lat_day_avg_sum += sl.latency_avg
                lat_day_count += 1
            lat_peak = max(lat_peak, sl.latency_p95)
            for err_k, err_v in sl.errors.items():
                err_acc[err_k] += err_v
            for model_name, m_in, m_out, m_cr, m_cw in sl.models:
                model_acc[model_name].add(m_in, m_out, m_cr, m_cw)
            for type_name, t_in, t_out, t_cr, t_cw, t_req in sl.types:
                type_acc[type_name].add(t_in, t_out, t_cr, t_cw, t_req)

        by_model = [
            _model_usage_row(
                model_name,
                u.input_tokens,
                u.output_tokens,
                u.cache_read_tokens,
                u.cache_write_tokens,
                include_total=True,
            )
            for model_name, u in sorted(
                model_acc.items(),
                key=lambda kv: kv[1].input_tokens + kv[1].output_tokens,
                reverse=True,
            )
        ]
        by_type = [
            _type_usage_row(
                type_name,
                u.input_tokens,
                u.output_tokens,
                u.cache_read_tokens,
                u.cache_write_tokens,
                u.request_count,
                include_total=True,
            )
            for type_name, u in sorted(
                type_acc.items(),
                key=lambda kv: kv[1].input_tokens + kv[1].output_tokens,
                reverse=True,
            )
        ]

        return {
            "start_date": start_str,
            "end_date": end_str,
            "days": len(daily),
            "total": {
                "input_tokens": total.input_tokens,
                "output_tokens": total.output_tokens,
                "cache_read_tokens": total.cache_read_tokens,
                "cache_write_tokens": total.cache_write_tokens,
                "total_tokens": total.input_tokens + total.output_tokens,
                "total_cost": round(sum(item["cost"] for item in by_model), 6),
            },
            "efficiency": {
                "user_turn_count": sum_user_turns,
                "agent_run_count": sum_agent_runs,
                "user_turn_agent_run_count": sum_ut_agent_runs,
                "avg_tokens_per_user_turn": _safe_avg(sum_ut_in + sum_ut_out, sum_user_turns),
                "avg_tokens_per_agent_run": _safe_avg(total.input_tokens + total.output_tokens, sum_agent_runs),
                "avg_agent_runs_per_user_turn": _safe_avg(sum_ut_agent_runs, sum_user_turns),
            },
            "daily": daily,
            "by_model": by_model,
            "by_type": by_type,
            "latency": {
                "avg": (
                    _safe_avg(lat_weight_sum, lat_weight_n)
                    if lat_weight_n > 0
                    else _safe_avg(lat_day_avg_sum, lat_day_count)
                ),
                "p95": lat_peak,
                # 0 = 区间内全是迁移前的历史行，avg 是按日均估算的
                "samples": lat_weight_n,
            },
            "errors": {**err_acc, "total": sum(err_acc.values())},
        }

    async def get_efficiency(self, date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取指定日的效率指标；date 为空或今日走内存，历史走 DB。"""
        if date is None or date == self._today:
            return self._efficiency_from_state(self._bot_state)
        stats = await AIDailyStatistics.get_daily_stats(date)
        if not stats:
            return None
        return self._efficiency_from_daily_stats(stats)

    def _daily_stats_to_dict(
        self,
        stats: AIDailyStatistics,
        rag_hit: int = 0,
        rag_miss: int = 0,
        rag_hit_rate: float = 0.0,
        by_model: Optional[Sequence[_ModelUsageRow]] = None,
        by_type: Optional[Sequence[_TypeUsageRow]] = None,
        active_users: Optional[List[Dict[str, Any]]] = None,
        heartbeat: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """将 AIDailyStatistics 转换为字典格式"""
        errors = _errors_from_daily_stats(stats)
        return {
            "date": stats.date,
            "token_usage": {
                "total_input_tokens": stats.total_input_tokens or 0,
                "total_output_tokens": stats.total_output_tokens or 0,
                "total_cache_read_tokens": stats.total_cache_read_tokens or 0,
                "total_cache_write_tokens": stats.total_cache_write_tokens or 0,
                "total_cost": round(sum(item["cost"] for item in (by_model or ())), 6),
                "by_model": list(by_model or ()),
                "by_type": list(by_type or ()),
            },
            "latency": {"avg": stats.avg_latency or 0.0, "p95": stats.p95_latency or 0.0},
            "intent_distribution": _distribution_dict(
                [
                    ("chat", stats.intent_chat_count or 0),
                    ("tool", stats.intent_tool_count or 0),
                    ("qa", stats.intent_qa_count or 0),
                ]
            ),
            "errors": {**errors, "total": sum(errors.values())},
            "trigger_distribution": _distribution_dict(
                [
                    ("mention", stats.trigger_mention_count or 0),
                    ("keyword", stats.trigger_keyword_count or 0),
                    ("heartbeat", stats.trigger_heartbeat_count or 0),
                    ("scheduled", stats.trigger_scheduled_count or 0),
                ]
            ),
            "rag": {"hit_count": rag_hit, "miss_count": rag_miss, "hit_rate": rag_hit_rate},
            "memory": _memory_stats_dict(stats),
            "efficiency": self._efficiency_from_daily_stats(stats),
            "heartbeat": heartbeat or _heartbeat_dict(0, 0),
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

    # 预算用量内存账本共用本持久化节拍：把内存增量整批落库（闸门/看板读内存，不依赖此次成败）。
    from gsuid_core.ai_core.budget import budget_manager

    await budget_manager.flush()
    logger.info(i18n_t("log.ai.statisticsmanager_scheduled_persistence_minutes"))
