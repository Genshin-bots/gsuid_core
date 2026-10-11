"""远程 Qdrant 的 HTTP 客户端。只依赖 httpx，不导入 qdrant-client。"""

from __future__ import annotations

import enum
import json
from typing import Mapping, Protocol, Sequence, runtime_checkable

import httpx

from gsuid_core.ai_core.rag.qmodels import (
    Range,
    Filter,
    Fusion,
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


class UnexpectedResponse(Exception):
    """Qdrant 返回了 4xx/5xx。消息里保留响应正文，供维度/索引错误识别。"""


class ResponseHandlingException(Exception):
    """Qdrant 响应体不是 JSON。"""


@runtime_checkable
class _SizedVector(Protocol):
    size: int
    distance: object
    on_disk: bool | None


def _distance_text(distance: object) -> str:
    if isinstance(distance, Distance):
        return distance.value
    if isinstance(distance, enum.Enum):
        raw = distance.value
        if isinstance(raw, str):
            return raw
    if isinstance(distance, str):
        return distance
    return "Cosine"


def _modifier_text(modifier: object) -> str | None:
    if modifier is None:
        return None
    if isinstance(modifier, Modifier):
        return modifier.value
    if isinstance(modifier, enum.Enum):
        raw = modifier.value
        if isinstance(raw, str):
            return raw
    if isinstance(modifier, str):
        return modifier
    return None


def _schema_text(field_schema: object) -> str:
    if isinstance(field_schema, PayloadSchemaType):
        return field_schema.value
    if isinstance(field_schema, enum.Enum):
        raw = field_schema.value
        if isinstance(raw, str):
            return raw
    if isinstance(field_schema, str):
        return field_schema
    return str(field_schema)


def _point_id(value: object) -> PointId:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    return str(value)


def _vector_rest(vector: object) -> object:
    if isinstance(vector, SparseVector):
        return {"indices": list(vector.indices), "values": list(vector.values)}
    if isinstance(vector, list):
        return [float(item) for item in vector]
    if isinstance(vector, dict):
        return {str(key): _vector_rest(val) for key, val in vector.items()}
    return vector


def _match_rest(match: MatchValue | MatchAny) -> dict[str, object]:
    if isinstance(match, MatchValue):
        return {"value": match.value}
    return {"any": list(match.any)}


def _range_rest(bounds: Range) -> dict[str, float]:
    body: dict[str, float] = {}
    if bounds.lt is not None:
        body["lt"] = bounds.lt
    if bounds.gt is not None:
        body["gt"] = bounds.gt
    if bounds.lte is not None:
        body["lte"] = bounds.lte
    if bounds.gte is not None:
        body["gte"] = bounds.gte
    return body


def _condition_rest(condition: FieldCondition | HasIdCondition) -> dict[str, object]:
    if isinstance(condition, HasIdCondition):
        return {"has_id": [_point_id(item) for item in condition.has_id]}
    body: dict[str, object] = {"key": condition.key}
    if condition.match is not None:
        body["match"] = _match_rest(condition.match)
    if condition.range is not None:
        body["range"] = _range_rest(condition.range)
    return body


def _filter_rest(query_filter: Filter) -> dict[str, object]:
    body: dict[str, object] = {}
    if query_filter.must:
        body["must"] = [_condition_rest(item) for item in query_filter.must]
    if query_filter.should:
        body["should"] = [_condition_rest(item) for item in query_filter.should]
    if query_filter.must_not:
        body["must_not"] = [_condition_rest(item) for item in query_filter.must_not]
    return body


def _one_vector_rest(params: object) -> dict[str, object]:
    if isinstance(params, VectorParams):
        body: dict[str, object] = {"size": params.size, "distance": _distance_text(params.distance)}
        if params.on_disk is not None:
            body["on_disk"] = params.on_disk
        return body
    if isinstance(params, dict):
        nested: dict[str, object] = {"size": int(params["size"]), "distance": _distance_text(params["distance"])}
        if "on_disk" in params and params["on_disk"] is not None:
            nested["on_disk"] = bool(params["on_disk"])
        return nested
    if isinstance(params, _SizedVector):
        body = {"size": int(params.size), "distance": _distance_text(params.distance)}
        if params.on_disk is not None:
            body["on_disk"] = bool(params.on_disk)
        return body
    return {}


def _vectors_rest(vectors_config: object) -> object:
    if isinstance(vectors_config, dict):
        if "size" in vectors_config:
            return _one_vector_rest(vectors_config)
        return {str(key): _one_vector_rest(val) for key, val in vectors_config.items()}
    return _one_vector_rest(vectors_config)


def _sparse_rest(sparse_config: object) -> dict[str, object] | None:
    if sparse_config is None:
        return None
    if not isinstance(sparse_config, dict):
        return None
    body: dict[str, object] = {}
    for key, val in sparse_config.items():
        modifier = val.modifier if isinstance(val, SparseVectorParams) else None
        if modifier is None and isinstance(val, dict) and "modifier" in val:
            modifier = val["modifier"]
        text = _modifier_text(modifier)
        entry: dict[str, object] = {}
        if text is not None:
            entry["modifier"] = text
        body[str(key)] = entry
    return body


def _diff_rest(diff: object) -> dict[str, object]:
    if isinstance(diff, VectorParamsDiff):
        if diff.on_disk is None:
            return {}
        return {"on_disk": diff.on_disk}
    if isinstance(diff, dict) and "on_disk" in diff and diff["on_disk"] is not None:
        return {"on_disk": bool(diff["on_disk"])}
    return {}


def _query_rest(query: list[float] | SparseVector | FusionQuery) -> object:
    if isinstance(query, FusionQuery):
        fusion = query.fusion.value if isinstance(query.fusion, Fusion) else str(query.fusion)
        return {"fusion": fusion}
    if isinstance(query, SparseVector):
        return _vector_rest(query)
    return [float(item) for item in query]


def _prefetch_rest(item: Prefetch) -> dict[str, object]:
    body: dict[str, object] = {"query": _query_rest(item.query), "limit": item.limit}
    if item.using is not None:
        body["using"] = item.using
    if item.filter is not None:
        body["filter"] = _filter_rest(item.filter)
    if item.score_threshold is not None:
        body["score_threshold"] = item.score_threshold
    return body


def _parse_point(raw: object) -> ScoredPoint:
    if not isinstance(raw, dict):
        return ScoredPoint(id="", version=0, score=0.0)
    point_id = raw["id"] if "id" in raw else ""
    version = raw["version"] if "version" in raw else 0
    score = raw["score"] if "score" in raw else 0.0
    payload = raw["payload"] if "payload" in raw else None
    vector = raw["vector"] if "vector" in raw else None
    parsed_payload = payload if isinstance(payload, dict) else None
    parsed_vector = _parse_vector(vector)
    return ScoredPoint(
        id=_point_id(point_id),
        version=int(version) if isinstance(version, int) else 0,
        score=float(score) if isinstance(score, (int, float)) else 0.0,
        payload=parsed_payload,
        vector=parsed_vector,
    )


def _parse_vector(vector: object) -> list[float] | SparseVector | dict[str, list[float] | SparseVector] | None:
    if isinstance(vector, list):
        return [float(item) for item in vector]
    if isinstance(vector, dict) and "indices" in vector and "values" in vector:
        indices = vector["indices"]
        values = vector["values"]
        if isinstance(indices, list) and isinstance(values, list):
            return SparseVector(
                indices=[int(item) for item in indices],
                values=[float(item) for item in values],
            )
    if isinstance(vector, dict):
        named: dict[str, list[float] | SparseVector] = {}
        for key, val in vector.items():
            parsed = _parse_vector(val)
            if isinstance(parsed, list) or isinstance(parsed, SparseVector):
                named[str(key)] = parsed
        return named
    return None


def _parse_vectors(raw: object) -> VectorParams | dict[str, VectorParams]:
    if isinstance(raw, dict) and "size" in raw and isinstance(raw["size"], int):
        on_disk = raw["on_disk"] if "on_disk" in raw and isinstance(raw["on_disk"], bool) else None
        distance = raw["distance"] if "distance" in raw and isinstance(raw["distance"], str) else "Cosine"
        return VectorParams(size=raw["size"], distance=distance, on_disk=on_disk)
    named: dict[str, VectorParams] = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            parsed = _parse_vectors(val)
            if isinstance(parsed, VectorParams):
                named[str(key)] = parsed
    return named


def _parse_sparse(raw: object) -> dict[str, SparseVectorParams] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, SparseVectorParams] = {}
    for key, val in raw.items():
        modifier = None
        if isinstance(val, dict) and "modifier" in val and isinstance(val["modifier"], str):
            modifier = val["modifier"]
        out[str(key)] = SparseVectorParams(modifier=modifier)
    return out


