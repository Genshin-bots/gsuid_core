"""本地嵌入式 Qdrant。只有这条路径才导入 qdrant-client。"""

from __future__ import annotations

import enum
from typing import Mapping, TypeVar, Protocol, Sequence, Awaitable, runtime_checkable
from pathlib import Path

from qdrant_client import models as qm

from gsuid_core.ai_core.rag.qhttp import UnexpectedResponse, ResponseHandlingException
from gsuid_core.ai_core.rag.qmodels import (
    Range,
    Filter,
    PointId,
    Distance,
    MatchAny,
    Modifier,
    Prefetch,
    MatchValue,
    CountResult,
    FusionQuery,
    PointStruct,
    ScoredPoint,
    PointIdsList,
    SparseVector,
    VectorParams,
    QueryResponse,
    CollectionInfo,
    FieldCondition,
    HasIdCondition,
    CollectionConfig,
    CollectionParams,
    PayloadIndexInfo,
    VectorParamsDiff,
    PayloadSchemaType,
    SparseVectorParams,
    CollectionsResponse,
    CollectionDescription,
)


def _distance(value: Distance | str) -> qm.Distance:
    text = value.value if isinstance(value, Distance) else value
    return qm.Distance(text)


def _modifier(value: Modifier | str | None) -> qm.Modifier | None:
    if value is None:
        return None
    text = value.value if isinstance(value, Modifier) else value
    return qm.Modifier(text)


def _vectors(
    value: VectorParams | dict[str, VectorParams],
) -> qm.VectorParams | dict[str, qm.VectorParams]:
    if isinstance(value, dict):
        return {
            key: qm.VectorParams(size=item.size, distance=_distance(item.distance), on_disk=item.on_disk)
            for key, item in value.items()
        }
    return qm.VectorParams(size=value.size, distance=_distance(value.distance), on_disk=value.on_disk)


def _sparse_map(value: dict[str, SparseVectorParams] | None) -> dict[str, qm.SparseVectorParams] | None:
    if value is None:
        return None
    return {key: qm.SparseVectorParams(modifier=_modifier(item.modifier)) for key, item in value.items()}


def _diff_map(value: dict[str, VectorParamsDiff]) -> dict[str, qm.VectorParamsDiff]:
    return {key: qm.VectorParamsDiff(on_disk=item.on_disk) for key, item in value.items()}


def _schema(value: PayloadSchemaType | str) -> qm.PayloadSchemaType:
    text = value.value if isinstance(value, PayloadSchemaType) else value
    return qm.PayloadSchemaType(text)


def _match(value: MatchValue | MatchAny | None) -> qm.MatchValue | qm.MatchAny | None:
    if isinstance(value, MatchValue):
        if isinstance(value.value, str):
            return qm.MatchValue(value=value.value)
        if isinstance(value.value, bool):
            return qm.MatchValue(value=int(value.value))
        if isinstance(value.value, int):
            return qm.MatchValue(value=value.value)
        return qm.MatchValue(value=str(value.value))
    if isinstance(value, MatchAny):
        texts = [item for item in value.any if isinstance(item, str)]
        if len(texts) == len(list(value.any)):
            return qm.MatchAny(any=texts)
        numbers = [item for item in value.any if isinstance(item, int) and not isinstance(item, bool)]
        return qm.MatchAny(any=numbers)
    return None


def _bounds(value: Range | None) -> qm.Range | None:
    if value is None:
        return None
    return qm.Range(lt=value.lt, gt=value.gt, lte=value.lte, gte=value.gte)


def _condition(value: FieldCondition | HasIdCondition) -> qm.FieldCondition | qm.HasIdCondition:
    if isinstance(value, HasIdCondition):
        return qm.HasIdCondition(has_id=list(value.has_id))
    return qm.FieldCondition(key=value.key, match=_match(value.match), range=_bounds(value.range))


def _filter(value: Filter | None) -> qm.Filter | None:
    if value is None:
        return None
    return qm.Filter(
        must=[_condition(item) for item in value.must] if value.must is not None else None,
        should=[_condition(item) for item in value.should] if value.should is not None else None,
        must_not=[_condition(item) for item in value.must_not] if value.must_not is not None else None,
    )


def _named_vector(
    value: list[float] | SparseVector | dict[str, list[float] | SparseVector],
) -> list[float] | qm.SparseVector | dict[str, list[float] | qm.SparseVector]:
    if isinstance(value, SparseVector):
        return qm.SparseVector(indices=list(value.indices), values=list(value.values))
    if isinstance(value, dict):
        named: dict[str, list[float] | qm.SparseVector] = {}
        for key, item in value.items():
            converted = _named_vector(item)
            if isinstance(converted, list) or isinstance(converted, qm.SparseVector):
                named[key] = converted
        return named
    return [float(item) for item in value]


