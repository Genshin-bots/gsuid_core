from typing import Dict, List
from collections import Counter, defaultdict
from dataclasses import field, dataclass


@dataclass
class TokenUsage:
    """Token 使用量统计"""

    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, input_tokens: int, output_tokens: int):
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens


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
    total_tokens: Counter = field(default_factory=Counter)  # keys: input, output
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
    memory_retrievals: int = 0  # 检索请求总数
    memory_entities_created: int = 0  # 新建 Entity 总数
    memory_edges_created: int = 0  # 新建 Edge 总数
    memory_episodes_created: int = 0  # 新建 Episode 总数