def _parse_collection(raw: object) -> CollectionInfo:
    if not isinstance(raw, dict):
        empty = CollectionParams(vectors=VectorParams(size=0), sparse_vectors=None, on_disk_payload=False)
        return CollectionInfo(config=CollectionConfig(params=empty), payload_schema={})
    config = raw["config"] if "config" in raw and isinstance(raw["config"], dict) else {}
    params = config["params"] if "params" in config and isinstance(config["params"], dict) else {}
    vectors = _parse_vectors(params["vectors"] if "vectors" in params else {})
    sparse = _parse_sparse(params["sparse_vectors"] if "sparse_vectors" in params else None)
    on_disk_payload = params["on_disk_payload"] if "on_disk_payload" in params else False
    schema_raw = raw["payload_schema"] if "payload_schema" in raw and isinstance(raw["payload_schema"], dict) else {}
    schema: dict[str, PayloadIndexInfo] = {}
    for key, val in schema_raw.items():
        data_type = "keyword"
        if isinstance(val, dict) and "data_type" in val and isinstance(val["data_type"], str):
            data_type = val["data_type"]
        schema[str(key)] = PayloadIndexInfo(data_type=data_type)
    return CollectionInfo(
        config=CollectionConfig(
            params=CollectionParams(
                vectors=vectors,
                sparse_vectors=sparse,
                on_disk_payload=bool(on_disk_payload),
            )
        ),
        payload_schema=schema,
    )


