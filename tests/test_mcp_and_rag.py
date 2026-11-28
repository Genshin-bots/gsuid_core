"""MCP 运行时、RAG 文本切分与认知枢纽测试。"""

from __future__ import annotations

import re
import sys
import json
import time
import shutil
import asyncio
import inspect
from pathlib import Path
from dataclasses import dataclass

import pytest
from fastmcp.client.transports import StdioTransport

from tests._support import isolated_sqlite_db
from gsuid_core.ai_core.rag.chunking import (
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


def test_rag_chunking_and_rebuild_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.ai_core.rag.base as rag_base

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

    assert len(pieces_for_embed("甲" * 2000, max_tokens=8192)) == 1
    with monkeypatch.context() as mp:
        mp.setattr(rag_base, "embedding_provider", None)
        assert embed_char_budget() == 480 and chunker_stamp() == "embed-v1@512"

    stamp = chunk_method_hash("content-hash")
    assert should_skip_rebuild(stamp, 3, "content-hash", 3) is True
    assert should_skip_rebuild(stamp, 2, "content-hash", 3) is False
    assert chunk_method_hash("base", 512) != chunk_method_hash("base", 1024)
    assert logical_chunk_ids("doc", 1) == ["doc"] and logical_chunk_ids("doc", 2) == ["doc#0", "doc#1"]


def test_mcp_config_node_profile_and_tool_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.ai_core.mcp.config_manager as cfg_mod
    from gsuid_core.ai_core.register import find_tool_base
    from gsuid_core.ai_core.agent_node import unregister_agent_node
    from gsuid_core.ai_core.mcp.startup import _forget_mcp_server, register_single_mcp_server
    from gsuid_core.ai_core.mcp.node_profile import (
        mcp_node_id,
        build_mcp_node_base,
        compute_mcp_overlay,
        mcp_registered_tool_name,
        mcp_hint_allowed_for_turn,
    )
    from gsuid_core.ai_core.agent_run.support import _capability_exclusive_tool_names
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
    from gsuid_core.ai_core.buildin_tools.find_tools_rank import RankedHit, classify_callable_tool
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import _apply_mcp_tool_quota

    for name, raw, expected_transport, expected_env in (
        ("fs", {"command": "npx", "args": ["-y", "srv"], "env": {"A": "1"}}, "stdio", {"A": "1"}),
        ("legacy", {"url": "https://x.example/sse"}, "sse", None),
        ("remote", {"url": "https://x.example/mcp"}, "streamable_http", None),
        ("bad", "not-a-dict", None, None),
    ):
        parsed = parse_mcp_json_server(name, raw)
        if expected_transport is None:
            assert parsed is None
        else:
            assert parsed is not None and "transport" in parsed and parsed["transport"] == expected_transport
            if expected_env is not None:
                assert "env" in parsed and parsed["env"] == expected_env

    old = {
        "name": "luckin",
        "transport": "streamable_http",
        "url": "https://x.example/mcp",
        "env": {"LUCKIN_MCP_TOKEN": "tok"},
        "tools": [{"name": "t1"}],
    }
    (tmp_path / "luckin.json").write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "MCP_CONFIGS_PATH", tmp_path)
    manager = MCPConfigManager()
    data = json.loads((tmp_path / "luckin.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == MCP_CONFIG_SCHEMA_VERSION and data["register_as_ai_tools"] is True
    assert data["user_env_keys"] == ["LUCKIN_MCP_TOKEN"] and data["tools"][0]["exposure"] == "private"

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
    assert "qualified_name" in meta and meta["qualified_name"] == "a/b"
    assert "categories" in meta and meta["categories"] == ["工具"]
    assert parse_market_meta("x") == {}
    prof = parse_generated_profile(
        {"description": " 简介 ", "keywords": ["k1", 2], "source_hash": "h", "model": "m", "generated_at": 3}
    )
    assert prof is not None and prof["description"] == "简介" and parse_generated_profile({"description": ""}) is None

    node = build_mcp_node_base("cfg1", MCPConfig(name="demo", url="https://x/mcp", description="演示服务"), ["mcp_t1"])
    overlay = compute_mcp_overlay(
        {}, node, {"display_name": "自定义", "when_to_use": node.when_to_use, "tool_names": ["hack"]}
    )
    assert overlay == {"display_name": "自定义"}
    assert compute_mcp_overlay(overlay, node, {"display_name": node.display_name}) == {}

    fallback_cfg = MCPConfig(
        name="Demo's Shop",
        transport="streamable_http",
        url="https://x.example/mcp",
        description="搜索活动日历、领取优惠券等核心能力",
        market_meta={"chinese_name": "演示门店（中国）"},
        register_as_ai_tools=True,
        tools=[MCPToolDefinition(name="claim-coupon", description="自动领取当前可用优惠券\n补充说明")],
    )
    manager._cache["demo_s_shop"] = fallback_cfg

    async def _timeout_list_tools(_self: object) -> list[object]:
        raise TimeoutError("连接超时")

    async def _noop_async(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr("gsuid_core.ai_core.mcp.startup.mcp_config_manager", manager)
    monkeypatch.setattr("gsuid_core.ai_core.mcp.client.MCPClient.list_tools", _timeout_list_tools)
    monkeypatch.setattr("gsuid_core.ai_core.mcp.startup._sync_tool_index", _noop_async)
    monkeypatch.setattr("gsuid_core.ai_core.mcp.startup._warm_agent_node_index", _noop_async)
    monkeypatch.setattr("gsuid_core.ai_core.mcp.profile_generator.schedule_profile_generation", lambda *_a: False)
    try:
        reg_count, _ = asyncio.run(register_single_mcp_server("demo_s_shop"))
        assert reg_count == 1
        sanitized = mcp_registered_tool_name("Demo's Shop", "claim-coupon")
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sanitized) is not None
        reg_name = "mcp_demo_s_shop_claim-coupon"
        tb = find_tool_base(reg_name)
        assert tb is not None and tb.tool.name == reg_name
        assert "演示门店（中国）" in tb.description and tb.covers[0] == "[演示门店（中国）] 自动领取当前可用优惠券"

        private_cfg = MCPConfig(
            name="DemoBrand",
            transport="http",
            url="https://example.invalid/mcp",
            enabled=True,
            user_env_keys=["DEMO_TOKEN"],
            market_meta={"chinese_name": "演示品牌"},
            tools=[MCPToolDefinition(name="query-my-account", description="查询积分账户", exposure="private")],
        )
        manager._cache["demo_brand"] = private_cfg
        monkeypatch.setattr(cfg_mod, "mcp_config_manager", manager)

        async def _no_creds(*_a: object, **_kw: object) -> tuple[None, None]:
            return None, None

        monkeypatch.setattr("gsuid_core.ai_core.mcp.credential_store.resolve_user_creds", _no_creds)
        for query, allowed in (("我有多少钱", False), ("查一下演示品牌积分", True)):
            assert (
                asyncio.run(
                    mcp_hint_allowed_for_turn("mcp_demo_brand_query-my-account", query, bot_id="b1", user_id="u1")
                )
                is allowed
            )

        exclusive_names = _capability_exclusive_tool_names()
        assert reg_name not in exclusive_names
        assert (
            classify_callable_tool(
                name=tb.name,
                category=tb.category,
                plugin=tb.plugin,
                hide_from_main=tb.hide_from_main,
                exclusive=exclusive_names,
            )
            == "dedicated"
        )
    finally:
        _forget_mcp_server("demo_s_shop")
        unregister_agent_node(mcp_node_id("demo_s_shop"))

    @dataclass(frozen=True)
    class _PluginStub:
        plugin: str

    monkeypatch.setattr(
        "gsuid_core.ai_core.register.find_tool_base",
        lambda n: _PluginStub(plugin="mcp_x" if n != "other" else "builtin"),
    )
    hits = [RankedHit(name=f"t{i}", label="l", score=float(100 - i)) for i in range(4)] + [
        RankedHit(name="other", label="l", score=1.0)
    ]
    kept, _ = _apply_mcp_tool_quota(hits)
    assert [h.name for h in kept] == ["t0", "t1", "other"]


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

    py_cmd = "python" if shutil.which("python") else "python3"
    found = check_stdio_runtime(py_cmd)
    assert found["available"] is True and found["runtime"] == "python"
    assert check_stdio_runtime("__gs_no_such_cmd__")["available"] is False
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
            "tools_enrich": [{"name": "query-stores", "description": "查询附近门店"}],
            "tools": [
                {
                    "name": "query-stores",
                    "description": "EN desc",
                    "inputSchema": {
                        "properties": {"city": {"type": "string", "description": "城市"}},
                        "required": ["city"],
                    },
                }
            ],
        }
    )
    assert cn_detail is not None and cn_detail["qualified_name"] == "mcpcn:68f8abc123"
    assert len(cn_detail["variants"]) == 2 and cn_detail["variants"][0]["transport"] == "streamable_http"
    assert cn_detail["tools"][0]["description"] == "查询附近门店"
    assert cn_detail["tools"][0]["params"][0]["name"] == "city" and cn_detail["tools"][0]["params"][0]["required"]

    for url, expected_public in (
        ("http://localhost:8080/see", False),
        ("http://127.0.0.1:8080/sse", False),
        ("http://192.168.1.5/mcp", False),
        ("http://8.217.140.2/mcp", False),
        ("http://mcp-server:3000/", False),
        ("http://mcp.example.com/mcp", False),
        ("https://mcp.so/server/abc", True),
    ):
        assert _is_public_remote_url(url) is expected_public, url
    assert _mcpcn_stdio_variants({"mcpServers": {"x": {"url": "http://localhost:9/sse"}}}) == []

    vs = _mcpcn_remote_variants(
        {"custom_headers": {"Authorization": "***"}, "streamable_http_url": "https://mcp.tikhub.io/x/mcp"}
    )
    assert "headers" in vs[0] and vs[0]["headers"] == {"Authorization": "Bearer {AUTHORIZATION}"}
    for raw_headers, expected_headers, expected_env in (
        ({"x-api-key": "***"}, {"x-api-key": "{X_API_KEY}"}, {"X_API_KEY": ""}),
        ({"Authorization": "Bearer ${STORMY_KEY}"}, {"Authorization": "Bearer {STORMY_KEY}"}, {"STORMY_KEY": ""}),
    ):
        assert _normalize_market_headers(raw_headers) == (expected_headers, expected_env)
    assert _normalize_market_url("https://mcp.gezhe.com/mcp?API_KEY=%2A%2A%2A") == (
        "https://mcp.gezhe.com/mcp?API_KEY={API_KEY}",
        {"API_KEY": ""},
    )

    mcp_market_api._DETAIL_CACHE.clear()
    try:
        details: dict[str, dict[str, list[dict[str, str]]]] = {
            "a/x": {"variants": [{"transport": "stdio"}]},
            "b/y": {"variants": []},
        }

        async def fake_detail(qn: str) -> dict[str, list[dict[str, str]]] | None:
            return details[qn] if qn in details else None

        monkeypatch.setattr(mcp_market_api, "_get_detail", fake_detail)
        kept, incomplete = asyncio.run(
            mcp_market_api._filter_installable(
                [MarketEntry(qualified_name=q, source="modelscope") for q in ("a/x", "b/y")]
            )
        )
        assert [e["qualified_name"] for e in kept] == ["a/x"] and not incomplete

        async def budget_scenario() -> None:
            monkeypatch.setattr(mcp_market_api, "_DETAIL_CHECK_BUDGET", 0.03)
            monkeypatch.setattr(mcp_market_api, "_BG_DRAIN_CAP", 3)

            async def slow_detail(qn: str) -> dict[str, list[dict[str, str]]]:
                if qn == "fast/live":
                    return {"variants": [{"transport": "stdio"}]}
                await asyncio.sleep(0.2)
                return {"variants": [{"transport": "stdio"}]}

            monkeypatch.setattr(mcp_market_api, "_get_detail", slow_detail)
            batch = [MarketEntry(qualified_name="fast/live", source="modelscope")] + [
                MarketEntry(qualified_name=f"slow/{i}", source="modelscope") for i in range(10)
            ]
            kept_part, inc = await mcp_market_api._filter_installable(batch)
            state = mcp_market_api._loop_state()
            assert [e["qualified_name"] for e in kept_part] == ["fast/live"] and inc is True
            assert len(state.bg_drain_queue) <= 3
            if state.bg_drain_task is not None:
                state.bg_drain_task.cancel()
                await asyncio.gather(state.bg_drain_task, return_exceptions=True)

        asyncio.run(budget_scenario())
    finally:
        mcp_market_api._DETAIL_CACHE.clear()


