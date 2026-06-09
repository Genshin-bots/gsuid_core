from typing import Dict, List
from collections import Counter, defaultdict
from dataclasses import field, dataclass


@dataclass
class TokenUsage:
    """Token 使用量统计"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens


@dataclass
class LatencyStats:
    """延迟统计"""

    latencies: List[float] = field(default_factory=list)

    def add(self, latency: float):
        self.latencies.append(latency)

    @property
    def avg(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0

    @property
    def p95(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_latencies = sorted(self.latencies)
        index = int(len(sorted_latencies) * 0.95)
        return sorted_latencies[min(index, len(sorted_latencies) - 1)]


@dataclass
class BotState:
    """单个 Bot 的所有内存统计状态"""

    # 基础统计 (使用 Counter 简化累加逻辑)
    total_tokens: Counter = field(default_factory=Counter)  # keys: input, output, cache_read, cache_write
    intents: Counter = field(default_factory=Counter)  # keys: chat, tool, qa
    errors: Counter = field(default_factory=Counter)  # keys: timeout, rate_limit...
    triggers: Counter = field(default_factory=Counter)  # keys: mention, keyword...

    # 模型细分统计
    token_by_model: Dict[str, TokenUsage] = field(default_factory=lambda: defaultdict(TokenUsage))
    token_by_type: Dict[str, TokenUsage] = field(default_factory=lambda: defaultdict(TokenUsage))

    # 性能统计
    latencies: LatencyStats = field(default_factory=LatencyStats)

    # 嵌套/ID 相关统计 (key 为关联 ID，value 为 Counter)
    heartbeats: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    activities: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))

    # 记忆系统统计
    memory_observations: int = 0  # 观察入队总数
    memory_ingestions: int = 0  # 摄入完成总数
    memory_ingestion_errors: int = 0  # 摄入失败总数
    memory_extraction_errors: int = 0  # 提取失败总数
    memory_retrievals: int = 0  # 检索请求总数
    memory_entities_created: int = 0  # 新建 Entity 总数
    memory_edges_created: int = 0  # 新建 Edge 总数
    memory_episodes_created: int = 0  # 新建 Episode 总数


@dataclass
class HourlyPerformanceEntry:
    """小时级模型请求性能与 Token 统计（内存缓冲用）

    TTFT/TPS 只统计 >0 的有效样本（sample_count 单独计数），
    避免无文本输出的请求把 min 顶成 0、把 avg 拉低。
    min/max 以 sample_count == 0 作为"未赋值"判据，而非 0.0 哨兵值。
    """

    request_count: int = 0
    ttft_ms_min: float = 0.0
    ttft_ms_max: float = 0.0
    ttft_ms_sum: float = 0.0
    ttft_sample_count: int = 0
    tps_min: float = 0.0
    tps_max: float = 0.0
    tps_sum: float = 0.0
    tps_sample_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_call_count: int = 0

    @property
    def ttft_ms_avg(self) -> float:
        return round(self.ttft_ms_sum / self.ttft_sample_count, 2) if self.ttft_sample_count > 0 else 0.0

    @property
    def tps_avg(self) -> float:
        return round(self.tps_sum / self.tps_sample_count, 2) if self.tps_sample_count > 0 else 0.0

    def _add_ttft(self, ttft_ms: float) -> None:
        if ttft_ms <= 0:
            return
        if self.ttft_sample_count == 0 or ttft_ms < self.ttft_ms_min:
            self.ttft_ms_min = ttft_ms
        if ttft_ms > self.ttft_ms_max:
            self.ttft_ms_max = ttft_ms
        self.ttft_ms_sum += ttft_ms
        self.ttft_sample_count += 1

    def _add_tps(self, tps: float) -> None:
        if tps <= 0:
            return
        if self.tps_sample_count == 0 or tps < self.tps_min:
            self.tps_min = tps
        if tps > self.tps_max:
            self.tps_max = tps
        self.tps_sum += tps
        self.tps_sample_count += 1

    def update(
        self,
        *,
        ttft_ms: float = 0.0,
        tps: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        tool_call_count: int = 0,
    ) -> None:
        """累加一次模型请求的统计"""
        self.request_count += 1
        self._add_ttft(ttft_ms)
        self._add_tps(tps)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens
        self.tool_call_count += tool_call_count

    def merge(self, other: "HourlyPerformanceEntry") -> None:
        """合并另一份聚合统计（用于 DB 基线数据与内存增量叠加、失败回滚）"""
        self.request_count += other.request_count
        if other.ttft_sample_count > 0:
            if self.ttft_sample_count == 0 or other.ttft_ms_min < self.ttft_ms_min:
                self.ttft_ms_min = other.ttft_ms_min
            if other.ttft_ms_max > self.ttft_ms_max:
                self.ttft_ms_max = other.ttft_ms_max
            self.ttft_ms_sum += other.ttft_ms_sum
            self.ttft_sample_count += other.ttft_sample_count
        if other.tps_sample_count > 0:
            if self.tps_sample_count == 0 or other.tps_min < self.tps_min:
                self.tps_min = other.tps_min
            if other.tps_max > self.tps_max:
                self.tps_max = other.tps_max
            self.tps_sum += other.tps_sum
            self.tps_sample_count += other.tps_sample_count
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.tool_call_count += other.tool_call_count
