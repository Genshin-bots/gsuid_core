from unittest.mock import AsyncMock, patch

import pytest

from gsuid_core.ai_core.mcp.client import (
    _RESOURCE_HANDLE_RE,
    MCPClient,
    _materialize_resource_args,
    _materialize_resource_handle,
)
from gsuid_core.utils.resource_manager import RM

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def test_resource_handle_regex_case_insensitivity():
    """验证句柄正则对大小写十六进制与扩展名的兼容性。"""
    assert _RESOURCE_HANDLE_RE.match("img_0123456789abcdef")
    assert _RESOURCE_HANDLE_RE.match("img_0123456789ABCDEF")
    assert _RESOURCE_HANDLE_RE.match("img_0123456789abcdef.png")
    assert _RESOURCE_HANDLE_RE.match("img_0123456789abcdef.PNG")
    assert _RESOURCE_HANDLE_RE.match("img_0123456789abcdef.JpG")
    assert _RESOURCE_HANDLE_RE.match("res_0123456789ab")
    assert _RESOURCE_HANDLE_RE.match("res_0123456789AB.webp")

    # 非句柄格式
    assert not _RESOURCE_HANDLE_RE.match("img_short")
    assert not _RESOURCE_HANDLE_RE.match("/tmp/img_0123456789abcdef.png")
    assert not _RESOURCE_HANDLE_RE.match("https://example.com/img_0123456789abcdef.png")


@pytest.mark.asyncio
async def test_materialize_resource_handle_success():
    """验证 RM 中有效图片句柄成功物化为 data URI，且支持大写后缀剥离。"""
    rid = RM.register(PNG_BYTES)

    # 裸句柄
    uri = await _materialize_resource_handle(rid)
    assert uri.startswith("data:image/png;base64,")

    # 带大写扩展名
    uri_upper = await _materialize_resource_handle(f"{rid}.PNG")
    assert uri_upper.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_materialize_resource_handle_unresolved(caplog):
    """验证不存在的句柄平滑原样放行并记录 warning。"""
    non_existent = "img_0000111122223333"
    result = await _materialize_resource_handle(non_existent)
    assert result == non_existent
    assert "resource_handle_unresolved" in caplog.text or "解析失败" in caplog.text or non_existent in caplog.text


@pytest.mark.asyncio
async def test_materialize_resource_args_recursive():
    """验证参数结构体中嵌套的句柄递归物化。"""
    rid = RM.register(PNG_BYTES)
    args = {
        "prompt": "generate video",
        "first_frame": f"{rid}.png",
        "frames": [rid, "regular_string"],
        "count": 1,
        "flag": True,
    }

    materialized = await _materialize_resource_args(args)
    assert isinstance(materialized, dict)
    assert materialized["prompt"] == "generate video"
    assert str(materialized["first_frame"]).startswith("data:image/png;base64,")
    assert isinstance(materialized["frames"], list)
    assert str(materialized["frames"][0]).startswith("data:image/png;base64,")
    assert materialized["frames"][1] == "regular_string"
    assert materialized["count"] == 1
    assert materialized["flag"] is True


@pytest.mark.asyncio
async def test_call_tool_materializes_only_for_stdio():
    """验证只有 StdioTransport 下才会物化句柄，SSETransport 保持原句柄。"""
    rid = RM.register(PNG_BYTES)

    # 测试 Stdio 传输
    stdio_client = MCPClient(
        name="test_stdio",
        command="python",
        args=["-m", "demo"],
        transport="stdio",
    )
    mock_stdio_result = AsyncMock()
    mock_stdio_result.content = []
    mock_stdio_result.is_error = False

    with patch("gsuid_core.ai_core.mcp.client.Client") as mock_client_cls:
        mock_instance = AsyncMock()
        mock_instance.call_tool = AsyncMock(return_value=mock_stdio_result)
        mock_client_cls.return_value = mock_instance
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None

        await stdio_client.call_tool("gen_video", {"image": rid})

        # 验证发送给 client.call_tool 的参数已经被物化为 data URI
        call_args = mock_instance.call_tool.call_args[1]
        assert call_args["arguments"]["image"].startswith("data:image/png;base64,")

    # 测试 SSE 传输
    sse_client = MCPClient(
        name="test_sse",
        url="http://localhost:8000/sse",
        transport="sse",
    )
    mock_sse_result = AsyncMock()
    mock_sse_result.content = []
    mock_sse_result.is_error = False

    with patch("gsuid_core.ai_core.mcp.client.Client") as mock_client_cls:
        mock_instance = AsyncMock()
        mock_instance.call_tool = AsyncMock(return_value=mock_sse_result)
        mock_client_cls.return_value = mock_instance
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None

        await sse_client.call_tool("gen_video", {"image": rid})

        # 验证发送给 client.call_tool 的参数依然是原始句柄
        call_args = mock_instance.call_tool.call_args[1]
        assert call_args["arguments"]["image"] == rid


def test_prepare_source_for_mcp_with_handle():
    """验证 prepare_source_for_mcp 支持将 img_ 句柄落地为真实临时文件。"""
    import asyncio
    from pathlib import Path

    from gsuid_core.ai_core.mcp.utils import cleanup_tempfile, prepare_source_for_mcp

    rid = RM.register(PNG_BYTES)
    path = asyncio.run(prepare_source_for_mcp(f"{rid}.png", "🖼️ [Test]"))
    try:
        p = Path(path)
        assert p.is_file()
        assert path.endswith(".png")
        assert p.read_bytes() == PNG_BYTES
    finally:
        cleanup_tempfile(path, "🖼️ [Test]")

    non_existent = "img_9999999999999999"
    unresolved = asyncio.run(prepare_source_for_mcp(non_existent))
    assert unresolved == non_existent
