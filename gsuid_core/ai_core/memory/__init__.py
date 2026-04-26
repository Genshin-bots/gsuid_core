"""多群组 / 多用户 Agent 记忆系统

基于 Mnemis 双路检索思想，适配 gsuid_core 单进程架构。
存储层：SQLAlchemy（图结构） + Qdrant（向量索引，复用现有 rag/base.py）

主要组件：
- scope: Scope Key 体系（群组隔离）
- config: 记忆系统全局配置
- observer: 消息观察者（被动感知层）
- database: SQLAlchemy 数据模型
- vector: Qdrant 向量操作
- ingestion: 摄入引擎（Episode/Entity/Edge 提取与写入）
- retrieval: 双路检索引擎（System-1 向量 + System-2 分层图）
- prompts: LLM 提示词模板
- startup: 初始化入口
"""

from .scope import ScopeType, make_scope_key
from .config import memory_config
from .startup import *  # noqa: F401, F403
from .startup import get_ingestion_worker
from .observer import ObservationRecord, observe, get_observation_queue
from .retrieval.dual_route import MemoryContext, dual_route_retrieve

__all__ = [
    "memory_config",
    "ScopeType",
    "make_scope_key",
    "observe",
    "get_observation_queue",
    "ObservationRecord",
    "dual_route_retrieve",
    "MemoryContext",
    "get_ingestion_worker",
]
