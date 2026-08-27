"""认知层：统一节点身份 + 联邦单入口检索。

背景（记忆耦合诊断）：存储层已经是多套专业库（对模块作者清晰），但对 Agent 是杂乱的——
没有跨库节点身份、没有单检索入口、自动注入只喂记忆碎片。于是 Agent 想「查全」的最小路径
是三四次往返、三份 schema、三份可能为空的正文。

本包做的是「**把节点身份与检索面合成一层**，而不是把表合成一张」：

- :mod:`types`  ``CogKind`` / ``CogScope`` / ``CognitiveHit`` 契约
- :mod:`facade` ``search_cognition`` 联邦检索 + 渲染
- :mod:`remember` 统一写契约（索引层，不搬正文）
- :mod:`nodes`  认知节点表与跨 kind 边（索引与关系层，正文不搬家）
"""

from gsuid_core.ai_core.cognition.hub import expand_hub, run_cognition_mount, rebuild_cognition_mount
from gsuid_core.ai_core.cognition.types import (
    ALL_KINDS,
    KIND_LABEL,
    WORK_KINDS,
    MEDIA_KINDS,
    MEMORY_KINDS,
    KNOWLEDGE_KINDS,
    DEFAULT_RECALL_KINDS,
    SPEAKER_RECALL_KINDS,
    CogKind,
    CogScope,
    CognitiveHit,
)
from gsuid_core.ai_core.cognition.facade import (
    kinds_from_names,
    search_cognition,
    probe_handle_alive,
    resolve_recall_kinds,
    query_mentions_speaker,
    render_cognition_block,
)
from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember

__all__ = [
    "ALL_KINDS",
    "DEFAULT_RECALL_KINDS",
    "KIND_LABEL",
    "KNOWLEDGE_KINDS",
    "MEDIA_KINDS",
    "MEMORY_KINDS",
    "SPEAKER_RECALL_KINDS",
    "WORK_KINDS",
    "CogKind",
    "CogScope",
    "CognitiveHit",
    "MemoryWrite",
    "expand_hub",
    "kinds_from_names",
    "probe_handle_alive",
    "query_mentions_speaker",
    "rebuild_cognition_mount",
    "remember",
    "render_cognition_block",
    "resolve_recall_kinds",
    "run_cognition_mount",
    "search_cognition",
]
