"""嵌入配置保存：下拉传来的数字字符串不能静默丢掉，整数写入要落盘。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Protocol
from pathlib import Path
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gsuid_core.webconsole import embedding_config_api as embedding_api
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_admin, require_admin_header
from gsuid_core.ai_core.configs.ai_config import LOCAL_EMBEDDING_CONFIG, OPENAI_EMBEDDING_CONFIG
from gsuid_core.utils.plugins_config.gs_config import StringConfig, all_config_list

_LOCAL_NAME = "gs-test-local-embedding-save"
_OPENAI_NAME = "gs-test-openai-embedding-save"


def _admin() -> dict[str, dict[str, str]]:
    return {"user": {"role": "admin"}}


def _as_object_dict(loaded: object) -> dict[str, object]:
    assert isinstance(loaded, dict)
    out: dict[str, object] = {}
    for key, value in loaded.items():
        assert isinstance(key, str)
        out[key] = value
    return out


def _field_data(doc: dict[str, object], key: str) -> object:
    item = doc[key]
    assert isinstance(item, dict)
    assert "data" in item
    return item["data"]


def _load(path: Path) -> dict[str, object]:
    return _as_object_dict(json.loads(path.read_text(encoding="utf-8")))


class _JsonResponse(Protocol):
    status_code: int

    def json(self) -> object: ...


def _body(resp: _JsonResponse) -> dict[str, object]:
    assert resp.status_code == 200
    return _as_object_dict(resp.json())


@pytest.fixture
def embedding_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path, Path]]:
    all_config_list.pop(_LOCAL_NAME, None)
    all_config_list.pop(_OPENAI_NAME, None)
    local_path = tmp_path / "local.json"
    openai_path = tmp_path / "openai.json"
    local = StringConfig(_LOCAL_NAME, local_path, deepcopy(LOCAL_EMBEDDING_CONFIG))
    openai = StringConfig(_OPENAI_NAME, openai_path, deepcopy(OPENAI_EMBEDDING_CONFIG))
    monkeypatch.setattr(embedding_api, "local_embedding_config", local)
    monkeypatch.setattr(embedding_api, "openai_embedding_config", openai)
    app.dependency_overrides[require_admin] = _admin
    app.dependency_overrides[require_admin_header] = _admin
    client = TestClient(app)
    try:
        yield client, local_path, openai_path
    finally:
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides.pop(require_admin_header, None)
        all_config_list.pop(_LOCAL_NAME, None)
        all_config_list.pop(_OPENAI_NAME, None)


def test_summary_marks_embed_ints(embedding_client: tuple[TestClient, Path, Path]) -> None:
    client, _local_path, _openai_path = embedding_client
    body = _body(client.get("/api/embedding_config/summary"))
    assert body["status"] == 0
    data = body["data"]
    assert isinstance(data, dict)
    local = data["local_config"]
    openai = data["openai_config"]
    assert isinstance(local, dict)
    assert isinstance(openai, dict)
    workers = local["embed_batch_workers"]
    threads = local["embed_threads"]
    batch = local["embed_batch_size"]
    name = local["embedding_model_name"]
    dimension = openai["dimension"]
    assert isinstance(workers, dict)
    assert isinstance(threads, dict)
    assert isinstance(batch, dict)
    assert isinstance(name, dict)
    assert isinstance(dimension, dict)
    assert workers["type"] == "gsint"
    assert threads["type"] == "gsint"
    assert batch["type"] == "gsint"
    assert name["type"] == "gsstr"
    assert dimension["type"] == "gsint"
    assert workers["data"] == 1
    assert threads["data"] == 1
    assert batch["data"] == 16
    assert dimension["data"] == 0


def test_numeric_strings_do_not_persist(embedding_client: tuple[TestClient, Path, Path]) -> None:
    """issue #298：下拉把 2 / 64 收成字符串，磁盘必须保持 1 / 1 / 16，且接口报失败。"""
    client, local_path, openai_path = embedding_client
    body = _body(
        client.post(
            "/api/embedding_config/local",
            json={
                "embedding_model_name": "jinaai/jina-embeddings-v2-base-zh",
                "embed_batch_workers": "2",
                "embed_threads": "2",
                "embed_batch_size": "64",
            },
        )
    )
    assert body["status"] == 1
    msg = body["msg"]
    assert isinstance(msg, str)
    assert "embed_batch_workers" in msg
    assert "embed_threads" in msg
    assert "embed_batch_size" in msg
    disk = _load(local_path)
    assert _field_data(disk, "embed_batch_workers") == 1
    assert _field_data(disk, "embed_threads") == 1
    assert _field_data(disk, "embed_batch_size") == 16
    # 同一次请求里类型正确的字符串项仍然写入
    assert _field_data(disk, "embedding_model_name") == "jinaai/jina-embeddings-v2-base-zh"

    dimension = _body(client.post("/api/embedding_config/openai", json={"dimension": "768"}))
    assert dimension["status"] == 1
    assert _field_data(_load(openai_path), "dimension") == 0


def test_int_payload_persists(embedding_client: tuple[TestClient, Path, Path]) -> None:
    client, local_path, openai_path = embedding_client
    body = _body(
        client.post(
            "/api/embedding_config/local",
            json={
                "embed_batch_workers": 2,
                "embed_threads": 4,
                "embed_batch_size": 64,
            },
        )
    )
    assert body["status"] == 0
    msg = body["msg"]
    assert isinstance(msg, str)
    assert "重启后生效" in msg
    disk = _load(local_path)
    assert _field_data(disk, "embed_batch_workers") == 2
    assert _field_data(disk, "embed_threads") == 4
    assert _field_data(disk, "embed_batch_size") == 64

    dimension = _body(client.post("/api/embedding_config/openai", json={"dimension": 768}))
    assert dimension["status"] == 0
    assert _field_data(_load(openai_path), "dimension") == 768
