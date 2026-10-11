"""远程 Qdrant 走 HTTP。导入 RAG 不再把 qdrant-client 装进进程。"""

from __future__ import annotations

import sys
import json
import asyncio
import textwrap
import subprocess

import httpx

from gsuid_core.ai_core.rag.qhttp import HttpQdrant
from gsuid_core.ai_core.rag.qmodels import (
    Filter,
    Fusion,
    Prefetch,
    MatchValue,
    FusionQuery,
    SparseVector,
    FieldCondition,
)


def test_query_points_encodes_filter_and_reads_hits() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        payload = {
            "result": {
                "points": [
                    {
                        "id": "a",
                        "version": 3,
                        "score": 0.42,
                        "payload": {"title": "面板"},
                    }
                ]
            },
            "status": "ok",
        }
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)

    async def run() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://qdrant") as http:
            client = HttpQdrant("http://qdrant", None, http=http)
            result = await client.query_points(
                collection_name="tools",
                query=[0.1, 0.2],
                query_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value="plugin"))]),
                limit=3,
                with_payload=True,
                prefetch=[
                    Prefetch(query=SparseVector(indices=[1], values=[0.5]), using="sparse", limit=4),
                ],
                using="dense",
            )
            point = result.points[0]
            assert point.id == "a"
            assert point.score == 0.42
            assert point.payload is not None
            assert point.payload["title"] == "面板"

    asyncio.run(run())
    assert seen["path"] == "/collections/tools/points/query"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["limit"] == 3
    assert body["using"] == "dense"
    assert body["query"] == [0.1, 0.2]
    query_filter = body["filter"]
    assert isinstance(query_filter, dict)
    must = query_filter["must"]
    assert isinstance(must, list)
    assert must[0] == {"key": "source", "match": {"value": "plugin"}}
    prefetch = body["prefetch"]
    assert isinstance(prefetch, list)
    assert prefetch[0]["using"] == "sparse"
    assert prefetch[0]["query"] == {"indices": [1], "values": [0.5]}


def test_fusion_query_uses_rrf_name() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"result": {"points": []}, "status": "ok"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://qdrant") as http:
            client = HttpQdrant("http://qdrant", None, http=http)
            await client.query_points(
                collection_name="memory",
                query=FusionQuery(fusion=Fusion.RRF),
                limit=2,
            )

    asyncio.run(run())
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["query"] == {"fusion": "rrf"}


def test_http_error_keeps_response_text() -> None:
    from gsuid_core.ai_core.rag.qhttp import UnexpectedResponse

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Wrong input: Vector dimension error: expected dim: 4")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://qdrant") as http:
            client = HttpQdrant("http://qdrant", None, http=http)
            await client.collection_exists("tools")

    try:
        asyncio.run(run())
    except UnexpectedResponse as exc:
        assert "dimension error" in str(exc).lower()
        return
    raise AssertionError("expected UnexpectedResponse")


def test_rag_import_does_not_load_qdrant_client() -> None:
    """单独导入 qdrant-client 约 +100MB。RAG 包导入后进程里不该有它，工作集也应更低。"""
    script = textwrap.dedent(
        """
        import gc
        import logging
        import sys
        import psutil
        logging.disable(logging.CRITICAL)
        load_pkg = {load!r}
        if load_pkg:
            import qdrant_client
        import gsuid_core.ai_core.rag
        import gsuid_core.ai_core.buildin_tools.rag_search
        gc.collect()
        print("RSS", psutil.Process().memory_info().rss / 1048576)
        print("LOADED", "qdrant_client" in sys.modules)
        """
    )

    def measure(load_pkg: bool) -> tuple[float, bool]:
        completed = subprocess.run(
            [sys.executable, "-c", script.format(load=load_pkg)],
            check=True,
            capture_output=True,
            text=True,
        )
        rss_line = [line for line in completed.stdout.splitlines() if line.startswith("RSS ")][-1]
        loaded_line = [line for line in completed.stdout.splitlines() if line.startswith("LOADED ")][-1]
        return float(rss_line.split()[1]), loaded_line.split()[1] == "True"

    without_pkg, loaded_without = measure(False)
    with_pkg, loaded_with = measure(True)
    assert loaded_without is False
    assert loaded_with is True
    assert without_pkg + 10 < with_pkg
