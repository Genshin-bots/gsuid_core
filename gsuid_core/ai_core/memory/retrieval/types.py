"""记忆检索模块的共享类型定义"""

from typing import Optional, TypedDict


class Episode(TypedDict):
    """对话片段"""

    id: str
    content: str
    valid_at: str
    scope_key: str
    embedding: list[float]


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
    fact: str
    weight: float
    score: float
    invalid_at_ts: Optional[float]


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
