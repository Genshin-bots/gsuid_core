"""MCP 运行时与 RAG 文本切分契约测试。"""

from __future__ import annotations

import sys
import json
import time
import types
import asyncio
import inspect
from typing import List
from unittest.mock import patch

import pytest
from fastmcp import FastMCP
from starlette.applications import Starlette
from fastmcp.client.transports import StdioTransport

from gsuid_core.ai_core.mcp import server as mcp_server
from gsuid_core.ai_core.rag.base import calculate_hash
from gsuid_core.ai_core.rag.chunking import (
    CHUNKER_ID,
    EMBED_CHAR_BUDGET,
    chunk_body,
    split_text,
    chunker_stamp,
    document_bodies,
    pieces_for_embed,
    chunk_method_hash,
    embed_char_budget,
    logical_chunk_ids,
    should_skip_rebuild,
)


def test_rag_chunking_budget_and_lexical_rrf() -> None:
    from gsuid_core.ai_core.rag.tool_lexical import has_cjk, rrf_fuse, ensure_index, lexical_scores

    decimal = chunk_body("版本是 3.14。下一句很短。", room=12)
    assert len(decimal) == 2 and "3.14" in decimal[0]
    headed = chunk_body("## 小节\n" + ("甲" * 300), room=80, overlap=10)
    assert len(headed) > 1 and all(piece.startswith("## 小节") and len(piece) <= 80 for piece in headed)
    sentence = "一二三四五六七八九十。"
    assert chunk_body(sentence * 2, room=16, overlap=10) == [sentence, sentence]
    fence = "```\nKEEP\n```"
    assert fence in chunk_body(("甲" * 40) + "\n\n" + fence + "\n\n" + ("乙" * 40), room=50)
    assert chunk_body("   ", room=40) == [] and split_text("") == []

    pieces = pieces_for_embed("甲" * 2000, title="标" * 40, tags=["标签"], max_tokens=512)
    assert len(pieces) > 1 and all(len(p.embed_text) <= EMBED_CHAR_BUDGET for p in pieces)
    huge_title = pieces_for_embed("正文", title="题" * 2000, max_tokens=512)
    assert huge_title and all(len(p.embed_text) <= EMBED_CHAR_BUDGET for p in huge_title)
    bodies = document_bodies(
        full_text="", sections=["甲" * 2000], title="题", tags=["a"], budget=4000, overlap=60, max_tokens=512
    )
    assert len(bodies) > 1 and all(0 < len(b) <= EMBED_CHAR_BUDGET for b in bodies)

    assert embed_char_budget(512) == 480 and embed_char_budget(8192) == 8160
    assert len(pieces_for_embed("甲" * 2000, max_tokens=8192)) == 1
    with patch("gsuid_core.ai_core.rag.base.embedding_provider", None):
        assert embed_char_budget() == 480 and chunker_stamp() == "embed-v1@512"

    stamp = chunk_method_hash("content-hash")
    assert stamp != calculate_hash({"_content": "content-hash"}) and CHUNKER_ID
    assert should_skip_rebuild(stamp, 3, "content-hash", 3) is True
    assert should_skip_rebuild(stamp, 2, "content-hash", 3) is False
    assert chunk_method_hash("base", 512) != chunk_method_hash("base", 1024)
    assert logical_chunk_ids("doc", 1) == ["doc"] and logical_chunk_ids("doc", 2) == ["doc#0", "doc#1"]

    h1 = calculate_hash({"a": 1, "b": "test"})
    assert h1 == calculate_hash({"b": "test", "a": 1}) and len(h1) == 32

    class _Tool:
        capability_domain: str | None

        def __init__(self, name: str, description: str, domain: str | None, covers: list[str], aliases: list[str]):
            self.name = name
            self.description = description
            self.capability_domain = domain
            self.covers = covers
            self.aliases = aliases

    tools = {
        "get_weather": _Tool(
            "get_weather", "查询城市实时天气与气温预报", "天气", ["北京上海等城市气温"], ["天气·气温查询"]
        ),
        "generate_image": _Tool("generate_image", "生成图片与海报", "绘图", ["文生图"], ["绘图·生成图片"]),
    }
    ensure_index(tools)
    assert has_cjk("帮我查明天北京的气温")
    scores = lexical_scores("帮我查明天北京的气温")
    assert scores["get_weather"] > 0 and rrf_fuse([], scores)[0] == "get_weather"