def _points(value: Sequence[PointStruct]) -> list[qm.PointStruct]:
    encoded: list[qm.PointStruct] = []
    for item in value:
        vector = _named_vector(item.vector)
        payload = dict(item.payload) if item.payload is not None else None
        if isinstance(vector, list):
            encoded.append(qm.PointStruct(id=item.id, vector=vector, payload=payload))
        elif isinstance(vector, qm.SparseVector):
            # stub 的 VectorStruct 不包含这个已收窄的稀疏向量。
            encoded.append(qm.PointStruct(id=item.id, vector=vector, payload=payload))  # type: ignore[arg-type]
        else:
            # stub 的 VectorStruct 不包含命名向量 dict。
            encoded.append(qm.PointStruct(id=item.id, vector=vector, payload=payload))  # type: ignore[arg-type]
    return encoded


def _selector(value: PointIdsList | Filter | Sequence[PointId]) -> qm.PointIdsList | qm.Filter | list[PointId]:
    if isinstance(value, Filter):
        converted = _filter(value)
        if converted is None:
            return qm.Filter()
        return converted
    if isinstance(value, PointIdsList):
        return qm.PointIdsList(points=list(value.points))
    return list(value)


def _query(value: list[float] | SparseVector | FusionQuery) -> list[float] | qm.SparseVector | qm.FusionQuery:
    if isinstance(value, FusionQuery):
        return qm.FusionQuery(fusion=qm.Fusion.RRF)
    if isinstance(value, SparseVector):
        return qm.SparseVector(indices=list(value.indices), values=list(value.values))
    return [float(item) for item in value]


def _prefetch_query(value: list[float] | SparseVector) -> list[float] | qm.SparseVector:
    if isinstance(value, SparseVector):
        return qm.SparseVector(indices=list(value.indices), values=list(value.values))
    return [float(item) for item in value]


def _prefetch(value: Prefetch) -> qm.Prefetch:
    return qm.Prefetch(
        query=_prefetch_query(value.query),
        using=value.using,
        filter=_filter(value.filter),
        score_threshold=value.score_threshold,
        limit=value.limit,
    )


def _with_names(value: bool | Sequence[str]) -> bool | list[str]:
    if isinstance(value, bool):
        return value
    return [item for item in value if isinstance(item, str)]


class _PointLike(Protocol):
    id: object
    version: object
    score: object
    payload: object
    vector: object


class _VectorObj(Protocol):
    size: object
    distance: object
    on_disk: object


class _ParamsObj(Protocol):
    vectors: object
    sparse_vectors: object
    on_disk_payload: object


class _ConfigObj(Protocol):
    params: _ParamsObj


class _InfoObj(Protocol):
    config: _ConfigObj
    payload_schema: object


@runtime_checkable
class _SchemaObj(Protocol):
    data_type: object


_T = TypeVar("_T")


def _plain_vector(vector: object) -> list[float] | SparseVector | dict[str, list[float] | SparseVector] | None:
    if vector is None:
        return None
    if isinstance(vector, qm.SparseVector):
        return SparseVector(indices=[int(i) for i in vector.indices], values=[float(v) for v in vector.values])
    if isinstance(vector, list):
        return [float(item) for item in vector]
    if isinstance(vector, dict):
        named: dict[str, list[float] | SparseVector] = {}
        for key, val in vector.items():
            parsed = _plain_vector(val)
            if isinstance(parsed, list) or isinstance(parsed, SparseVector):
                named[str(key)] = parsed
        return named
    return None


def _plain_point(point: qm.ScoredPoint | qm.Record) -> ScoredPoint:
    point_id = point.id
    parsed_id: PointId
    if isinstance(point_id, int) and not isinstance(point_id, bool):
        parsed_id = point_id
    else:
        parsed_id = str(point_id)
    version = 0
    score = 0.0
    if isinstance(point, qm.ScoredPoint):
        if isinstance(point.version, int):
            version = point.version
        if isinstance(point.score, (int, float)) and not isinstance(point.score, bool):
            score = float(point.score)
    payload = point.payload
    parsed_payload = dict(payload) if isinstance(payload, dict) else None
    return ScoredPoint(
        id=parsed_id,
        version=version,
        score=score,
        payload=parsed_payload,
        vector=_plain_vector(point.vector),
    )


