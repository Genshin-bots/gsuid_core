"""记忆系统配置模块

管理记忆系统的全局配置项，包括观察者开关、检索参数、黑名单等。
配置项可通过 ai_config 系统动态读取。
"""

from typing import List
from dataclasses import field, dataclass

from gsuid_core.ai_core.configs.ai_config import memory_config as mrc


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

    batch_interval_seconds: int = 3600
    """消息聚合窗口（秒），超过此时间强制 flush"""

    batch_max_size: int = 40
    """单次最大聚合条数，防止单个 LLM 调用 token 超限"""

    llm_semaphore_limit: int = 3
    """同时进行的 LLM 调用上限"""

    # ====== 检索（Retrieval）配置 ======
    enable_retrieval: bool = True
    """是否启用记忆检索，关闭后 AI 回复不注入记忆上下文"""

    enable_user_global_memory: bool = True
    """是否联合查询用户跨群画像"""

    enable_heartbeat_memory: bool = True
    """是否在 Heartbeat 决策中注入群组摘要缓存"""

    search_edge_count: int = 30
    """Edge 搜索结果数量上限"""

    min_edge_weight: float = 0.0
    """【置信度轴】注入核心事实时过滤 weight（事实可信度）低于此值的 Edge。

    weight 与"相关性"是两个独立维度：weight 衡量"这条事实有多可信"、与 query 无关；
    相关性由 min_edge_rerank_score 负责。weight 由检索期 compute_edge_confidence 现场折算
    = 佐证(mention_count) × 新鲜度(decay_score) ∈ (0, 1]，单次提及的新鲜事实约 0.5。
    默认 0.0=不过滤；可调高收紧低置信事实（如 0.4 ≈ 至少需两次佐证或一次新鲜提及）。
    """

    min_edge_rerank_score: float = 0.0
    """【相关性轴】注入核心事实时过滤 Reranker 相关性分数低于此值的 Edge。

    与 weight（置信度）正交：此值衡量"这条事实与当前 query 有多相关"，由 Reranker 现场打分。
    默认 0.0：仅剔除被 Reranker 判为"负相关/完全无关"的边（交叉编码器对无关文本给负分），
    对输出 0~1 归一化分数的远程 Reranker 等价于不过滤，可按需调高（如 0.3）收紧弱相关事实。
    无 Reranker 时无相关性信号，不做此过滤。
    """

    # ====== 去重与冲突阈值 ======
    dedup_similarity_threshold: float = 0.92
    """Entity 去重余弦相似度阈值，超过则视为同一实体"""

    edge_conflict_threshold: float = 0.88
    """Edge 语义冲突判断阈值，低于 Entity 阈值更宽松"""

    # ====== 分层图（Hierarchical Graph）配置 ======
    min_children_per_category: int = 3
    """每个 Category 至少包含的子节点数（压缩效率约束）"""

    max_layers: int = 3
    """分层图最大层数"""

    hiergraph_rebuild_ratio: float = 2.50
    """Entity 增长超过此比例时触发增量重建"""

    hiergraph_rebuild_interval_seconds: int = 172800
    """距上次重建超过此秒数时触发增量重建（默认 48h）"""

    @property
    def retrieval_top_k(self) -> int:
        """最终检索数量，可以提高检索精度但会增加性能开销"""
        return mrc.get_config("retrieval_top_k").data

    @property
    def memory_inject_max_chars(self) -> int:
        """单次注入对话上下文的记忆文本最大字符数（Token 预算）"""
        return mrc.get_config("memory_inject_max_chars").data

    @property
    def enable_system2(self) -> bool:
        """是否启用 System-2 全局选择（成本较高，可按需关闭）"""
        return mrc.get_config("enable_system2get").data

    @property
    def eval_mode(self) -> bool:
        """评测模式：启用后摄入时不自动触发分层图重建，由外部统一调用 rebuild_task"""
        return mrc.get_config("eval_mode").data

    @property
    def memory_mode(self) -> list[str]:
        """记忆路径

        指定启用的记忆路径, 被动感知全部群友会话或只记住自己有参与的聊天记录
        """
        return mrc.get_config("memory_mode").data

    @property
    def memory_session(self) -> str:
        """被动感知范围

        指定被动感知的范围
        """
        return mrc.get_config("memory_session").data


# 全局单例
memory_config = MemoryConfig()
