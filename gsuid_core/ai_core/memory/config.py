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

    batch_interval_seconds: int = 7200
    """消息聚合窗口（秒），超过此时间强制 flush。

    窗口越长，单个 scope 的累积消息越多、单次 flush 覆盖的对话越完整，
    抽取调用次数越少（固定 prompt 开销按调用次数支付），从而摊薄 Token。
    """

    batch_max_size: int = 80
    """单次最大聚合条数，防止单个 LLM 调用 token 超限。

    调大可让每次抽取覆盖更多消息、减少调用次数以摊薄固定开销；
    但受 `_llm_extract` 的 MAX_CHARS 上限约束，过大会触发分片反而增加调用。
    """

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

    @property
    def background_episode_count(self) -> int:
        """实体抽取时注入的近期对话片段（Episode）数量。

        用于跨批次指代消解；调小可显著降低每次抽取的 Token 开销
        （原始信息仍由 Episode 完整留存），0 表示不注入背景。
        """
        return mrc.get_config("background_episode_count").data

    @property
    def background_episode_max_chars(self) -> int:
        """每条注入的近期对话片段在抽取提示词中的最大字符数（超出截断）"""
        return mrc.get_config("background_episode_max_chars").data

    @property
    def extraction_value_gate(self) -> str:
        """抽取价值门控档位（宽松 / 均衡 / 严格）。

        决定哪些消息触发 LLM 实体抽取；无论档位，原文都完整存为 Episode 不丢信息。
        """
        return mrc.get_config("extraction_value_gate").data

    @property
    def hiergraph_build_mode(self) -> str:
        """分层图构建模式（自动 / 始终 / 仅摘要 / 关闭）。

        分层类目树仅被 System-2 检索消费；非"始终"模式下可跳过 Layer-1/2/3 的 LLM
        分类，大幅削减重建 Token（Episode/Entity/Edge 等记忆本体不受影响）。
        """
        return mrc.get_config("hiergraph_build_mode").data

    @property
    def hiergraph_batch_size(self) -> int:
        """建树时每次 LLM 分类的节点数。

        调大可减少单轮 LLM 调用次数、摊薄每批重发的固定开销，但过大有超时风险。
        """
        return mrc.get_config("hiergraph_batch_size").data

    @property
    def hiergraph_vector_assign_threshold(self) -> float:
        """建树时向量预分配的余弦相似度阈值（配置为字符串，此处解析为 float）。

        调低可让更多实体走零 LLM 的预分配路径以省 Token，代价是误归类风险上升。
        """
        return float(mrc.get_config("hiergraph_vector_assign_threshold").data)

    @property
    def hiergraph_min_entities(self) -> int:
        """分层图最小实体门槛：scope 实体数低于此值则整体跳过分层图（含轻量群摘要）。"""
        return mrc.get_config("hiergraph_min_entities").data

    @property
    def hiergraph_max_existing_cats(self) -> int:
        """建树分类时每批最多带入的已有类目数（仅名称），上限越小越省 Token。"""
        return mrc.get_config("hiergraph_max_existing_cats").data

    @property
    def hiergraph_node_summary_chars(self) -> int:
        """建树分类时每个待分类节点附带的实体摘要字符上限（0 表示不带摘要）。"""
        return mrc.get_config("hiergraph_node_summary_chars").data

    @property
    def hiergraph_summary_delta(self) -> int:
        """群摘要刷新的新增实体阈值：达此增量才重算群摘要，调大更省 Token。"""
        return mrc.get_config("hiergraph_summary_delta").data


# 全局单例
memory_config = MemoryConfig()
