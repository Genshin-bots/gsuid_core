"""MCP 运行时与 RAG 文本切分契约测试。"""

from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import patch

import pytest
from fastmcp import FastMCP
from starlette.applications import Starlette

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


def test_structure_cut_keeps_decimals_headings_and_fences() -> None:
    decimal = chunk_body("版本是 3.14。下一句很短。", room=12)
    assert len(decimal) == 2
    assert "3.14" in decimal[0]

    headed = chunk_body("## 小节\n" + ("甲" * 300), room=80, overlap=10)
    assert len(headed) > 1
    assert all(piece.startswith("## 小节") and len(piece) <= 80 for piece in headed)

    sentence = "一二三四五六七八九十。"
    assert chunk_body(sentence * 2, room=16, overlap=10) == [sentence, sentence]

    fence = "```\nKEEP\n```"
    fenced = chunk_body(("甲" * 40) + "\n\n" + fence + "\n\n" + ("乙" * 40), room=50)
    assert fence in fenced
    assert chunk_body("   ", room=40) == []
    assert split_text("") == []


def test_embed_budget_clamps_caller_and_recuts_long_sections() -> None:
    pieces = pieces_for_embed("甲" * 2000, title="标" * 40, tags=["标签"], max_tokens=512)
    assert len(pieces) > 1
    assert all(len(piece.embed_text) <= EMBED_CHAR_BUDGET for piece in pieces)

    huge_title = pieces_for_embed("正文", title="题" * 2000, max_tokens=512)
    assert huge_title
    assert all(len(piece.embed_text) <= EMBED_CHAR_BUDGET for piece in huge_title)

    bodies = document_bodies(
        full_text="",
        sections=["甲" * 2000],
        title="题",
        tags=["a"],
        budget=4000,
        overlap=60,
        max_tokens=512,
    )
    assert len(bodies) > 1
    assert all(0 < len(body) <= EMBED_CHAR_BUDGET for body in bodies)


def test_budget_follows_declared_token_limit() -> None:
    assert embed_char_budget(512) == 480
    assert embed_char_budget(8192) == 8160
    assert len(pieces_for_embed("甲" * 2000, max_tokens=8192)) == 1
    narrow = pieces_for_embed("甲" * 2000, max_tokens=512)
    assert len(narrow) > 1
    with patch("gsuid_core.ai_core.rag.base.embedding_provider", None):
        assert embed_char_budget() == 480
        assert chunker_stamp() == "embed-v1@512"


def test_skip_rebuild_uses_chunker_stamp() -> None:
    content_hash = "content-hash"
    stamp = chunk_method_hash(content_hash)
    content_only = calculate_hash({"_content": content_hash})
    assert stamp != content_only
    assert CHUNKER_ID
    assert should_skip_rebuild(stamp, 3, content_hash, 3) is True
    assert should_skip_rebuild(stamp, 2, content_hash, 3) is False


def test_single_piece_keeps_parent_id() -> None:
    assert logical_chunk_ids("doc", 1) == ["doc"]
    assert logical_chunk_ids("doc", 2) == ["doc#0", "doc#1"]