def _plain_params(params: qm.VectorParams) -> VectorParams:
    if isinstance(params, VectorParams):
        return params
    size = params.size
    distance = params.distance
    on_disk = params.on_disk
    dist: Distance | str
    if isinstance(distance, str):
        dist = distance
    elif isinstance(distance, enum.Enum):
        raw = distance.value
        dist = raw if isinstance(raw, str) else "Cosine"
    else:
        dist = "Cosine"
    disk = on_disk if isinstance(on_disk, bool) else None
    size_int = int(size) if isinstance(size, int) else 0
    return VectorParams(size=size_int, distance=dist, on_disk=disk)


def _plain_collection(info: qm.CollectionInfo) -> CollectionInfo:
    params = info.config.params
    vectors = params.vectors
    if isinstance(vectors, dict):
        named = {str(key): _plain_params(val) for key, val in vectors.items()}
        parsed_vectors: VectorParams | dict[str, VectorParams] = named
    elif isinstance(vectors, qm.VectorParams):
        parsed_vectors = _plain_params(vectors)
    else:
        parsed_vectors = VectorParams(size=0)
    sparse_raw = params.sparse_vectors
    sparse: dict[str, SparseVectorParams] | None = None
    if isinstance(sparse_raw, dict):
        sparse = {}
        for key, val in sparse_raw.items():
            modifier = val.modifier
            text: str | None
            if isinstance(modifier, str) or modifier is None:
                text = modifier
            else:
                raw = modifier.value if isinstance(modifier, Modifier) else str(modifier)
                text = raw if isinstance(raw, str) else None
            sparse[str(key)] = SparseVectorParams(modifier=text)
    schema: dict[str, PayloadIndexInfo] = {}
    for key, val in info.payload_schema.items():
        raw_type = val.data_type
        if isinstance(raw_type, enum.Enum):
            enum_value = raw_type.value
            data_type = enum_value if isinstance(enum_value, str) else "keyword"
        elif isinstance(raw_type, str):
            data_type = raw_type
        else:
            data_type = "keyword"
        schema[str(key)] = PayloadIndexInfo(data_type=data_type)
    on_disk = params.on_disk_payload
    return CollectionInfo(
        config=CollectionConfig(
            params=CollectionParams(
                vectors=parsed_vectors,
                sparse_vectors=sparse,
                on_disk_payload=bool(on_disk) if isinstance(on_disk, bool) else False,
            )
        ),
        payload_schema=schema,
    )