def test_mcp_config_node_profile_and_tool_quota(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.mcp.node_profile import mcp_node_id, build_mcp_node_base, compute_mcp_overlay
    from gsuid_core.ai_core.mcp.config_manager import (
        MCP_CONFIG_SCHEMA_VERSION,
        MCPConfig,
        MCPConfigManager,
        MCPToolDefinition,
        parse_market_meta,
        parse_mcp_json_server,
        parse_generated_profile,
        suggest_shared_env_keys,
        input_schema_to_parameters,
    )
    from gsuid_core.ai_core.buildin_tools.find_tools_rank import RankedHit
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import _apply_mcp_tool_quota

    stdio = parse_mcp_json_server("fs", {"command": "npx", "args": ["-y", "srv"], "env": {"A": "1"}})
    assert stdio is not None and stdio.get("transport") == "stdio" and stdio.get("env") == {"A": "1"}
    sse = parse_mcp_json_server("legacy", {"url": "https://x.example/sse"})
    http = parse_mcp_json_server("remote", {"url": "https://x.example/mcp"})
    assert sse is not None and sse.get("transport") == "sse"
    assert http is not None and http.get("transport") == "streamable_http"
    assert parse_mcp_json_server("bad", "not-a-dict") is None

    old = {
        "name": "luckin",
        "transport": "streamable_http",
        "url": "https://x.example/mcp",
        "env": {"LUCKIN_MCP_TOKEN": "tok"},
        "tools": [{"name": "t1"}],
    }
    (tmp_path / "luckin.json").write_text(json.dumps(old), encoding="utf-8")
    manager = MCPConfigManager()
    monkeypatch.setattr(manager, "_base_path", tmp_path)
    manager._load_all()
    data = json.loads((tmp_path / "luckin.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == MCP_CONFIG_SCHEMA_VERSION and data["register_as_ai_tools"] is True
    assert data["user_env_keys"] == ["LUCKIN_MCP_TOKEN"] and data["tools"][0]["exposure"] == "private"
    assert MCPToolDefinition.from_dict({"name": "t2", "exposure": "public", "mutating": True}).exposure == "public"

    shared = suggest_shared_env_keys({"MINIMAX_API_KEY": "k", "MINIMAX_API_HOST": "https://x", "LUCKIN_MCP_TOKEN": "t"})
    assert "MINIMAX_API_HOST" in shared and "MINIMAX_API_KEY" not in shared and "LUCKIN_MCP_TOKEN" not in shared

    params = input_schema_to_parameters(
        {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "查询词"},
                "limit": {"title": "上限", "type": "integer", "default": 5},
                "bad": "not-a-dict",
            },
            "required": ["q"],
        }
    )
    assert params["q"]["required"] is True and params["limit"]["default"] == 5 and "bad" not in params

    meta = parse_market_meta({"source": "modelscope", "qualified_name": "a/b", "categories": ["工具", 1]})
    assert meta.get("qualified_name") == "a/b" and meta.get("categories") == ["工具"] and parse_market_meta("x") == {}
    prof = parse_generated_profile(
        {"description": " 简介 ", "keywords": ["k1", 2], "source_hash": "h", "model": "m", "generated_at": 3}
    )
    assert prof is not None and prof["description"] == "简介" and parse_generated_profile({"description": ""}) is None

    node = build_mcp_node_base("cfg1", MCPConfig(name="demo", url="https://x/mcp", description="演示服务"), ["mcp_t1"])
    assert node.node_id == mcp_node_id("cfg1") and node.source == "mcp" and not node.in_roster
    overlay = compute_mcp_overlay(
        {}, node, {"display_name": "自定义", "when_to_use": node.when_to_use, "tool_names": ["hack"]}
    )
    assert overlay == {"display_name": "自定义"}
    assert compute_mcp_overlay(overlay, node, {"display_name": node.display_name}) == {}

    monkeypatch.setattr(
        "gsuid_core.ai_core.register.find_tool_base",
        lambda n: types.SimpleNamespace(plugin="mcp_x" if n != "other" else "builtin"),
    )
    monkeypatch.setattr(
        "gsuid_core.ai_core.agent_node.registry.owning_nodes_of_tools", lambda names: {n: ["mcp_x"] for n in names}
    )
    hits = [RankedHit(name=f"t{i}", label="l", score=float(100 - i)) for i in range(4)] + [
        RankedHit(name="other", label="l", score=1.0)
    ]
    kept, node_ids = _apply_mcp_tool_quota(hits)
    assert len([h for h in kept if h.name != "other"]) == 2 and node_ids == ["mcp_x"]


def test_mcp_market_normalization_and_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.webconsole import mcp_market_api
    from gsuid_core.webconsole.mcp_market_api import (
        MarketEntry,
        _sniff_image,
        check_stdio_runtime,
        _is_public_remote_url,
        _mcpcn_stdio_variants,
        _normalize_market_url,
        _mcpcn_remote_variants,
        _normalize_mcpcn_detail,
        _normalize_market_detail,
        _normalize_market_headers,
    )

    found = check_stdio_runtime(sys.executable)
    assert found["available"] is True and found["runtime"] == "python"
    assert check_stdio_runtime("__gs_no_such_cmd__")["available"] is False
    assert check_stdio_runtime("uvx -y pkg")["command"] == "uvx"
    assert _sniff_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "application/octet-stream") == (".png", "image/png")
    assert _sniff_image(b"<html></html>", "text/html") is None

    ms_detail = _normalize_market_detail(
        {
            "id": "MChina/mcd_mcp_server",
            "name": "mcd",
            "is_hosted": True,
            "is_verified": True,
            "readme": "# EN",
            "locales": {"zh": {"readme": "# 中文介绍"}},
            "operational_url": "https://mcp.api-inference.modelscope.cn/sse/abc",
            "server_config": [],
        }
    )
    assert ms_detail is not None and ms_detail["readme"] == "# 中文介绍" and len(ms_detail["variants"]) == 1

    cn_detail = _normalize_mcpcn_detail(
        {
            "_id": "68f8abc123",
            "name": "mcd-mcp",
            "alias": "麦当劳（中国）",
            "description": {"zh": "麦当劳中国官方 MCP 服务"},
            "mcp_url": {
                "streamable_http_url": "https://mcp.mcd.cn/mcp-servers/mcd-mcp",
                "header_templates": {"Authorization": "Bearer {MCD_MCP_TOKEN}"},
            },
            "env": {"MCD_MCP_TOKEN": {"zh": "Token 说明"}},
            "auth_help": {"obtain_url": "https://open.mcd.cn/mcp/doc"},
            "hosted": True,
            "detail_content": {"zh": "# 麦当劳 MCP"},
            "mcp_config": {
                "mcpServers": {"mcd": {"command": "npx", "args": ["-y", "mcd-mcp"], "env": {"MCD_MCP_TOKEN": ""}}}
            },
        }
    )
    assert cn_detail is not None and cn_detail["qualified_name"] == "mcpcn:68f8abc123"
    assert len(cn_detail["variants"]) == 2 and cn_detail["variants"][0]["transport"] == "streamable_http"

    for bad_url in (
        "http://localhost:8080/see",
        "http://127.0.0.1:8080/sse",
        "http://192.168.1.5/mcp",
        "http://8.217.140.2/mcp",
        "http://mcp-server:3000/",
        "http://mcp.example.com/mcp",
    ):
        assert not _is_public_remote_url(bad_url), bad_url
    assert _is_public_remote_url("https://mcp.so/server/abc")
    assert _mcpcn_stdio_variants({"mcpServers": {"x": {"url": "http://localhost:9/sse"}}}) == []

    vs = _mcpcn_remote_variants(
        {"custom_headers": {"Authorization": "***"}, "streamable_http_url": "https://mcp.tikhub.io/x/mcp"}
    )
    assert vs[0].get("headers") == {"Authorization": "Bearer {AUTHORIZATION}"}
    assert _normalize_market_headers({"x-api-key": "***"}) == ({"x-api-key": "{X_API_KEY}"}, {"X_API_KEY": ""})
    assert _normalize_market_headers({"Authorization": "Bearer ${STORMY_KEY}"}) == (
        {"Authorization": "Bearer {STORMY_KEY}"},
        {"STORMY_KEY": ""},
    )
    assert _normalize_market_url("https://mcp.gezhe.com/mcp?API_KEY=%2A%2A%2A") == (
        "https://mcp.gezhe.com/mcp?API_KEY={API_KEY}",
        {"API_KEY": ""},
    )

    mcp_market_api._DETAIL_CACHE.clear()
    details = {"a/x": {"variants": [{"transport": "stdio"}]}, "b/y": {"variants": []}}

    async def fake_detail(qn: str):
        return details.get(qn)

    monkeypatch.setattr(mcp_market_api, "_get_detail", fake_detail)
    kept, incomplete = asyncio.run(
        mcp_market_api._filter_installable([MarketEntry(qualified_name=q, source="modelscope") for q in ("a/x", "b/y")])
    )
    assert [e["qualified_name"] for e in kept] == ["a/x"] and not incomplete

    key = f"{mcp_market_api._market_endpoint()}|m/x"
    mcp_market_api._DETAIL_CACHE[key] = (time.time(), {"variants": [{"transport": "stdio"}]})

    async def boom(qn: str):
        raise AssertionError("缓存命中不应发起详情请求")

    monkeypatch.setattr(mcp_market_api, "_get_detail", boom)
    kept_cached, _ = asyncio.run(
        mcp_market_api._filter_installable([MarketEntry(qualified_name="m/x", source="modelscope")])
    )
    assert len(kept_cached) == 1
    mcp_market_api._DETAIL_CACHE.clear()