class HttpQdrant:
    """Qdrant REST。连接在第一次请求时打开，避免在没有事件循环的线程里建客户端。"""

    def __init__(
        self,
        url: str,
        api_key: str | None,
        *,
        timeout: float = 30,
        trust_env: bool = True,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._trust_env = trust_env
        self._http = http
        self._client: object | None = None

    def local_collection_map(self) -> dict[str, object] | None:
        return None

    def release_embedded_cache(self) -> None:
        return None

    async def _session(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        headers: dict[str, str] = {}
        if self._api_key:
            headers["api-key"] = self._api_key
        self._http = httpx.AsyncClient(
            base_url=self._url,
            timeout=self._timeout,
            headers=headers,
            trust_env=self._trust_env,
        )
        return self._http

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
    ) -> object:
        session = await self._session()
        response = await session.request(method, path, json=body, params=params)
        if response.status_code >= 400:
            raise UnexpectedResponse(response.text)
        if response.status_code == 204 or response.content == b"":
            return {}
        try:
            parsed = json.loads(response.text)
        except json.JSONDecodeError as e:
            raise ResponseHandlingException(response.text) from e
        if isinstance(parsed, dict) and "result" in parsed:
            return parsed["result"]
        return parsed

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def collection_exists(self, collection_name: str) -> bool:
        result = await self._request("GET", f"/collections/{collection_name}/exists")
        if isinstance(result, dict) and "exists" in result:
            return bool(result["exists"])
        return False

    async def get_collections(self) -> CollectionsResponse:
        result = await self._request("GET", "/collections")
        names: list[CollectionDescription] = []
        if isinstance(result, dict) and "collections" in result and isinstance(result["collections"], list):
            for item in result["collections"]:
                if isinstance(item, dict) and "name" in item and isinstance(item["name"], str):
                    names.append(CollectionDescription(name=item["name"]))
        return CollectionsResponse(collections=names)

    async def get_collection(self, collection_name: str) -> CollectionInfo:
        result = await self._request("GET", f"/collections/{collection_name}")
        return _parse_collection(result)

    async def create_collection(
        self,
        *,
        collection_name: str,
        vectors_config: VectorParams | dict[str, VectorParams],
        sparse_vectors_config: dict[str, SparseVectorParams] | None = None,
        on_disk_payload: bool = True,
    ) -> None:
        body: dict[str, object] = {
            "vectors": _vectors_rest(vectors_config),
            "on_disk_payload": on_disk_payload,
        }
        sparse = _sparse_rest(sparse_vectors_config)
        if sparse is not None:
            body["sparse_vectors"] = sparse
        await self._request("PUT", f"/collections/{collection_name}", body=body)

    async def delete_collection(self, *, collection_name: str) -> None:
        await self._request("DELETE", f"/collections/{collection_name}")

    async def update_collection(self, *, collection_name: str, vectors_config: dict[str, VectorParamsDiff]) -> None:
        vectors: dict[str, object] = {}
        if isinstance(vectors_config, dict):
            vectors = {str(key): _diff_rest(val) for key, val in vectors_config.items()}
        await self._request("PATCH", f"/collections/{collection_name}", body={"vectors": vectors})

    async def recreate_collection(
        self,
        *,
        collection_name: str,
        vectors_config: VectorParams | dict[str, VectorParams],
        sparse_vectors_config: dict[str, SparseVectorParams] | None = None,
        on_disk_payload: bool = True,
    ) -> None:
        if await self.collection_exists(collection_name):
            await self.delete_collection(collection_name=collection_name)
        await self.create_collection(
            collection_name=collection_name,
            vectors_config=vectors_config,
            sparse_vectors_config=sparse_vectors_config,
            on_disk_payload=on_disk_payload,
        )

    async def create_payload_index(
        self,
        *,
        collection_name: str,
        field_name: str,
        field_schema: PayloadSchemaType | str,
    ) -> None:
        await self._request(
            "PUT",
            f"/collections/{collection_name}/index",
            body={"field_name": field_name, "field_schema": _schema_text(field_schema)},
        )

    async def upsert(
        self,
        *,
        collection_name: str,
        points: Sequence[PointStruct],
        wait: bool = True,
    ) -> None:
        encoded: list[dict[str, object]] = []
        for point in points:
            item: dict[str, object] = {"id": _point_id(point.id), "vector": _vector_rest(point.vector)}
            if point.payload is not None:
                item["payload"] = dict(point.payload)
            encoded.append(item)
        await self._request(
            "PUT",
            f"/collections/{collection_name}/points",
            body={"points": encoded},
            params={"wait": "true" if wait else "false"},
        )

    async def delete(
        self,
        *,
        collection_name: str,
        points_selector: PointIdsList | Filter | Sequence[PointId],
        wait: bool = True,
    ) -> None:
        if isinstance(points_selector, Filter):
            body: dict[str, object] = {"filter": _filter_rest(points_selector)}
        elif isinstance(points_selector, PointIdsList):
            body = {"points": [_point_id(item) for item in points_selector.points]}
        else:
            body = {"points": [_point_id(item) for item in points_selector]}
        await self._request(
            "POST",
            f"/collections/{collection_name}/points/delete",
            body=body,
            params={"wait": "true" if wait else "false"},
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
        body: dict[str, object] = {"query": _query_rest(query), "limit": limit}
        if using is not None:
            body["using"] = using
        if query_filter is not None:
            body["filter"] = _filter_rest(query_filter)
        if score_threshold is not None:
            body["score_threshold"] = score_threshold
        if with_payload is not None:
            body["with_payload"] = with_payload
        if with_vectors is not None:
            body["with_vector"] = with_vectors
        if prefetch is not None:
            body["prefetch"] = [_prefetch_rest(item) for item in prefetch]
        result = await self._request("POST", f"/collections/{collection_name}/points/query", body=body)
        points: list[ScoredPoint] = []
        if isinstance(result, dict) and "points" in result and isinstance(result["points"], list):
            points = [_parse_point(item) for item in result["points"]]
        return QueryResponse(points=points)

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
        body: dict[str, object] = {
            "limit": limit,
            "with_payload": with_payload,
            "with_vector": with_vectors,
        }
        if offset is not None:
            body["offset"] = _point_id(offset)
        if scroll_filter is not None:
            body["filter"] = _filter_rest(scroll_filter)
        result = await self._request("POST", f"/collections/{collection_name}/points/scroll", body=body)
        points: list[ScoredPoint] = []
        next_offset: PointId | None = None
        if isinstance(result, dict):
            if "points" in result and isinstance(result["points"], list):
                points = [_parse_point(item) for item in result["points"]]
            if "next_page_offset" in result and result["next_page_offset"] is not None:
                next_offset = _point_id(result["next_page_offset"])
        return points, next_offset

    async def count(
        self,
        *,
        collection_name: str,
        count_filter: Filter | None = None,
        exact: bool = True,
    ) -> CountResult:
        body: dict[str, object] = {"exact": exact}
        if count_filter is not None:
            body["filter"] = _filter_rest(count_filter)
        result = await self._request("POST", f"/collections/{collection_name}/points/count", body=body)
        count = 0
        if isinstance(result, dict) and "count" in result and isinstance(result["count"], int):
            count = result["count"]
        return CountResult(count=count)

    async def retrieve(
        self,
        *,
        collection_name: str,
        ids: Sequence[PointId],
        with_payload: bool = True,
        with_vectors: bool | list[str] = False,
    ) -> list[ScoredPoint]:
        result = await self._request(
            "POST",
            f"/collections/{collection_name}/points",
            body={
                "ids": [_point_id(item) for item in ids],
                "with_payload": with_payload,
                "with_vector": with_vectors,
            },
        )
        if isinstance(result, list):
            return [_parse_point(item) for item in result]
        return []

    async def set_payload(
        self,
        *,
        collection_name: str,
        payload: Mapping[str, object],
        points: Sequence[PointId],
        wait: bool = True,
    ) -> None:
        await self._request(
            "POST",
            f"/collections/{collection_name}/points/payload",
            body={"payload": dict(payload), "points": [_point_id(item) for item in points]},
            params={"wait": "true" if wait else "false"},
        )