class LocalQdrant:
    """包一层官方客户端，入参出参都换成上面的值对象。"""

    def __init__(self, path: Path) -> None:
        from qdrant_client import AsyncQdrantClient

        self._inner = AsyncQdrantClient(path=str(path))
        self._client: object | None = self._inner._client

    def local_collection_map(self) -> dict[str, object] | None:
        from qdrant_client.local.async_qdrant_local import AsyncQdrantLocal

        inner = self._client
        if not isinstance(inner, AsyncQdrantLocal):
            return None
        found: dict[str, object] = {}
        for key, value in inner.collections.items():
            found[str(key)] = value
        return found

    def release_embedded_cache(self) -> None:
        from qdrant_client.local.async_qdrant_local import AsyncQdrantLocal

        inner = self._client
        if not isinstance(inner, AsyncQdrantLocal):
            return
        inner.collections.clear()
        inner.aliases.clear()

    async def _run(self, coro: Awaitable[_T]) -> _T:
        try:
            return await coro
        except Exception as e:
            if type(e).__name__ == "UnexpectedResponse":
                raise UnexpectedResponse(str(e)) from e
            if type(e).__name__ == "ResponseHandlingException":
                raise ResponseHandlingException(str(e)) from e
            raise

    async def close(self) -> None:
        await self._inner.close()

    async def collection_exists(self, collection_name: str) -> bool:
        result = await self._run(self._inner.collection_exists(collection_name))
        return bool(result)

    async def get_collections(self) -> CollectionsResponse:
        raw = await self._run(self._inner.get_collections())
        return CollectionsResponse(collections=[CollectionDescription(name=item.name) for item in raw.collections])

    async def get_collection(self, collection_name: str) -> CollectionInfo:
        raw = await self._run(self._inner.get_collection(collection_name=collection_name))
        return _plain_collection(raw)

    async def create_collection(
        self,
        *,
        collection_name: str,
        vectors_config: VectorParams | dict[str, VectorParams],
        sparse_vectors_config: dict[str, SparseVectorParams] | None = None,
        on_disk_payload: bool = True,
    ) -> None:
        await self._run(
            self._inner.create_collection(
                collection_name=collection_name,
                vectors_config=_vectors(vectors_config),
                sparse_vectors_config=_sparse_map(sparse_vectors_config),
                on_disk_payload=on_disk_payload,
            )
        )

    async def delete_collection(self, *, collection_name: str) -> None:
        await self._run(self._inner.delete_collection(collection_name=collection_name))

    async def update_collection(self, *, collection_name: str, vectors_config: dict[str, VectorParamsDiff]) -> None:
        await self._run(
            self._inner.update_collection(
                collection_name=collection_name,
                vectors_config=_diff_map(vectors_config),
            )
        )

    async def recreate_collection(
        self,
        *,
        collection_name: str,
        vectors_config: VectorParams | dict[str, VectorParams],
        sparse_vectors_config: dict[str, SparseVectorParams] | None = None,
        on_disk_payload: bool = True,
    ) -> None:
        await self._run(
            self._inner.recreate_collection(
                collection_name=collection_name,
                vectors_config=_vectors(vectors_config),
                sparse_vectors_config=_sparse_map(sparse_vectors_config),
                on_disk_payload=on_disk_payload,
            )
        )

    async def create_payload_index(
        self,
        *,
        collection_name: str,
        field_name: str,
        field_schema: PayloadSchemaType | str,
    ) -> None:
        await self._run(
            self._inner.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=_schema(field_schema),
            )
        )

    async def upsert(
        self,
        *,
        collection_name: str,
        points: Sequence[PointStruct],
        wait: bool = True,
    ) -> None:
        await self._run(
            self._inner.upsert(
                collection_name=collection_name,
                points=_points(points),
                wait=wait,
            )
        )

    async def delete(
        self,
        *,
        collection_name: str,
        points_selector: PointIdsList | Filter | Sequence[PointId],
        wait: bool = True,
    ) -> None:
        selector = _selector(points_selector)
        if isinstance(selector, list):
            ids: list[str | int] = []
            for item in selector:
                if isinstance(item, int) and not isinstance(item, bool):
                    ids.append(item)
                else:
                    ids.append(str(item))
            # stub 的 ExtendedPointId 不接受已经收窄的 str | int 列表。
            await self._run(
                self._inner.delete(
                    collection_name=collection_name,
                    points_selector=qm.PointIdsList(points=ids),  # type: ignore[arg-type]
                    wait=wait,
                )
            )
            return
        await self._run(
            self._inner.delete(
                collection_name=collection_name,
                points_selector=selector,
                wait=wait,
            )
        )

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
    ) -> QueryResponse:
        raw = await self._run(
            self._inner.query_points(
                collection_name=collection_name,
                query=_query(query),
                using=using,
                query_filter=_filter(query_filter),
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True if with_payload is None else with_payload,
                with_vectors=False if with_vectors is None else with_vectors,
                prefetch=None if prefetch is None else [_prefetch(item) for item in prefetch],
            )
        )
        return QueryResponse(points=[_plain_point(item) for item in raw.points])

    async def scroll(
        self,
        *,
        collection_name: str,
        limit: int = 10,
        offset: PointId | None = None,
        with_payload: bool | Sequence[str] = True,
        with_vectors: bool | Sequence[str] = False,
        scroll_filter: Filter | None = None,
    ) -> tuple[list[ScoredPoint], PointId | None]:
        raw = await self._run(
            self._inner.scroll(
                collection_name=collection_name,
                limit=limit,
                offset=offset,
                with_payload=_with_names(with_payload),
                with_vectors=_with_names(with_vectors),
                scroll_filter=_filter(scroll_filter),
            )
        )
        records, next_offset = raw
        parsed_offset: PointId | None
        if next_offset is None:
            parsed_offset = None
        elif isinstance(next_offset, int) and not isinstance(next_offset, bool):
            parsed_offset = next_offset
        else:
            parsed_offset = str(next_offset)
        return [_plain_point(item) for item in records], parsed_offset

    async def count(
        self,
        *,
        collection_name: str,
        count_filter: Filter | None = None,
        exact: bool = True,
    ) -> CountResult:
        raw = await self._run(
            self._inner.count(
                collection_name=collection_name,
                count_filter=_filter(count_filter),
                exact=exact,
            )
        )
        return CountResult(count=int(raw.count))

    async def retrieve(
        self,
        *,
        collection_name: str,
        ids: Sequence[PointId],
        with_payload: bool = True,
        with_vectors: bool | list[str] = False,
    ) -> list[ScoredPoint]:
        raw = await self._run(
            self._inner.retrieve(
                collection_name=collection_name,
                ids=list(ids),
                with_payload=_with_names(with_payload),
                with_vectors=_with_names(with_vectors),
            )
        )
        return [_plain_point(item) for item in raw]

    async def set_payload(
        self,
        *,
        collection_name: str,
        payload: Mapping[str, object],
        points: Sequence[PointId],
        wait: bool = True,
    ) -> None:
        await self._run(
            self._inner.set_payload(
                collection_name=collection_name,
                payload=dict(payload),
                points=list(points),
                wait=wait,
            )
        )
