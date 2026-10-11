"""Qdrant 调用用到的值对象。远程客户端把它们编成 JSON，不再导入 qdrant-client。"""

from __future__ import annotations

import enum
from uuid import UUID
from typing import Mapping, Protocol, Sequence
from dataclasses import dataclass


class Distance(enum.Enum):
    COSINE = "Cosine"
    EUCLID = "Euclid"
    DOT = "Dot"
    MANHATTAN = "Manhattan"


class Modifier(enum.Enum):
    IDF = "idf"
    NONE = "none"


class Fusion(enum.Enum):
    RRF = "rrf"


class PayloadSchemaType(enum.Enum):
    KEYWORD = "keyword"
    INTEGER = "integer"
    FLOAT = "float"
    BOOL = "bool"
    GEO = "geo"
    TEXT = "text"
    DATETIME = "datetime"


PointId = str | int | UUID
ExtendedPointId = PointId


@dataclass
class SparseVector:
    indices: list[int]
    values: list[float]


Vector = list[float] | SparseVector


@dataclass
class VectorParams:
    size: int
    distance: Distance | str = Distance.COSINE
    on_disk: bool | None = None


@dataclass
class VectorParamsDiff:
    on_disk: bool | None = None


@dataclass
class SparseVectorParams:
    modifier: Modifier | str | None = None


@dataclass
class MatchValue:
    value: str | int | float | bool


@dataclass
class MatchAny:
    any: Sequence[str | int | float | bool]


@dataclass
class Range:
    lt: float | None = None
    gt: float | None = None
    lte: float | None = None
    gte: float | None = None


@dataclass
class FieldCondition:
    key: str
    match: MatchValue | MatchAny | None = None
    range: Range | None = None


@dataclass
class HasIdCondition:
    has_id: Sequence[PointId]


Condition = FieldCondition | HasIdCondition


@dataclass
class Filter:
    must: Sequence[Condition] | None = None
    should: Sequence[Condition] | None = None
    must_not: Sequence[Condition] | None = None


@dataclass
class PointStruct:
    id: PointId
    vector: list[float] | SparseVector | dict[str, list[float] | SparseVector]
    payload: Mapping[str, object] | None = None


@dataclass
class PointIdsList:
    points: Sequence[PointId]


@dataclass
class Prefetch:
    query: list[float] | SparseVector
    using: str | None = None
    filter: Filter | None = None
    score_threshold: float | None = None
    limit: int = 10


@dataclass
class FusionQuery:
    fusion: Fusion


@dataclass
class ScoredPoint:
    id: PointId
    version: int
    score: float
    payload: Mapping[str, object] | None = None
    vector: list[float] | SparseVector | dict[str, list[float] | SparseVector] | None = None


@dataclass
class QueryResponse:
    points: list[ScoredPoint]


@dataclass
class CountResult:
    count: int


@dataclass
class CollectionDescription:
    name: str


@dataclass
class CollectionsResponse:
    collections: list[CollectionDescription]


@dataclass
class PayloadIndexInfo:
    data_type: str


@dataclass
class CollectionParams:
    vectors: VectorParams | dict[str, VectorParams]
    sparse_vectors: dict[str, SparseVectorParams] | None
    on_disk_payload: bool


@dataclass
class CollectionConfig:
    params: CollectionParams


@dataclass
class CollectionInfo:
    config: CollectionConfig
    payload_schema: dict[str, PayloadIndexInfo]


class QdrantSession(Protocol):
    """远程 HTTP 客户端与本地嵌入式客户端的共同调用面。"""

    _client: object | None

    def local_collection_map(self) -> dict[str, object] | None: ...

    def release_embedded_cache(self) -> None: ...

    async def close(self) -> None: ...

    async def collection_exists(self, collection_name: str) -> bool: ...

    async def get_collections(self) -> CollectionsResponse: ...

    async def get_collection(self, collection_name: str) -> CollectionInfo: ...

    async def create_collection(
        self,
        *,
        collection_name: str,
        vectors_config: VectorParams | dict[str, VectorParams],
        sparse_vectors_config: dict[str, SparseVectorParams] | None = None,
        on_disk_payload: bool = True,
    ) -> None: ...

    async def delete_collection(self, *, collection_name: str) -> None: ...

    async def update_collection(
        self,
        *,
        collection_name: str,
        vectors_config: dict[str, VectorParamsDiff],
    ) -> None: ...

    async def recreate_collection(
        self,
        *,
        collection_name: str,
        vectors_config: VectorParams | dict[str, VectorParams],
        sparse_vectors_config: dict[str, SparseVectorParams] | None = None,
        on_disk_payload: bool = True,
    ) -> None: ...

    async def create_payload_index(
        self,
        *,
        collection_name: str,
        field_name: str,
        field_schema: PayloadSchemaType | str,
    ) -> None: ...

    async def upsert(
        self,
        *,
        collection_name: str,
        points: Sequence[PointStruct],
        wait: bool = True,
    ) -> None: ...

    async def delete(
        self,
        *,
        collection_name: str,
        points_selector: PointIdsList | Filter | Sequence[PointId],
        wait: bool = True,
    ) -> None: ...

    async def query_points(
        self,
        *,
        collection_name: str,
        query: list[float] | SparseVector | FusionQuery,
        using: str | None = None,
        query_filter: Filter | None = None,
        limit: int = 10,
        score_threshold: float | None = None,
        with_payload: bool | list[str] | None = None,
        with_vectors: bool | list[str] | None = None,
        prefetch: list[Prefetch] | None = None,
    ) -> QueryResponse: ...

    async def scroll(
        self,
        *,
        collection_name: str,
        limit: int = 10,
        offset: PointId | None = None,
        with_payload: bool | Sequence[str] = True,
        with_vectors: bool | Sequence[str] = False,
        scroll_filter: Filter | None = None,
    ) -> tuple[list[ScoredPoint], PointId | None]: ...

    async def count(
        self,
        *,
        collection_name: str,
        count_filter: Filter | None = None,
        exact: bool = True,
    ) -> CountResult: ...

    async def retrieve(
        self,
        *,
        collection_name: str,
        ids: Sequence[PointId],
        with_payload: bool = True,
        with_vectors: bool | list[str] = False,
    ) -> list[ScoredPoint]: ...

    async def set_payload(
        self,
        *,
        collection_name: str,
        payload: Mapping[str, object],
        points: Sequence[PointId],
        wait: bool = True,
    ) -> None: ...
