"""记忆检索共享类型。"""

from typing import Optional, TypedDict, NotRequired


class Episode(TypedDict):
    id: str
    content: str
    valid_at: str
    scope_key: str
    embedding: list[float]
    session_id: NotRequired[str]
    thread_id: NotRequired[str]
    turn_index: NotRequired[int]
    event_at: NotRequired[str]
    speaker_summary: NotRequired[str]
    kind: NotRequired[str]


class Entity(TypedDict):
    id: str
    name: str
    summary: str
    entity_type: str
    layer: int
    score: float


class Edge(TypedDict):
    id: str
    source_id: str
    target_id: str
    source_name: str
    target_name: str
    fact: str
    weight: float
    score: float
    valid_at_ts: Optional[float]
    invalid_at_ts: Optional[float]
    expired_at_ts: NotRequired[Optional[float]]
    edge_kind: NotRequired[str]
    slot_key: NotRequired[str]
    resolution_state: NotRequired[str]
    superseded_by_id: NotRequired[Optional[str]]
    contradicts_edge_id: NotRequired[Optional[str]]
    conflict_note: NotRequired[str]
    provenance_tier: NotRequired[str]
    event_at_ts: NotRequired[Optional[float]]


class ThreadSummary(TypedDict):
    id: str
    title: str
    summary: str
    status: str
    milestone_count: int
    last_active_at: str


class MemoryEventCue(TypedDict):
    summary: str
    stated_at: str
    event_at: str
    turn_episode_id: str
    thread_id: str
    source: NotRequired[str]


class Category(TypedDict):
    id: str
    name: str
    summary: str
    layer: int


class RetrievalMeta(TypedDict):
    s1_episodes: int
    s2_episodes: int
    scope_keys: list[str]