def test_rebuild_skips_when_unmounted(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-unmounted")
    calls: List[int] = []

    def factory(*, path: str, transport: str, stateless_http: bool) -> Starlette:
        calls.append(1)
        return Starlette()

    monkeypatch.setattr(server, "http_app", factory)
    runtime = mcp_server._McpHttpRuntime(server)
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", runtime)
    monkeypatch.setattr(mcp_server, "_http_mounted", False)
    monkeypatch.setattr(mcp_server, "_mcp_last_rebuild", 0.0)
    monkeypatch.setattr(mcp_server, "_mcp_rebuild_lock", asyncio.Lock())

    asyncio.run(mcp_server._rebuild_http_mcp_session())
    assert calls == []


def test_rag_base_calculate_hash() -> None:
    h1 = calculate_hash({"a": 1, "b": "test"})
    h2 = calculate_hash({"b": "test", "a": 1})
    assert h1 == h2
    assert len(h1) == 32


def test_chunk_method_hash_changes_with_tokens() -> None:
    h512 = chunk_method_hash("base", 512)
    h1024 = chunk_method_hash("base", 1024)
    assert h512 != h1024


def test_parse_mcp_json_server_normalizes_drafts() -> None:
    from gsuid_core.ai_core.mcp.config_manager import parse_mcp_json_server

    stdio = parse_mcp_json_server(
        "fs", {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"], "env": {"A": "1"}}
    )
    assert stdio is not None
    assert stdio.get("transport") == "stdio"
    assert stdio.get("command") == "npx" and stdio.get("env") == {"A": "1"}

    sse = parse_mcp_json_server("legacy", {"url": "https://x.example/sse"})
    http = parse_mcp_json_server("remote", {"url": "https://x.example/mcp"})
    assert sse is not None and sse.get("transport") == "sse"
    assert http is not None and http.get("transport") == "streamable_http"
    assert parse_mcp_json_server("bad", "not-a-dict") is None


def test_mcp_config_schema_migrates_old_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from gsuid_core.ai_core.mcp.config_manager import MCP_CONFIG_SCHEMA_VERSION, MCPConfigManager

    old = {"name": "old-srv", "transport": "stdio", "command": "uvx", "enabled": True}
    (tmp_path / "abc.json").write_text(json.dumps(old), encoding="utf-8")
    manager = MCPConfigManager()
    monkeypatch.setattr(manager, "_base_path", tmp_path)
    manager._load_all()

    data = json.loads((tmp_path / "abc.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == MCP_CONFIG_SCHEMA_VERSION
    assert data["register_as_ai_tools"] is True
    assert manager.get_config("abc") is not None


def test_build_mcp_node_defaults_mcp_source_out_of_roster() -> None:
    from gsuid_core.ai_core.mcp.node_profile import mcp_node_id, build_mcp_node_base
    from gsuid_core.ai_core.mcp.config_manager import MCPConfig

    config = MCPConfig(name="demo", url="https://x.example/mcp", description="演示服务")
    node = build_mcp_node_base("cfg1", config, ["mcp_demo_t1"])
    assert node.node_id == mcp_node_id("cfg1")
    assert node.source == "mcp"
    assert node.in_roster is False
    assert node.tool_names == ["mcp_demo_t1"]
    assert "演示服务" in node.when_to_use


def test_apply_mcp_tool_quota_limits_direct_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from gsuid_core.ai_core.buildin_tools.find_tools_rank import RankedHit
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import _apply_mcp_tool_quota

    def fake_tb(name: str):
        return types.SimpleNamespace(plugin="mcp_x" if name != "other" else "builtin")

    monkeypatch.setattr("gsuid_core.ai_core.register.find_tool_base", fake_tb)
    monkeypatch.setattr(
        "gsuid_core.ai_core.agent_node.registry.owning_nodes_of_tools",
        lambda names: {n: ["mcp_x"] for n in names},
    )
    hits = [RankedHit(name=f"t{i}", label="l", score=float(100 - i)) for i in range(4)]
    hits.append(RankedHit(name="other", label="l", score=1.0))
    kept, node_ids = _apply_mcp_tool_quota(hits)
    mcp_kept = [h.name for h in kept if h.name != "other"]
    assert len(mcp_kept) == 2
    assert "other" in [h.name for h in kept]
    assert node_ids == ["mcp_x"]


def test_market_meta_and_generated_profile_parse() -> None:
    from gsuid_core.ai_core.mcp.config_manager import parse_market_meta, parse_generated_profile

    meta = parse_market_meta(
        {"source": "modelscope", "qualified_name": "a/b", "chinese_name": "中文名", "categories": ["工具", 1]}
    )
    assert meta.get("qualified_name") == "a/b" and meta.get("categories") == ["工具"]
    assert parse_market_meta("x") == {}

    profile = parse_generated_profile(
        {"description": "  简介 ", "keywords": ["k1", 2], "source_hash": "h", "model": "m", "generated_at": 3}
    )
    assert profile is not None
    assert profile["description"] == "简介" and profile["keywords"] == ["k1"]
    assert parse_generated_profile({"description": ""}) is None


def test_compute_mcp_overlay_strips_base_equal_fields() -> None:
    from gsuid_core.ai_core.mcp.node_profile import build_mcp_node_base, compute_mcp_overlay
    from gsuid_core.ai_core.mcp.config_manager import MCPConfig

    base = build_mcp_node_base("c", MCPConfig(name="demo", url="https://x/mcp"), [])
    overlay = compute_mcp_overlay(
        {}, base, {"display_name": "自定义", "when_to_use": base.when_to_use, "tool_names": ["hack"]}
    )
    assert overlay.get("display_name") == "自定义"
    assert "when_to_use" not in overlay  # 与基线一致 → 剔除
    assert "tool_names" not in overlay  # 不可覆盖字段
    cleared = compute_mcp_overlay(overlay, base, {"display_name": base.display_name})
    assert "display_name" not in cleared


def test_check_stdio_runtime_detects_path_and_family() -> None:
    import sys

    from gsuid_core.webconsole.mcp_market_api import check_stdio_runtime

    found = check_stdio_runtime(sys.executable)
    assert found["available"] is True and found["resolved"]
    assert found["runtime"] == "python"

    missing = check_stdio_runtime("__gs_no_such_cmd__")
    assert missing["available"] is False and missing["resolved"] is None
    assert missing["runtime"] == "unknown"

    # 命令带参数时按首个 token 归类
    assert check_stdio_runtime("uvx -y pkg")["command"] == "uvx"


def test_sniff_image_accepts_octet_stream_png() -> None:
    from gsuid_core.webconsole.mcp_market_api import _sniff_image

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    assert _sniff_image(png, "application/octet-stream") == (".png", "image/png")
    assert _sniff_image(b"<html><body>x</body></html>", "text/html") is None


def test_normalize_market_detail_readme_hosted_and_single_op_url() -> None:
    from gsuid_core.webconsole.mcp_market_api import _normalize_market_detail

    raw = {
        "id": "MChina/mcd_mcp_server",
        "name": "mcd",
        "is_hosted": True,
        "is_verified": True,
        "readme": "# EN readme",
        "locales": {"zh": {"readme": "# 中文介绍"}},
        "operational_url": "https://mcp.api-inference.modelscope.cn/sse/abc",
        "server_config": [],
    }
    detail = _normalize_market_detail(raw)
    assert detail is not None
    assert detail["readme"] == "# 中文介绍"  # zh locale 优先
    assert detail["is_hosted"] is True and detail["is_verified"] is True
    assert detail["page_url"].endswith("/mcp/servers/MChina/mcd_mcp_server")
    variants = detail["variants"]
    assert len(variants) == 1 and variants[0]["url"].startswith("https://mcp.api-inference")

    # 无 locale 时回落顶层 readme；无 op_url 且无 server_config → 空变体（托管不可自动安装）
    bare = _normalize_market_detail({"id": "a/b", "name": "b", "readme": "# bare"})
    assert bare is not None and bare["readme"] == "# bare" and bare["variants"] == []


def test_normalize_mcpcn_detail_variants() -> None:
    from gsuid_core.webconsole.mcp_market_api import _normalize_mcpcn_detail

    # 真实 mcd 详情形态：官方直连 URL + header 模板变量 + env 中文说明 + auth 指引
    raw = {
        "_id": "68f8abc123",
        "name": "mcd-mcp",
        "alias": "麦当劳（中国）",
        "description": {"zh": "麦当劳中国官方 MCP 服务", "en": "McDonald's China MCP"},
        "mcp_url": {
            "streamable_http_url": "https://mcp.mcd.cn/mcp-servers/mcd-mcp",
            "header_templates": {"Authorization": "Bearer {MCD_MCP_TOKEN}"},
        },
        "env": {"MCD_MCP_TOKEN": {"zh": "麦当劳开放平台申请的 MCP Token"}},
        "auth_help": {"zh": "前往开放平台申请", "obtain_url": "https://open.mcd.cn/mcp/doc"},
        "hosted": True,
        "detail_content": {"zh": "# 麦当劳 MCP\n点餐查询"},
        "url": "https://open.mcd.cn/mcp",
        "mcp_config": {
            "mcpServers": {"mcd": {"command": "npx", "args": ["-y", "mcd-mcp"], "env": {"MCD_MCP_TOKEN": ""}}}
        },
    }
    detail = _normalize_mcpcn_detail(raw)
    assert detail is not None and detail["qualified_name"] == "mcpcn:68f8abc123"
    assert detail["source"] == "mcpmarket" and detail["chinese_name"] == "麦当劳（中国）"
    assert detail["description"] == "麦当劳中国官方 MCP 服务"
    assert detail["is_hosted"] is True and detail["page_url"].endswith("/server/68f8abc123")
    assert detail["obtain_url"] == "https://open.mcd.cn/mcp/doc"
    assert detail["env_hints"]["MCD_MCP_TOKEN"] == "麦当劳开放平台申请的 MCP Token"
    assert detail["readme"].startswith("# 麦当劳")
    v = detail["variants"][0]
    assert v["transport"] == "streamable_http" and v["url"].startswith("https://mcp.mcd.cn")
    assert v["headers"]["Authorization"] == "Bearer {MCD_MCP_TOKEN}" and v["env"] == {"MCD_MCP_TOKEN": ""}
    v2 = detail["variants"][1]
    assert v2["transport"] == "stdio" and v2["command"] == "npx"

    empty = _normalize_mcpcn_detail({"_id": "x1", "name": "no-install"})
    assert empty is not None and empty["variants"] == []
    assert _normalize_mcpcn_detail({"name": "missing-id"}) is None


def test_filter_installable_drops_variantless(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from gsuid_core.webconsole import mcp_market_api
    from gsuid_core.webconsole.mcp_market_api import MarketEntry

    details = {"a/x": {"variants": [{"transport": "stdio"}]}, "b/y": {"variants": []}, "c/z": None}

    async def fake_detail(qn: str):
        return details.get(qn)

    mcp_market_api._DETAIL_CACHE.clear()
    monkeypatch.setattr(mcp_market_api, "_get_detail", fake_detail)
    entries = [MarketEntry(qualified_name=q, source="modelscope") for q in ("a/x", "b/y", "c/z")]
    kept, incomplete = asyncio.run(mcp_market_api._filter_installable(entries))
    assert [e["qualified_name"] for e in kept] == ["a/x"]
    assert incomplete is False


def test_filter_installable_uses_cached_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """详情缓存命中直接分类，绝不发起网络请求。"""
    import time
    import asyncio

    from gsuid_core.webconsole import mcp_market_api
    from gsuid_core.webconsole.mcp_market_api import MarketEntry

    mcp_market_api._DETAIL_CACHE.clear()
    key = f"{mcp_market_api._market_endpoint()}|m/x"
    mcp_market_api._DETAIL_CACHE[key] = (time.time(), {"variants": [{"transport": "stdio"}]})

    async def boom(qn: str):
        raise AssertionError("缓存命中不应发起详情请求")

    monkeypatch.setattr(mcp_market_api, "_get_detail", boom)
    entries = [MarketEntry(qualified_name="m/x", source="modelscope")]
    kept, incomplete = asyncio.run(mcp_market_api._filter_installable(entries))
    assert len(kept) == 1 and incomplete is False


def test_filter_installable_budget_returns_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """未判定条目超预算 → 返回已完成部分 + incomplete=True；后台任务续跑填缓存。"""
    import time
    import asyncio

    from gsuid_core.webconsole import mcp_market_api
    from gsuid_core.webconsole.mcp_market_api import MarketEntry

    mcp_market_api._DETAIL_CACHE.clear()
    monkeypatch.setattr(mcp_market_api, "_DETAIL_CHECK_BUDGET", 0.05)
    done: list[str] = []

    async def slow_detail(qn: str):
        await asyncio.sleep(0.4)
        done.append(qn)
        key = f"{mcp_market_api._market_endpoint()}|{qn}"
        mcp_market_api._DETAIL_CACHE[key] = (time.time(), {"variants": [{"transport": "stdio"}]})
        return {"variants": [{"transport": "stdio"}]}

    monkeypatch.setattr(mcp_market_api, "_get_detail", slow_detail)
    entries = [MarketEntry(qualified_name=f"m{i}/x", source="modelscope") for i in range(3)]

    async def run() -> None:
        kept, incomplete = await mcp_market_api._filter_installable(entries)
        assert kept == [] and incomplete is True
        # 后台任务继续跑完并写入详情缓存
        await asyncio.sleep(0.8)
        assert len(done) == 3
        assert len(mcp_market_api._DETAIL_CACHE) == 3

    asyncio.run(run())
    mcp_market_api._DETAIL_CACHE.clear()


def test_mcp_tool_signature_required_before_optional() -> None:
    import inspect

    from gsuid_core.ai_core.mcp.client import MCPClient
    from gsuid_core.ai_core.mcp.startup import JsonSchemaObject, _build_mcp_tool_function

    client = MCPClient(name="demo", transport="streamable_http", url="https://x.example/mcp")
    schema: JsonSchemaObject = {
        "type": "object",
        "properties": {
            "opt_a": {"type": "string"},
            "req_b": {"type": "integer"},
            "opt_c": {"type": "boolean"},
        },
        "required": ["req_b"],
    }
    fn = _build_mcp_tool_function(client, "t1", "desc", schema)
    params = list(inspect.signature(fn).parameters.values())
    assert [p.name for p in params] == ["ctx", "req_b", "opt_a", "opt_c"]
    assert params[1].default is inspect.Parameter.empty
    assert params[2].default is None and params[3].default is None


def test_input_schema_to_parameters_preserves_required_and_extras() -> None:
    from gsuid_core.ai_core.mcp.config_manager import input_schema_to_parameters

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
    assert params["q"] == {"type": "string", "description": "查询词", "required": True}
    assert params["limit"] == {"type": "integer", "description": "上限", "default": 5}
    assert "bad" not in params
    assert input_schema_to_parameters({"properties": "nope"}) == {}


# ── 多租户凭据 / 脱敏（schema v3）─────────────────────────────────


def test_pii_mask_phone_address_name_credential() -> None:
    from gsuid_core.ai_core.mcp.pii import mask_pii, pii_hits

    s = "收货人 张三 电话13812345678 地址 上海市黄浦区南京东路100号3栋502室"
    masked = mask_pii(s)
    assert "13812345678" not in masked and "138****5678" in masked
    assert "张*" in masked and "南京东路" not in masked and "黄浦区***" in masked
    assert "phone" in pii_hits(s) and "name_field" in pii_hits(s)
    # 非地址句不误掩
    assert mask_pii("我在黄浦区上班") == "我在黄浦区上班"


def test_pii_mask_credential_forms() -> None:
    from gsuid_core.ai_core.mcp.pii import mask_pii, has_credential_form

    assert "abc123def" not in mask_pii("Authorization: Bearer abc123def456")
    assert "secret99" not in mask_pii("token=secret99xx")
    assert has_credential_form("api_key=xyz12345")
    assert not has_credential_form("今天天气不错")


def test_credential_command_mask() -> None:
    from gsuid_core.ai_core.mcp.pii import (
        mask_for_agent,
        is_credential_command,
        mask_credential_command,
    )

    assert is_credential_command("mcp绑定 瑞幸 tok123")
    assert is_credential_command("mcp绑定列表")
    assert not is_credential_command("帮我绑定一下瑞幸")
    masked = mask_credential_command("mcp绑定 瑞幸 tok123")
    assert "tok123" not in masked and "mcp绑定" in masked
    # 群聊全掩 + 私聊只掩凭据形态
    grp = mask_for_agent("电话13812345678", is_group=True)
    prv = mask_for_agent("电话13812345678", is_group=False)
    assert "13812345678" not in grp and "13812345678" in prv


def test_suggest_shared_env_keys() -> None:
    from gsuid_core.ai_core.mcp.config_manager import suggest_shared_env_keys

    env = {
        "MINIMAX_API_KEY": "k",
        "MINIMAX_API_HOST": "https://x",
        "MINIMAX_API_RESOURCE_MODE": "public",
        "LUCKIN_MCP_TOKEN": "t",
    }
    shared = suggest_shared_env_keys(env)
    assert "MINIMAX_API_HOST" in shared
    assert "MINIMAX_API_RESOURCE_MODE" in shared
    assert "MINIMAX_API_KEY" not in shared
    assert "LUCKIN_MCP_TOKEN" not in shared


def test_schema_v3_migration_defaults() -> None:
    from gsuid_core.ai_core.mcp.config_manager import MCPToolDefinition

    tool = MCPToolDefinition.from_dict({"name": "t1", "description": "d"})
    assert tool.exposure == "private"
    assert tool.mutating is False and tool.sensitive_args is False
    assert tool.credential_params == []
    tool2 = MCPToolDefinition.from_dict({"name": "t2", "exposure": "public", "mutating": True})
    assert tool2.exposure == "public" and tool2.mutating is True


def test_schema_v3_migration_env_all_bindable(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from gsuid_core.ai_core.mcp.config_manager import MCPConfigManager

    old = {
        "name": "luckin",
        "transport": "streamable_http",
        "url": "https://x.example/mcp",
        "env": {"LUCKIN_MCP_TOKEN": "tok"},
        "tools": [{"name": "t1"}],
        "schema_version": 2,
    }
    (tmp_path / "luckin.json").write_text(json.dumps(old), encoding="utf-8")
    manager = MCPConfigManager()
    monkeypatch.setattr(manager, "_base_path", tmp_path)
    manager._load_all()

    data = json.loads((tmp_path / "luckin.json").read_text(encoding="utf-8"))
    assert data["user_env_keys"] == ["LUCKIN_MCP_TOKEN"]
    assert data["tools"][0]["exposure"] == "private"
    cfg = manager.get_config("luckin")
    assert cfg is not None and cfg.user_env_keys == ["LUCKIN_MCP_TOKEN"]


def test_credential_params_signature_removed() -> None:
    import inspect

    from gsuid_core.ai_core.mcp.client import MCPClient
    from gsuid_core.ai_core.mcp.startup import JsonSchemaObject, _build_mcp_tool_function
    from gsuid_core.ai_core.mcp.config_manager import MCPToolDefinition

    client = MCPClient(name="demo", transport="streamable_http", url="https://x.example/mcp")
    schema: JsonSchemaObject = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "api_key": {"type": "string"},
        },
        "required": ["city", "api_key"],
    }
    tool_def = MCPToolDefinition(name="t1", credential_params=["api_key"])
    fn = _build_mcp_tool_function(client, "t1", "desc", schema, tool_def=tool_def)
    names = [p.name for p in inspect.signature(fn).parameters.values()]
    assert "api_key" not in names and "city" in names


def test_parse_bind_args_tail_rules() -> None:
    from gsuid_core.ai_core.mcp.bind_command import _resolve_config, _parse_bind_args  # noqa: F401

    # 单 key 裸 token
    assert _parse_bind_args("tok123", ["LUCKIN_MCP_TOKEN"]) == {"LUCKIN_MCP_TOKEN": "tok123"}
    # 单 key k=v
    assert _parse_bind_args("LUCKIN_MCP_TOKEN=t1", ["LUCKIN_MCP_TOKEN"]) == {"LUCKIN_MCP_TOKEN": "t1"}
    # 多 key 必须绑齐
    keys = ["A_KEY", "B_KEY"]
    assert _parse_bind_args("A_KEY=1 B_KEY=2", keys) == {"A_KEY": "1", "B_KEY": "2"}
    assert _parse_bind_args("A_KEY=1", keys) is None
    # 含 = 的 token（base64 填充）不匹配已知 key 时按裸 token 处理
    assert _parse_bind_args("abc=", ["TOKEN"]) == {"TOKEN": "abc="}
    assert _parse_bind_args("abc= xyz", ["TOKEN"]) is None


# ── stdio 连接超时 / npx -y 注入 ──────────────────────────────


def test_stdio_transport_injects_npx_yes() -> None:
    from fastmcp.client.transports import StdioTransport

    from gsuid_core.ai_core.mcp.client import MCPClient

    def _args(command: str, args: list[str]) -> list[str]:
        tr = MCPClient(name="t", transport="stdio", command=command, args=args)._create_transport()
        assert isinstance(tr, StdioTransport)
        return tr.args

    # npx 不带 -y 会在 stdin 等确认 → 非 TTY 卡死握手
    assert _args("npx", ["pkg@v1", "run"])[:2] == ["-y", "pkg@v1"]
    # 已带 -y/--yes 不重复注入
    assert _args("npx", ["-y", "x"]) == ["-y", "x"]
    assert _args("npx", ["--yes", "x"]) == ["--yes", "x"]
    # 非 npx 命令不动
    assert _args("node", ["a.js"]) == ["a.js"]


def test_list_tools_bounded_by_connect_timeout(monkeypatch) -> None:
    import sys
    import time
    import asyncio

    import gsuid_core.ai_core.mcp.client as client_mod
    from gsuid_core.ai_core.mcp.client import MCPClient

    monkeypatch.setattr(client_mod, "_CONNECT_TIMEOUT_S", 2)
    # 子进程起得来但永远不说 MCP 协议 → 握手必须被超时打断而非无限挂
    c = MCPClient(
        name="hang",
        transport="stdio",
        command=sys.executable,
        args=["-c", "import time; time.sleep(60)"],
    )
    t0 = time.time()
    try:
        asyncio.run(c.list_tools())
        raise AssertionError("expected TimeoutError")
    except TimeoutError as e:
        assert "连接超时" in str(e)
    assert time.time() - t0 < 30


# ── 市场远程变体端点过滤 ─────────────────────────────────────


def test_market_remote_variant_requires_public_url() -> None:
    from gsuid_core.webconsole.mcp_market_api import _is_public_remote_url, _mcpcn_stdio_variants

    # localhost/回环/内网/占位 → 不可作为市场安装方式
    for u in [
        "http://localhost:8080/see",
        "http://foo.localhost/sse",
        "http://127.0.0.1:8080/sse",
        "http://[::1]:8080/sse",
        "http://10.0.0.5/sse",
        "http://192.168.1.5/mcp",
        "http://8.217.140.2/mcp",  # 公网 IP 字面量也拒（代跑共享实例）
        "http://mcp-server:3000/",  # docker 单标签内部名
        "http://printer.local/mcp",
        "http://x.internal/sse",
        "http://mcp.example.com/mcp",  # RFC2606 占位域名
        "http://user@localhost:9/mcp",
    ]:
        assert not _is_public_remote_url(u), u
    for u in [
        "https://mcp.so/server/abc",
        "https://dashscope.aliyuncs.com/api/v1/mcps/x/mcp",
        "https://my-mcp.example-corp.com/sse",
    ]:
        assert _is_public_remote_url(u), u
    # mcpServers 条目声明 url 型远程变体同样被拦
    assert _mcpcn_stdio_variants({"mcpServers": {"x": {"url": "http://localhost:9/sse"}}}) == []
    ok = _mcpcn_stdio_variants({"mcpServers": {"x": {"url": "https://ok.example.io/mcp"}}})
    assert len(ok) == 1


def test_market_masked_and_dollar_headers_become_env_placeholders() -> None:
    from gsuid_core.webconsole.mcp_market_api import _mcpcn_remote_variants, _normalize_market_headers

    # mcpmarket 把真实 header 值脱敏为 "***" → 转用户可填占位，Authorization 按主流 Bearer
    vs = _mcpcn_remote_variants(
        {
            "custom_headers": {"Authorization": "***"},
            "streamable_http_url": "https://mcp.tikhub.io/xiaohongshu/mcp",
        }
    )
    assert vs[0].get("headers") == {"Authorization": "Bearer {AUTHORIZATION}"}
    assert vs[0].get("env") == {"AUTHORIZATION": ""}
    # 非 Authorization 头脱敏 → 全值占位，env 名由 header 名派生
    hs, env = _normalize_market_headers({"x-api-key": "***"})
    assert hs == {"x-api-key": "{X_API_KEY}"} and env == {"X_API_KEY": ""}
    # ${VAR} 归一为 {VAR} 并提取；正常 {VAR} 与静态值原样
    hs, env = _normalize_market_headers({"Authorization": "Bearer ${STORMY_API_KEY}"})
    assert hs == {"Authorization": "Bearer {STORMY_API_KEY}"} and env == {"STORMY_API_KEY": ""}
    hs, env = _normalize_market_headers({"Authorization": "Bearer {TOK}", "X-Trace": "abc"})
    assert hs == {"Authorization": "Bearer {TOK}", "X-Trace": "abc"} and env == {"TOK": ""}


def test_market_url_query_credential_becomes_env_placeholder() -> None:
    """上游把凭据脱敏拼进 URL query（?API_KEY=%2A%2A%2A）→ {ENV} 占位 + env 键。"""
    from gsuid_core.webconsole.mcp_market_api import _normalize_market_url, _mcpcn_remote_variants

    url, env = _normalize_market_url("https://mcp.gezhe.com/mcp?API_KEY=%2A%2A%2A")
    assert url == "https://mcp.gezhe.com/mcp?API_KEY={API_KEY}" and env == {"API_KEY": ""}
    url, env = _normalize_market_url("https://a.com/mcp?api_key=your_api_key&x=1")
    assert url == "https://a.com/mcp?api_key={API_KEY}&x=1" and env == {"API_KEY": ""}
    url, env = _normalize_market_url("https://a.com/mcp?token={MY_TOKEN}&v=2")
    assert url == "https://a.com/mcp?token={MY_TOKEN}&v=2" and env == {"MY_TOKEN": ""}
    url, env = _normalize_market_url("https://a.com/mcp?key=${SK_KEY}")
    assert url == "https://a.com/mcp?key={SK_KEY}" and env == {"SK_KEY": ""}
    url, env = _normalize_market_url("https://a.com/mcp?key=realvalue123")
    assert url == "https://a.com/mcp?key=realvalue123" and env == {}
    url, env = _normalize_market_url("https://a.com/mcp")
    assert url == "https://a.com/mcp" and env == {}

    # mcp_url 远端变体整链：URL 占位 + env 提取
    vs = _mcpcn_remote_variants({"streamable_http_url": "https://mcp.gezhe.com/mcp?API_KEY=%2A%2A%2A"})
    assert vs[0].get("url") == "https://mcp.gezhe.com/mcp?API_KEY={API_KEY}"
    assert vs[0].get("env") == {"API_KEY": ""}


def test_render_client_kwargs_renders_url_placeholders() -> None:
    """url 里的 {KEY} 占位：共享 scope 填 env 值，绑定用户换绑定值。"""
    from gsuid_core.ai_core.mcp.config_manager import MCPConfig
    from gsuid_core.ai_core.mcp.credential_store import render_client_kwargs

    cfg = MCPConfig(
        name="gezhe",
        transport="streamable_http",
        url="https://mcp.gezhe.com/mcp?API_KEY={API_KEY}",
        headers={"Authorization": "Bearer {API_KEY}"},
        env={"API_KEY": "admin-shared-key-123"},
        user_env_keys=["API_KEY"],
    )
    shared = render_client_kwargs(cfg, None)
    assert shared["url"] == "https://mcp.gezhe.com/mcp?API_KEY=admin-shared-key-123"
    assert shared["headers"]["Authorization"] == "Bearer admin-shared-key-123"
    bound = render_client_kwargs(cfg, {"API_KEY": "user-key-999"})
    assert bound["url"] == "https://mcp.gezhe.com/mcp?API_KEY=user-key-999"
    assert bound["headers"]["Authorization"] == "Bearer user-key-999"

    # env 空值纯绑定配置：未绑定保留占位（凭据必填、失败可预期），绑定后替换
    cfg2 = MCPConfig(
        name="g2",
        transport="streamable_http",
        url="https://mcp.gezhe.com/mcp?API_KEY={API_KEY}",
        env={"API_KEY": ""},
        user_env_keys=["API_KEY"],
    )
    assert render_client_kwargs(cfg2, None)["url"] == "https://mcp.gezhe.com/mcp?API_KEY={API_KEY}"
    assert render_client_kwargs(cfg2, {"API_KEY": "mykey"})["url"] == ("https://mcp.gezhe.com/mcp?API_KEY=mykey")


def test_repair_checkout_script_appends_fastmcp_run_guard(tmp_path) -> None:
    """上游入口脚本只定义 FastMCP 不 .run() → 自动补启动守卫；幂等不误伤。"""
    from gsuid_core.ai_core.mcp.client import repair_checkout_script

    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "mcp_server.py"
    script.write_text('from mcp.server.fastmcp import FastMCP\nmcp = FastMCP("x")\n', encoding="utf-8")
    note = repair_checkout_script(["scripts/mcp_server.py"], tmp_path)
    assert "mcp_server.py" in note
    text = script.read_text(encoding="utf-8")
    assert text.endswith('if __name__ == "__main__":\n    mcp.run()\n')
    # 幂等：已含 .run( 不再重复追加
    assert repair_checkout_script(["scripts/mcp_server.py"], tmp_path) == ""
    # 已有 __main__ 守卫的脚本不动
    ok = tmp_path / "ok.py"
    ok.write_text('mcp = FastMCP("y")\nif __name__ == "__main__":\n    mcp.run()\n', encoding="utf-8")
    assert repair_checkout_script(["ok.py"], tmp_path) == ""
    # 非 FastMCP / 非 .py 参数不碰
    plain = tmp_path / "plain.py"
    plain.write_text("print('hi')\n", encoding="utf-8")
    assert repair_checkout_script(["plain.py"], tmp_path) == ""
    assert repair_checkout_script(["-y", "pkg@latest"], tmp_path) == ""


def test_stdio_transport_rewrites_bare_python_to_sys_executable() -> None:
    """裸 python 命令改写为 Core 解释器（系统 python 缺 MCP 依赖秒退）；显式路径保留。"""
    import sys

    from fastmcp.client.transports import StdioTransport

    from gsuid_core.ai_core.mcp.client import MCPClient

    tr = MCPClient(name="x", transport="stdio", command="python", args=["s.py"], cwd=".")._create_transport()
    assert isinstance(tr, StdioTransport) and tr.command == sys.executable
    tr = MCPClient(name="x", transport="stdio", command="python3", args=["s.py"], cwd=".")._create_transport()
    assert isinstance(tr, StdioTransport) and tr.command == sys.executable
    tr = MCPClient(name="x", transport="stdio", command="/usr/bin/python3", args=["s.py"], cwd=".")._create_transport()
    assert isinstance(tr, StdioTransport) and tr.command == "/usr/bin/python3"
    tr = MCPClient(name="x", transport="stdio", command="node", args=["s.js"], cwd=".")._create_transport()
    assert isinstance(tr, StdioTransport) and tr.command == "node"
