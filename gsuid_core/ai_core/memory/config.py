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
    """消息聚合窗口（秒）上限，超过此时间强制 flush。

    窗口越长，单个 scope 的累积消息越多、单次 flush 覆盖的对话越完整，
    抽取调用次数越少（固定 prompt 开销按调用次数支付），从而摊薄 Token。

    注意这是**上限**不是唯一出口：`idle_flush_seconds` 会在对话安静下来后提前落库，
    否则一段对话要在内存缓冲区里躺满 2 小时，core 一重启就全丢（见该字段）。
    """

    idle_flush_seconds: int = 180
    """scope 静默多久后落库（秒）。0 = 关闭，退回纯 `batch_interval_seconds` 行为。

    **为什么需要它**：flush 是唯一的落库时机，而缓冲区在进程内存里。旧实现只有
    「攒满 80 条」或「距上次 flush 满 2 小时」两个出口，于是一段几轮的对话要在内存里
    躺两小时；core 在这期间被强杀 / 重启，这段记忆就**永久消失**。实测生产库里
    真实 QQ 流量的 Episode 数为 **0**——能持久化的全部来自 webconsole / 评测端点，
    因为只有那些路径显式调了 `worker.flush_all()`。

    按「静默」而不是「固定周期」触发，是为了**不牺牲批量效率**：对话进行中一直有新消息
    进来 → 不算静默 → 不 flush，抽取仍是整段一次调用；对话结束后 3 分钟落库。
    LLM 调用次数与旧实现基本相同，但记忆的最长在险时间从 2 小时降到 3 分钟。
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

    # ====== 去重阈值 ======
    dedup_similarity_threshold: float = 0.92
    """Entity 去重余弦相似度阈值，超过则视为同一实体"""

    # ====== 程序性 / 偏好记忆：即时 flush 去抖（运行时字段，进阶可调） ======
    # 总开关 enable_preference_memory 与注入/裁剪阈值已上 MEMORY_CONFIG（见下方 @property）。
    preference_flush_settle_seconds: float = 60.0
    """同一 scope 两次优先 flush 的最小间隔，防连环纠正触发 flush 风暴。"""

    @property
    def retrieval_top_k(self) -> int:
        """最终检索数量，可以提高检索精度但会增加性能开销"""
        return mrc.get_config("retrieval_top_k").data

    @property
    def memory_inject_max_chars(self) -> int:
        """单次注入对话上下文的记忆文本最大字符数（Token 预算）"""
        return mrc.get_config("memory_inject_max_chars").data

    @property
    def memory_inject_wide_chars(self) -> int:
        """逐份材料覆盖 / 长时序题的记忆总帽（``inject_memory_cap`` 的宽档）。"""
        return self._eo_int("memory_inject_wide_chars", 48000, stored=True)

    @property
    def session_gap_seconds(self) -> int:
        """相邻 Episode 超过此时长则切新 session（秒）。"""
        return mrc.get_config("session_gap_seconds").data

    @property
    def abstention_score_threshold(self) -> float:
        """熟悉度探针 s̄ 低于此值时，span/点查注入头提示证据不足。"""
        return 0.22

    @property
    def predict_calibrate_threshold(self) -> float:
        """Phase 7：近邻余弦 ≥ 此值视为已知，HIGH 降为 LOW。"""
        return 0.92

    @property
    def sleep_extract_batch(self) -> int:
        """浅睡每轮最多消化的 session 数。"""
        return 8

    @property
    def fact_max_inject(self) -> int:
        """单次注入的核心事实条数上限（与字符预算双限取严）。"""
        return mrc.get_config("fact_max_inject").data

    @property
    def enable_system2(self) -> bool:
        """兼容保留（schema_v2 已无分层图遍历，固定返回 False）。"""
        return False

    @property
    def eval_mode(self) -> bool:
        """评测模式：启用后摄入不触发线程整理，由评测脚本统一触发。"""
        return mrc.get_config("eval_mode").data

    def _eo_int(self, key: str, default: int, *, stored: bool = False) -> int:
        import os

        env = os.environ.get(f"GSUID_{key.upper()}", "").strip()
        if env.isdigit():
            return int(env)
        if not stored:
            return default
        raw = mrc.get_config(key).data
        return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else default

    @property
    def retrieve_hook_timeout_ms(self) -> int:
        """H05 检索钩子超时。"""
        return 15_000

    # ====== 程序性 / 偏好记忆（已上 MEMORY_CONFIG，WebConsole 可调；默认开） ======
    @property
    def enable_preference_memory(self) -> bool:
        """程序性/偏好记忆总开关（默认开）：开启后纠错意图门控命中的批次会独立蒸馏偏好
        规则写 AIMemPreference 表（SQL-only），并在检索时以强约束语气置顶注入。"""
        return mrc.get_config("enable_preference_memory").data

    @property
    def preference_inject_budget_ratio(self) -> float:
        """注入时偏好区块占记忆预算的比例（置顶、强约束）。"""
        return float(mrc.get_config("preference_inject_budget_ratio").data)

    @property
    def preference_max_per_context(self) -> int:
        """单个 target_context（能力域/工具）保留的活跃规则上限（生命周期裁剪用）。"""
        return mrc.get_config("preference_max_per_context").data

    @property
    def preference_max_inject(self) -> int:
        """单次注入的偏好规则条数总上限，防 Token 膨胀。"""
        return mrc.get_config("preference_max_inject").data

    @property
    def preference_immediate_flush(self) -> bool:
        """纠错命中即时写：命中纠错意图的 scope 走优先 flush 快路径，
        让数分钟内的"下一次"请求即可召回，而非等 batch_interval_seconds 大窗。"""
        return mrc.get_config("preference_immediate_flush").data

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