def test_mcp_pii_credentials_and_client_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_core import ErrorDetails
    from pydantic_ai.messages import ImageUrl, RetryInfo, UserPromptPart, RetryFeedbackPart

    import gsuid_core.ai_core.mcp.client as client_mod
    from gsuid_core.ai_core.mcp.pii import (
        mask_pii,
        pii_hits,
        mask_for_agent,
        mask_pii_in_html,
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

    for text, must_have, must_hide, expected_hit in (
        (
            "收货人 张三 电话13812345678 地址 上海市黄浦区南京东路100号3栋502室",
            ("138********", "收货人 **"),
            ("张", "南京东路"),
            "phone",
        ),
        ("送到浙江省杭州市西湖区文三路90号2单元401室", (), ("文三路",), "address"),
        ("我住上海市黄浦区南京东路100号3栋502室靠近静安区", (), ("南京东路",), "address"),
        (
            "| **收货人** | 李四五 |\n| **收货地址** | 广东省深圳市南山区科技南十二路18号 |",
            ("***",),
            ("李", "科技南十二路"),
            "",
        ),
        ("Authorization: Bearer abc123def456", (), ("abc123def",), ""),
    ):
        masked = mask_pii(text)
        for token in must_have:
            assert token in masked
        for token in must_hide:
            assert token not in masked
        if expected_hit:
            assert expected_hit in pii_hits(text)

    b64_uri = "data:image/png;base64,iVBORw0KGgo13812345678AAAANSUhEUg1234567890123456789=="
    # 旅游攻略、公共景点与 base64 数据 URI 不误伤。
    for safe_text in (
        "我在黄浦区上班",
        "推荐路线：1. 上海市黄浦区南京东路299号 2. 地址：北京市东城区景山前街4号故宫博物院 3. 杭州市西湖区龙井路1号",
        b64_uri,
    ):
        assert mask_pii(safe_text) == safe_text and pii_hits(safe_text) == []

    html_order = (
        "<style>.c{color:#138123}</style><div class='c'>"
        "<span>收货人</span><span>王五</span>"
        "<span>电话</span><span>13987654321</span>"
        "<span>地址</span><span>上海市黄浦区南京东路100号</span></div>"
    )
    masked_html = mask_pii_in_html(html_order)
    assert "王" not in masked_html and "<span>**</span>" in masked_html
    assert "139********" in masked_html and "南京东路" not in masked_html and "#138123" in masked_html
    html_travel = "<div><span>景点</span><span>故宫</span><span>地址</span><span>北京市东城区景山前街4号</span></div>"
    assert mask_pii_in_html(html_travel) == html_travel
    assert "张三" not in mask_pii_in_html("收货人 张三 电话13812345678")

    for text, expected in (("api_key=xyz12345", True), ("今天天气不错", False)):
        assert has_credential_form(text) is expected
    for text, expected in (("mcp绑定 瑞幸 tok123", True), ("帮我绑定一下瑞幸", False)):
        assert is_credential_command(text) is expected
    assert "tok123" not in mask_credential_command("mcp绑定 瑞幸 tok123")
    assert "13812345678" not in mask_for_agent("电话13812345678", is_group=True)
    assert "13812345678" in mask_for_agent("电话13812345678", is_group=False)
    assert mask_for_agent("base64://abc13812345678xyz==", is_group=True) == "base64://abc13812345678xyz=="

    part_in = UserPromptPart(content=["联系电话13812345678", ImageUrl(url=b64_uri)])
    masked_parts = apply_context_mask([part_in], is_group=True)
    assert len(masked_parts) == 1 and isinstance(masked_parts[0], UserPromptPart)
    masked_content = list(masked_parts[0].content)
    assert masked_content[0] == "联系电话138********"
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
    assert fb_content[0]["msg"] == "号码138********不合法" and fb_content[0]["input"] == "138********"

    for raw, keys, expected in (
        ("tok123", ["LUCKIN_MCP_TOKEN"], {"LUCKIN_MCP_TOKEN": "tok123"}),
        ("A_KEY=1 B_KEY=2", ["A_KEY", "B_KEY"], {"A_KEY": "1", "B_KEY": "2"}),
        ("A_KEY=1", ["A_KEY", "B_KEY"], None),
        ("abc=", ["TOKEN"], {"TOKEN": "abc="}),
    ):
        assert _parse_bind_args(raw, keys) == expected

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

    monkeypatch.setattr(client_mod, "_CONNECT_TIMEOUT_S", 0.15)
    hang = MCPClient(name="hang", transport="stdio", command=sys.executable, args=["-c", "import time; time.sleep(60)"])
    with pytest.raises(TimeoutError, match="连接超时"):
        asyncio.run(hang.list_tools())


def test_evidence_block_groups_and_credential_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    """证据块：替别人查的观测、实时凭据状态、失效标记（§4.6）。"""
    from gsuid_core.ai_core.evidence import providers
    from gsuid_core.ai_core.kits.evidence import render_evidence_block
    from gsuid_core.ai_core.evidence.store import (
        LiveState,
        Observation,
        get_evidence_store,
        notify_credential_changed,
    )
    from gsuid_core.message_history.manager import MessageRecord

    class _StubProvider:
        def __init__(self, bound: set[str]) -> None:
            self.bound = bound

        async def current(self, service: str, bot_id: str, user_id: str) -> LiveState:
            return LiveState(
                service=service,
                user_id=user_id,
                aspect="credential",
                value="bound" if user_id in self.bound else "unbound",
            )

    sid = "test_evidence_sess_1"
    svc = "test_svc_1"
    bot = "test_bot_1"
    store = get_evidence_store(sid)
    store.observations.clear()
    store.invalidations.clear()
    stub = _StubProvider({"100000002"})
    monkeypatch.setattr(providers, "get_state_provider", lambda service: stub if service == svc else None)

    async def scenario() -> None:
        # 普通插件非凭据观测已在时间线带〔依据:…〕，证据块不重复转储
        store.append(
            Observation(
                obs_id="ob_plain01",
                session_id=sid,
                tool_name="plain_query",
                service="PlainPlugin",
                requested_by="100000001",
                requested_by_seq=2,
                credential_owner=None,
                outcome="data",
                observed_at=time.time() - 40,
                summary="普通公开查询",
                handle="",
            )
        )
        assert await render_evidence_block(sid, bot, target_uids=["100000002"], records=[]) == ""

        store.append(
            Observation(
                obs_id="ob_test01",
                session_id=sid,
                tool_name="mcp_test_svc_1_query",
                service=svc,
                requested_by="100000001",
                requested_by_seq=3,
                credential_owner="100000001",
                outcome="auth_required",
                observed_at=time.time() - 30,
                summary="需要绑定凭据",
                handle="",
            )
        )
        records = [
            MessageRecord(role="user", content="查一下", user_id="100000001", user_name="测试用户A"),
            MessageRecord(role="user", content="我也看看", user_id="100000002", user_name="测试用户B"),
        ]

        block = await render_evidence_block(sid, bot, target_uids=["100000002"], records=records)
        assert "（证据）" in block and "ob_test01" in block and "ob_plain01" not in block
        assert "替测试用户A查" in block and "已绑定" in block and "测试用户B" in block
        assert "缺凭据" in block

        notify_credential_changed(svc, bot, "100000001", session_id=sid)
        block2 = await render_evidence_block(sid, bot, target_uids=["100000002"], records=records)
        assert "已失效（凭据已变更）" in block2

        store.append(
            Observation(
                obs_id="ob_test02",
                session_id=sid,
                tool_name="mcp_test_svc_1_shared",
                service=svc,
                requested_by="100000002",
                requested_by_seq=4,
                credential_owner="shared",
                outcome="data",
                observed_at=time.time() - 5,
                summary="公开数据",
                handle="",
            )
        )
        block3 = await render_evidence_block(sid, bot, target_uids=["100000002"], records=records)
        assert "共享凭据" in block3 and "shared的凭据" not in block3

    try:
        asyncio.run(scenario())
    finally:
        store.observations.clear()
        store.invalidations.clear()


def test_static_safetensors_cache_and_local_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FastEmbed 静态模型（safetensors）缓存校验、mmap 零告警加载与 512 token 预算上限。"""
    import gc
    import struct
    import warnings

    import numpy as np
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    import gsuid_core.ai_core.rag.base as rag_base
    from gsuid_core.ai_core.configs.ai_config import local_embedding_config
    from gsuid_core.ai_core.rag.embedding.local import (
        LocalEmbeddingProvider,
        resolve_fastembed_model_spec,
    )

    model_name = "minishlab/potion-multilingual-128M"

    # 旧版 fastembed 缺少 static_embedding 子模块时，默认 ONNX 模型仍须正常解析不崩启动。
    with monkeypatch.context() as mp:
        mp.setitem(sys.modules, "fastembed.text.static_embedding", None)
        legacy_spec = resolve_fastembed_model_spec("BAAI/bge-small-en-v1.5")
        assert legacy_spec.is_static is False and "bge-small-en-v1.5" in legacy_spec.hf_repo

    cache_root = tmp_path / "models_cache"
    rev = "a" * 40
    repo_dir = cache_root / "models--minishlab--potion-multilingual-128M"
    snap = repo_dir / "snapshots" / rev
    refs = repo_dir / "refs"
    snap.mkdir(parents=True)
    refs.mkdir(parents=True)
    (refs / "main").write_text(rev, encoding="utf-8")
    (snap / "config.json").write_text(json.dumps({"max_position_embeddings": 8192}), encoding="utf-8")

    vocab = {"[UNK]": 0, "hello": 1, "world": 2, "test": 3}
    tok = Tokenizer(WordLevel(vocab=vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = Whitespace()
    tok.save(str(snap / "tokenizer.json"))

    monkeypatch.setattr(rag_base, "MODELS_CACHE", cache_root)
    monkeypatch.setattr(local_embedding_config.get_config("embedding_model_name"), "data", model_name)

    # 缺 model.safetensors 时缓存必须判无效
    assert rag_base._is_models_cache_valid() is False

    raw = np.ascontiguousarray(np.ones((4, 256), dtype=np.float32), dtype="<f4").tobytes()
    header = json.dumps({"embeddings": {"dtype": "F32", "shape": [4, 256], "data_offsets": [0, len(raw)]}}).encode(
        "utf-8"
    )
    (snap / "model.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + raw)
    assert rag_base._is_models_cache_valid() is True

    # 静态模型加载须彻底绕过 fastembed / onnxruntime（即使两者不可导入也正常工作）。
    with monkeypatch.context() as mp, warnings.catch_warnings():
        mp.setitem(sys.modules, "fastembed", None)
        mp.setitem(sys.modules, "onnxruntime", None)
        warnings.simplefilter("error", UserWarning)
        provider = LocalEmbeddingProvider(model_name=model_name, cache_dir=str(cache_root))
    try:
        assert provider.dimension == 256
        assert provider.max_input_tokens == 512
        vecs = provider.embed_sync(["hello world", "[UNK]"])
        assert len(vecs) == 2 and len(vecs[0]) == 256
        assert abs(sum(x * x for x in vecs[0]) - 1.0) < 1e-4
        assert all(x == 0.0 for x in vecs[1])
    finally:
        provider.close()
        del provider
        gc.collect()


def test_qdrant_circuit_breaker_and_search_tools_bm25_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """HttpQdrant 传输异常归一与熔断、search_tools 断连降级 BM25、知识分页边界。"""
    import httpx2
    from pydantic_ai.tools import Tool

    import gsuid_core.ai_core.rag.base as rag_base
    import gsuid_core.ai_core.rag.tools as rag_tools
    import gsuid_core.ai_core.rag.tool_lexical as tool_lexical
    from gsuid_core.ai_core.models import ToolBase, ToolContext
    from gsuid_core.ai_core.rag.qhttp import HttpQdrant, QdrantTransportError
    from gsuid_core.ai_core.rag.qmodels import CountResult, ScoredPoint
    from gsuid_core.ai_core.rag.knowledge import get_manual_knowledge_list

    # 1. HttpQdrant 连续 2 次连接拒绝后进入熔断，第 3 次不发真实 HTTP 请求直接快速失败
    calls = {"n": 0}
    mode = {"kind": "connect"}

    def _fail_transport(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if mode["kind"] == "read_timeout":
            raise httpx2.ReadTimeout("slow qdrant", request=request)
        raise httpx2.ConnectError("connection refused", request=request)

    async def _verify_breaker() -> None:
        mock_http = httpx2.AsyncClient(
            base_url="http://127.0.0.1:6333",
            transport=httpx2.MockTransport(_fail_transport),
        )
        client = HttpQdrant(url="http://127.0.0.1:6333", api_key=None, timeout=1.0, http=mock_http)
        try:
            assert client.is_circuit_open is False
            for _ in range(2):
                with pytest.raises(QdrantTransportError):
                    await client.collection_exists("demo_col")
            assert client.is_circuit_open is True and calls["n"] == 2
            with pytest.raises(QdrantTransportError, match="circuit open"):
                await client.collection_exists("demo_col")
            assert calls["n"] == 2
            client.reset_circuit_breaker()
            assert client.is_circuit_open is False
            mode["kind"] = "read_timeout"
            with pytest.raises(QdrantTransportError):
                await client.collection_exists("demo_col")
            assert client.is_circuit_open is False
        finally:
            await client.close()

    asyncio.run(_verify_breaker())

    # 2. search_tools 在 Qdrant 断连异常时降级走 tool_lexical BM25 召回并正确排序
    def _demo_calc() -> str:
        return "ok"

    demo_tool: Tool[ToolContext] = Tool(_demo_calc, name="demo_ascension_calculator", takes_ctx=False)
    other_tool: Tool[ToolContext] = Tool(_demo_calc, name="demo_image_generator", takes_ctx=False)
    fake_tb = ToolBase(
        name="demo_ascension_calculator",
        tool=demo_tool,
        description="计算角色突破材料缺口与采集路线",
        plugin="DemoPlugin",
        covers=["突破材料", "升级缺口", "ascension gap"],
        aliases=["材料计算器"],
    )
    other_tb = ToolBase(
        name="demo_image_generator",
        tool=other_tool,
        description="生成海报与插画图片",
        plugin="DemoPlugin",
        covers=["文生图"],
        aliases=["绘图工具"],
    )
    pool = {"demo_ascension_calculator": fake_tb, "demo_image_generator": other_tb}
    monkeypatch.setattr(tool_lexical, "_INDEX", tool_lexical.ToolBM25Index())
    tool_lexical.rebuild_index(pool)

    class _DeadEmbedder:
        async def aembed_query(self, _text: str) -> list[float]:
            return [0.1, 0.2]

    class _DeadQdrant:
        async def query_points(self, **_kw: object) -> object:
            raise QdrantTransportError("qdrant down")

    monkeypatch.setattr(rag_base, "embedding_model", _DeadEmbedder())
    monkeypatch.setattr(rag_base, "client", _DeadQdrant())
    monkeypatch.setattr(rag_tools, "get_all_tools", lambda: pool)
    monkeypatch.setattr(rag_tools, "get_registered_tools", lambda: {"common": pool})

    for query in ("角色突破材料缺口", "ascension gap"):
        hits = asyncio.run(rag_tools.search_tools(query, limit=5, rerank=False))
        assert hits and hits[0].name == "demo_ascension_calculator"

    # 3. 知识分页：offset+limit 恰为 100 整数倍且仍有剩余时不得过早返回 next_offset=None
    class _ScrollQdrant:
        def __init__(self, total: int) -> None:
            self._rows = [
                ScoredPoint(id=str(i), version=0, score=1.0, payload={"id": str(i), "title": f"条{i}"})
                for i in range(total)
            ]

        async def count(self, **_kw: object) -> CountResult:
            return CountResult(count=len(self._rows))

        async def scroll(
            self,
            *,
            limit: int = 100,
            offset: object = None,
            **_kw: object,
        ) -> tuple[list[ScoredPoint], int | None]:
            start = offset if isinstance(offset, int) else 0
            chunk = self._rows[start : start + limit]
            nxt = start + len(chunk) if start + len(chunk) < len(self._rows) else None
            return chunk, nxt

    for total, offset, limit, expected_len, expected_next in (
        (150, 80, 20, 20, 100),
        (150, 140, 20, 10, None),
        (100, 80, 20, 20, None),
    ):
        monkeypatch.setattr(rag_base, "client", _ScrollQdrant(total))
        page = asyncio.run(get_manual_knowledge_list(source_filter="all", offset=offset, limit=limit))
        assert len(page["list"]) == expected_len and page["next_offset"] == expected_next


def test_cognition_rendering_and_hub_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """认知渲染强弱分层与世界枢纽筛选：直提专名优先展示，其余按顺序落入 extra_titles。"""
    import gsuid_core.ai_core.cognition.hub as hub_mod
    from gsuid_core.ai_core.entity_index import clear_entity_index
    from gsuid_core.ai_core.cognition.nodes import AICogNode
    from gsuid_core.ai_core.cognition.types import CogKind, CogScope, CognitiveHit
    from gsuid_core.ai_core.cognition.facade import render_cognition_block
    from gsuid_core.ai_core.buildin_tools.rag_search import line_pages

    weak_ep = CognitiveHit(
        kind=CogKind.EPISODE,
        id="ep_1",
        title="",
        summary="Commuted by subway to work",
        score=0.4,
        high_confidence=False,
    )
    strong_fact = CognitiveHit(
        kind=CogKind.FACT,
        id="fact_1",
        title="",
        summary="用户偏好地铁通勤",
        score=0.8,
        high_confidence=True,
    )
    weak_rec = CognitiveHit(
        kind=CogKind.RECORD,
        id="rec_1",
        title="",
        summary="无关记录",
        score=0.1,
        high_confidence=False,
    )
    pages = line_pages([strong_fact, weak_ep, weak_rec])
    flat_ids = [h.id for page in pages for h in page]
    assert "ep_1" in flat_ids and "fact_1" in flat_ids and "rec_1" not in flat_ids
    rendered_mixed = render_cognition_block("怎么上班", [strong_fact, weak_ep, weak_rec])
    assert "Commuted by subway to work" in rendered_mixed
    assert "用户偏好地铁通勤" in rendered_mixed
    assert "另有 1 条弱相关" in rendered_mixed
    rendered_only_weak = render_cognition_block("怎么上班", [weak_ep])
    assert "Commuted by subway to work" in rendered_only_weak
    assert "弱相关" not in rendered_only_weak

    async def scenario() -> None:
        clear_entity_index()
        try:
            async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="cog-hub.db"):
                id1 = await AICogNode.upsert(kind=CogKind.ENTITY, ref="world:demo:h1", title="测试专名实体")
                id2 = await AICogNode.upsert(kind=CogKind.ENTITY, ref="world:demo:h2", title="测试词条二")
                id3 = await AICogNode.upsert(kind=CogKind.ENTITY, ref="world:demo:h3", title="测试词条三")
                id4 = await AICogNode.upsert(kind=CogKind.ENTITY, ref="world:demo:h4", title="测试词条四")
                assert id1 is not None and id2 is not None and id3 is not None and id4 is not None

                hits = [
                    CognitiveHit(kind=CogKind.ENTITY, id=f"node_{id2}", title="测试词条二", summary="", score=0.9),
                    CognitiveHit(kind=CogKind.ENTITY, id=f"node_{id3}", title="测试词条三", summary="", score=0.8),
                    CognitiveHit(kind=CogKind.ENTITY, id=f"node_{id4}", title="测试词条四", summary="", score=0.7),
                ]
                shown, extra_titles = await hub_mod._hubs_from_hits(
                    hits, "测试专名实体", scope=CogScope(group_id="", user_id="")
                )
                assert [n.title for n in shown] == ["测试专名实体", "测试词条二"]
                assert extra_titles == ["测试词条三", "测试词条四"]
        finally:
            clear_entity_index()

    asyncio.run(scenario())
