"""记忆系统配置模块

管理记忆系统的全局配置项，包括观察者开关、检索参数、黑名单等。
配置项可通过 ai_config 系统动态读取。
"""

from typing import List
from dataclasses import field, dataclass


@dataclass
class MemoryConfig:
    """记忆系统全局配置

    所有配置项均有合理默认值，可在运行时动态修改。
    """

    # ====== 观察者（Observer）配置 ======
    observer_enabled: bool = True
    """是否启用消息观察者，关闭后不再入队任何消息"""

    observer_blacklist: List[str] = field(default_factory=list)
    """观察者黑名单群组 ID 列表，这些群组的消息不会被记忆"""

    # ====== 摄入（Ingestion）配置 ======
    ingestion_enabled: bool = True
    """是否启用摄入引擎，关闭后消息入队但不处理"""

    batch_interval_seconds: int = 1800
    """消息聚合窗口（秒），超过此时间强制 flush"""

    batch_max_size: int = 30
    """单次最大聚合条数，防止单个 LLM 调用 token 超限"""

    llm_semaphore_limit: int = 2
    """同时进行的 LLM 调用上限"""

    # ====== 检索（Retrieval）配置 ======
    enable_retrieval: bool = True
    """是否启用记忆检索，关闭后 AI 回复不注入记忆上下文"""

    enable_system2: bool = True
    """是否启用 System-2 全局选择（成本较高，可按需关闭）"""

    enable_user_global_memory: bool = False
    """是否联合查询用户跨群画像"""

    enable_heartbeat_memory: bool = True
    """是否在 Heartbeat 决策中注入群组摘要缓存"""

    retrieval_top_k: int = 10
    """最终返回的 Episode 数量上限"""

    # ====== 去重与冲突阈值 ======
    dedup_similarity_threshold: float = 0.92
    """Entity 去重余弦相似度阈值，超过则视为同一实体"""

    edge_conflict_threshold: float = 0.88
    """Edge 语义冲突判断阈值，低于 Entity 阈值更宽松"""

    # ====== 分层图（Hierarchical Graph）配置 ======
    min_children_per_category: int = 3
    """每个 Category 至少包含的子节点数（压缩效率约束）"""

    max_layers: int = 5
    """分层图最大层数"""

    hiergraph_rebuild_ratio: float = 1.10
    """Entity 增长超过此比例时触发增量重建"""

    hiergraph_rebuild_interval_seconds: int = 86400
    """距上次重建超过此秒数时触发增量重建（默认 24h）"""


# 全局单例
memory_config = MemoryConfig()
