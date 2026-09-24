"""记忆检索模块的共享类型定义"""

from typing import Optional, TypedDict, NotRequired


class Episode(TypedDict):
    """对话片段"""

    id: str
    content: str
    valid_at: str
    scope_key: str
    embedding: list[float]
    session_id: NotRequired[str]
    turn_index: NotRequired[int]
    kind: NotRequired[str]


class Entity(TypedDict):
    """实体"""

    id: str
    name: str
    summary: str
    entity_type: str
    layer: int
    score: float


class Edge(TypedDict):
    """关系边"""

    id: str
    source_id: str
    target_id: str
    source_name: str  # source 实体名称，检索阶段填充，用于 fact 主语补全
    target_name: str  # target 实体名称，检索阶段填充
    fact: str
    weight: float
    score: float
    valid_at_ts: Optional[float]
    invalid_at_ts: Optional[float]
    expired_at_ts: NotRequired[Optional[float]]


class MemoryEventCue(TypedDict):
    """睡眠期事件线索，只当指针，证据仍是源 turn。"""

    summary: str
    stated_at: str
    event_at: str
    turn_episode_id: str
    thread_id: str
    source: NotRequired[str]


class Category(TypedDict):
    """语义类目"""

    id: str
    name: str
    summary: str
    layer: int


class RetrievalMeta(TypedDict):
    """检索元信息"""

    s1_episodes: int
    s2_episodes: int
    scope_keys: list[str]