def test_mcp_pii_credentials_and_client_transport(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_core import ErrorDetails
    from pydantic_ai.messages import ImageUrl, RetryInfo, UserPromptPart, RetryFeedbackPart

    import gsuid_core.ai_core.mcp.client as client_mod
    from gsuid_core.ai_core.mcp.pii import (
        mask_pii,
        pii_hits,
        mask_for_agent,
        apply_context_mask,
        has_credential_form,
        is_credential_command,
        mask_credential_command,
    )
    from gsuid_core.ai_core.mcp.client import MCPClient, repair_checkout_script
    from gsuid_core.ai_core.mcp.startup import JsonSchemaObject, _build_mcp_tool_function
    from gsuid_core.ai_core.mcp.bind_command import _parse_bind_args
    from gsuid_core.ai_core.mcp.config_manager import MCPConfig, MCPToolDefinition
    from gsuid_core.ai_core.mcp.credential_store import render_client_kwargs

    s = "收货人 张三 电话13812345678 地址 上海市黄浦区南京东路100号3栋502室"
    masked = mask_pii(s)
    assert "138****5678" in masked and "张*" in masked and "南京东路" not in masked
    assert "phone" in pii_hits(s) and mask_pii("我在黄浦区上班") == "我在黄浦区上班"
    assert "abc123def" not in mask_pii("Authorization: Bearer abc123def456")
    assert has_credential_form("api_key=xyz12345") and not has_credential_form("今天天气不错")

    assert is_credential_command("mcp绑定 瑞幸 tok123") and not is_credential_command("帮我绑定一下瑞幸")
    assert "tok123" not in mask_credential_command("mcp绑定 瑞幸 tok123")
    assert "13812345678" not in mask_for_agent("电话13812345678", is_group=True)
    assert "13812345678" in mask_for_agent("电话13812345678", is_group=False)

    b64_uri = "data:image/png;base64,iVBORw0KGgo13812345678AAAANSUhEUg1234567890123456789=="
    assert mask_pii(b64_uri) == b64_uri and pii_hits(b64_uri) == []
    assert mask_for_agent("base64://abc13812345678xyz==", is_group=True) == "base64://abc13812345678xyz=="
    part_in = UserPromptPart(content=["联系电话13812345678", ImageUrl(url=b64_uri)])
    masked_parts = apply_context_mask([part_in], is_group=True)
    assert len(masked_parts) == 1 and isinstance(masked_parts[0], UserPromptPart)
    masked_content = list(masked_parts[0].content)
    assert masked_content[0] == "联系电话138****5678"
    assert isinstance(masked_content[1], ImageUrl) and masked_content[1].url == b64_uri

    fb_detail: ErrorDetails = {
        "type": "value_error",
        "loc": ("x",),
        "msg": "号码13812345678不合法",
        "input": "13812345678",
        "ctx": {},
        "url": "",
    }
    fb_in = RetryFeedbackPart(content=[fb_detail], retry=RetryInfo(cause="validation_error"))
    fb_out = apply_context_mask([fb_in], is_group=True)
    assert len(fb_out) == 1 and isinstance(fb_out[0], RetryFeedbackPart)
    fb_content = fb_out[0].content
    assert isinstance(fb_content, list)
    assert fb_content[0]["msg"] == "号码138****5678不合法" and fb_content[0]["input"] == "138****5678"

    assert _parse_bind_args("tok123", ["LUCKIN_MCP_TOKEN"]) == {"LUCKIN_MCP_TOKEN": "tok123"}
    assert _parse_bind_args("A_KEY=1 B_KEY=2", ["A_KEY", "B_KEY"]) == {"A_KEY": "1", "B_KEY": "2"}
    assert _parse_bind_args("A_KEY=1", ["A_KEY", "B_KEY"]) is None
    assert _parse_bind_args("abc=", ["TOKEN"]) == {"TOKEN": "abc="}

    cfg = MCPConfig(
        name="gezhe",
        transport="streamable_http",
        url="https://mcp.gezhe.com/mcp?API_KEY={API_KEY}",
        headers={"Authorization": "Bearer {API_KEY}"},
        env={"API_KEY": "admin-key"},
        user_env_keys=["API_KEY"],
    )
    assert render_client_kwargs(cfg, None)["url"] == "https://mcp.gezhe.com/mcp?API_KEY=admin-key"
    assert render_client_kwargs(cfg, {"API_KEY": "user-key"})["url"] == "https://mcp.gezhe.com/mcp?API_KEY=user-key"

    client = MCPClient(name="demo", transport="streamable_http", url="https://x.example/mcp")
    schema: JsonSchemaObject = {
        "type": "object",
        "properties": {"opt_a": {"type": "string"}, "req_b": {"type": "integer"}, "api_key": {"type": "string"}},
        "required": ["req_b", "api_key"],
    }
    fn = _build_mcp_tool_function(
        client, "t1", "desc", schema, tool_def=MCPToolDefinition(name="t1", credential_params=["api_key"])
    )
    params = list(inspect.signature(fn).parameters.values())
    assert [p.name for p in params] == ["ctx", "req_b", "opt_a"]

    tr_npx = MCPClient(name="t", transport="stdio", command="npx", args=["pkg@v1"])._create_transport()
    assert isinstance(tr_npx, StdioTransport) and tr_npx.args[:2] == ["-y", "pkg@v1"]
    tr_py = MCPClient(name="x", transport="stdio", command="python", args=["s.py"], cwd=".")._create_transport()
    assert isinstance(tr_py, StdioTransport) and tr_py.command == sys.executable

    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "mcp_server.py"
    script.write_text('from mcp.server.fastmcp import FastMCP\nmcp = FastMCP("x")\n', encoding="utf-8")
    assert "mcp_server.py" in repair_checkout_script(["scripts/mcp_server.py"], tmp_path)
    assert script.read_text(encoding="utf-8").endswith('if __name__ == "__main__":\n    mcp.run()\n')
    assert repair_checkout_script(["scripts/mcp_server.py"], tmp_path) == ""

    server = FastMCP("synthetic-unmounted")
    calls: List[int] = []
    monkeypatch.setattr(server, "http_app", lambda **kw: (calls.append(1), Starlette())[1])
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", mcp_server._McpHttpRuntime(server))
    monkeypatch.setattr(mcp_server, "_http_mounted", False)
    monkeypatch.setattr(mcp_server, "_mcp_rebuild_lock", asyncio.Lock())
    asyncio.run(mcp_server._rebuild_http_mcp_session())
    assert calls == []

    monkeypatch.setattr(client_mod, "_CONNECT_TIMEOUT_S", 1)
    hang = MCPClient(name="hang", transport="stdio", command=sys.executable, args=["-c", "import time; time.sleep(60)"])
    with pytest.raises(TimeoutError, match="连接超时"):
        asyncio.run(hang.list_tools())
