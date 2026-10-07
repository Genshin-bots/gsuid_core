"""MCP Server 出图返回链路回归。

钉住两处对外契约：
1. handler 交给 fastmcp 的返回注解必须留在 ``str``。报成 ``ToolResult`` 会让 fastmcp 判定不可序列化，
   把**所有** MCP 工具的 outputSchema / structuredContent 一起抽掉（纯文本工具也受害）。
2. ``ToolContext.extra`` 必须全量带出 AccessToken.claims 与 HTTP 会话头，
   插件经 ``register_mcp_token_verifier`` 放的自定义 claim 靠这里进入工具。
"""

from __future__ import annotations

import base64
import asyncio
from typing import Dict, List, Union
from collections.abc import Callable, Awaitable

import httpx
import pytest
from mcp.types import TextContent, ImageContent
from pydantic_ai import RunContext, ToolReturn
from fastmcp.tools import Tool
from pydantic_ai.tools import Tool as AiTool
from fastmcp.tools.base import ToolResult
from pydantic_ai.messages import BinaryContent

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event, Message
from gsuid_core.ai_core import trigger_bridge
from gsuid_core.ai_core.mcp import server as mcp_server
from gsuid_core.ai_core.models import ToolBase, ToolContext

McpToolFn = Callable[[RunContext[ToolContext]], Awaitable[str | ToolReturn[str | None]]]

PNG = b"\x89PNG\r\n\x1a\n" + b"probe-png-body"
JPEG = b"\xff\xd8\xff\xe0" + b"probe-jpeg"
GIF = b"GIF89a" + b"probe-gif"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"probe-webp"
PDF = b"%PDF-1.7\n<html>not an image</html>"


class _RmStub:
    """RM 替身：只保留本次用例登记的字节，避免污染全局 30 分钟 TTL 存储。"""

    def __init__(self) -> None:
        self._store: Dict[str, Union[bytes, str]] = {}

    def register(self, data: Union[str, bytes]) -> str:
        rid = f"img_probe{len(self._store):04d}"
        self._store[rid] = data
        return rid

    def peek(self, resource_id: str) -> Union[bytes, str]:
        if resource_id not in self._store:
            raise ValueError(resource_id)
        return self._store[resource_id]

    async def get(self, resource_id: str) -> bytes:
        data = self.peek(resource_id)
        if isinstance(data, bytes):
            return data
        if data.startswith("base64://"):
            return base64.b64decode(data[len("base64://") :])
        raise AssertionError(f"用例未预期的资源形态: {data[:32]}")


def _tool_base(name: str, fn: McpToolFn) -> ToolBase:
    base = ToolBase.__new__(ToolBase)
    base.name = name
    base.description = name
    base.plugin = "test"
    base.tool = AiTool(fn, takes_ctx=True, name=name, description=name)
    return base


async def _call(fn: McpToolFn, name: str) -> Union[ToolResult, str]:
    handler = mcp_server._build_ai_tool_handler(_tool_base(name, fn), "common")
    return await handler()


async def _send(ctx: RunContext[ToolContext], payload: Union[Message, bytes, str]) -> None:
    """测试侧取 bot 的统一入口：ToolContext.bot 是 Optional，按契约断言一次。"""
    bot = ctx.deps.bot
    assert bot is not None
    await bot.send(payload)


@pytest.fixture
def rm_stub(monkeypatch: pytest.MonkeyPatch) -> _RmStub:
    stub = _RmStub()
    monkeypatch.setattr(mcp_server.RM, "register", stub.register)
    monkeypatch.setattr(mcp_server.RM, "peek", stub.peek)
    monkeypatch.setattr(mcp_server.RM, "get", stub.get)
    return stub


def _result_text(result: Union[ToolResult, str]) -> str:
    if isinstance(result, str):
        return result
    chunks: List[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            chunks.append(block.text)
    return "\n".join(chunks)


def _kinds(result: Union[ToolResult, str]) -> List[str]:
    if isinstance(result, str):
        return ["<str>"]
    return [type(c).__name__ for c in result.content]


# ── 魔数嗅探 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, "png"), (JPEG, "jpeg"), (GIF, "gif"), (WEBP, "webp"), (PDF, None), (b"", None)],
)
def test_sniff_image_format(data: bytes, expected: Union[str, None]) -> None:
    assert mcp_server._sniff_image_format(data) == expected


def test_extract_treats_http_url_as_image_ref(rm_stub: _RmStub) -> None:
    texts, ids = mcp_server._extract_messages_for_mcp("https://example.com/a.png")
    assert ids and not texts


def test_extract_keeps_plain_text_out_of_image_bucket(rm_stub: _RmStub) -> None:
    texts, ids = mcp_server._extract_messages_for_mcp("就是一段说明文字")
    assert texts == ["就是一段说明文字"]
    assert not ids


def test_extract_converts_markdown_and_at_to_text(rm_stub: _RmStub) -> None:
    texts, ids = mcp_server._extract_messages_for_mcp([Message("markdown", "**标题**"), Message("at", "u_1")])
    assert texts == ["**标题**", "@u_1"]
    assert not ids


# ── 契约一：交给 fastmcp 的返回注解留在 str，outputSchema 不被抽掉 ─────────────


def test_handler_signature_reports_str_to_fastmcp() -> None:
    async def _noop(ctx: RunContext[ToolContext]) -> str:  # pragma: no cover - 只看签名
        return "x"

    handler = mcp_server._build_ai_tool_handler(_tool_base("probe_annot", _noop), "common")
    assert handler.__signature__.return_annotation is str
    assert handler.__annotations__["return"] is str


def test_text_tool_keeps_output_schema_and_structured_content() -> None:
    """纯文本工具的对外契约不能因为支持出图而改变。"""

    async def _text(ctx: RunContext[ToolContext]) -> str:
        return "文本结果"

    async def run() -> Dict[str, object]:
        handler = mcp_server._build_ai_tool_handler(_tool_base("probe_text", _text), "common")
        tool = Tool.from_function(handler, name="probe_text")
        result = await tool.run({})
        return {
            "output_schema": tool.output_schema,
            "structured": result.structured_content,
            "texts": _result_text(result).split("\n") if _result_text(result) else [],
        }

    got = asyncio.run(run())
    assert got["output_schema"] is not None
    assert got["structured"] == {"result": "文本结果"}
    assert got["texts"] == ["文本结果"]


# ── 契约二：claims / 会话头全量进入 extra ─────────────────────────────────────


def test_custom_claims_reach_tool_context_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_identity_from_access_token",
        lambda: {"auth": "token", "user_id": "u_1", "user_pm": 5, "tenant": "acme"},
    )
    monkeypatch.setattr(mcp_server, "_http_session_overrides", lambda: {"X-Tenant": "acme"})

    ctx = mcp_server._build_run_context("probe_claims")

    assert ctx.deps.extra["tenant"] == "acme"
    assert ctx.deps.extra["X-Tenant"] == "acme"
    assert ctx.deps.extra["auth"] == "token"
    assert ctx.deps.extra["source"] == "mcp_server"
    assert ctx.deps.extra["mcp_image_ids"] == []
    assert ctx.deps.extra["mcp_texts"] == []


# ── 出图链路 ──────────────────────────────────────────────────────────────────


def test_bot_send_png_returns_image_content(rm_stub: _RmStub) -> None:
    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, PNG)
        return "画好了"

    out = asyncio.run(_call(_draw, "probe_png"))
    assert _kinds(out) == ["TextContent", "ImageContent"]
    assert isinstance(out, ToolResult)
    assert out.structured_content == {"result": "画好了"}


def test_bot_send_base64_returns_image_content(rm_stub: _RmStub) -> None:
    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, "base64://" + base64.b64encode(PNG).decode())
        return "ok"

    out = asyncio.run(_call(_draw, "probe_b64"))
    assert "ImageContent" in _kinds(out)


def test_binary_content_is_forwarded_as_image(rm_stub: _RmStub) -> None:
    async def _draw(ctx: RunContext[ToolContext]) -> ToolReturn[str | None]:
        return ToolReturn(return_value="完成", content=[BinaryContent(data=JPEG, media_type="image/jpeg")])

    out = asyncio.run(_call(_draw, "probe_binary"))
    assert _kinds(out) == ["TextContent", "ImageContent"]


def test_non_image_binary_is_announced_not_silently_dropped(rm_stub: _RmStub) -> None:
    async def _pdf(ctx: RunContext[ToolContext]) -> ToolReturn[str | None]:
        return ToolReturn(return_value=None, content=[BinaryContent(data=PDF, media_type="application/pdf")])

    out = asyncio.run(_call(_pdf, "probe_pdf"))
    assert isinstance(out, str)
    assert "非图片二进制未附带" in out


def test_image_count_over_quota_is_announced(rm_stub: _RmStub) -> None:
    async def _many(ctx: RunContext[ToolContext]) -> str:
        for _ in range(mcp_server.MAX_MCP_IMAGES + 2):
            await _send(ctx, PNG)
        return "多图"

    out = asyncio.run(_call(_many, "probe_many"))
    assert isinstance(out, ToolResult)
    assert _kinds(out).count("ImageContent") == mcp_server.MAX_MCP_IMAGES
    assert "数量上限" in str(out.structured_content)


def test_oversized_image_is_skipped_with_note(rm_stub: _RmStub) -> None:
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (mcp_server.MAX_MCP_IMAGE_BYTES + 1)

    async def _big(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, big)
        return "大图"

    out = asyncio.run(_call(_big, "probe_big"))
    assert isinstance(out, str)
    assert "图片过大未附带" in out


def test_failed_tool_still_returns_images_collected_before_error(rm_stub: _RmStub) -> None:
    async def _draw_then_boom(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, PNG)
        raise RuntimeError("后半段炸了")

    out = asyncio.run(_call(_draw_then_boom, "probe_boom"))
    assert "ImageContent" in _kinds(out)
    assert isinstance(out, ToolResult)
    assert out.structured_content is not None
    assert "执行异常" in str(out.structured_content["result"])


class _UrlFetch:
    def __init__(self, payload: bytes) -> None:
        self.urls: List[str] = []
        self._payload = payload

    async def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return self._payload


class _ForbidFetch:
    async def __call__(self, url: str) -> bytes:
        raise AssertionError(url)


class _TimeoutFetch:
    async def __call__(self, url: str) -> bytes:
        raise TimeoutError(url)


def _image_payloads(result: Union[ToolResult, str]) -> List[bytes]:
    if not isinstance(result, ToolResult):
        return []
    found: List[bytes] = []
    for block in result.content:
        if isinstance(block, ImageContent):
            found.append(base64.b64decode(block.data))
    return found


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]/a.png",
    ],
)
def test_refused_image_host_stays_in_text_and_is_not_fetched(
    rm_stub: _RmStub, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(mcp_server, "_fetch_mcp_image", _ForbidFetch())

    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, PNG)
        await _send(ctx, url)
        return "正文"

    out = asyncio.run(_call(_draw, "probe_deny"))
    assert isinstance(out, ToolResult)
    text = _result_text(out)
    assert "正文" in text
    assert url in text
    assert "取回失败未附带" in text
    assert _image_payloads(out) == [PNG]


def test_private_lan_image_is_fetched(rm_stub: _RmStub, monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = _UrlFetch(PNG)
    monkeypatch.setattr(mcp_server, "_fetch_mcp_image", fetch)
    url = "http://192.168.1.10/a.png"

    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, url)
        return "内网图"

    out = asyncio.run(_call(_draw, "probe_lan"))
    assert fetch.urls == [url]
    assert _result_text(out).split("\n")[0] == "内网图"
    assert _image_payloads(out) == [PNG]


def test_fetch_timeout_keeps_earlier_image_and_url(rm_stub: _RmStub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_fetch_mcp_image", _TimeoutFetch())
    url = "https://slow.example/a.png"

    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, PNG)
        await _send(ctx, url)
        return "还在"

    out = asyncio.run(_call(_draw, "probe_timeout"))
    assert isinstance(out, ToolResult)
    text = _result_text(out)
    assert "还在" in text
    assert url in text
    assert "取回失败未附带" in text
    assert _image_payloads(out) == [PNG]


def test_image_over_quota_does_not_fetch_the_next_url(rm_stub: _RmStub, monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = _UrlFetch(PNG)
    monkeypatch.setattr(mcp_server, "_fetch_mcp_image", fetch)
    url = "https://cdn.example/fifth.png"

    async def _many(ctx: RunContext[ToolContext]) -> str:
        for _ in range(mcp_server.MAX_MCP_IMAGES):
            await _send(ctx, PNG)
        await _send(ctx, url)
        return "满了"

    out = asyncio.run(_call(_many, "probe_cap_fetch"))
    assert fetch.urls == []
    assert isinstance(out, ToolResult)
    assert len(_image_payloads(out)) == mcp_server.MAX_MCP_IMAGES
    assert "数量上限" in _result_text(out)
    assert url not in _result_text(out)


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/secret.png",
        "http://169.254.169.254/latest/meta-data",
        "file:///C:/secret-mcp.png",
    ],
)
def test_redirect_to_blocked_target_stops_before_the_next_request(
    rm_stub: _RmStub, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*_args: object, **kwargs: object) -> httpx.AsyncClient:
        timeout = kwargs["timeout"] if "timeout" in kwargs else None
        follow = kwargs["follow_redirects"] if "follow_redirects" in kwargs else False
        if not isinstance(timeout, httpx.Timeout) or not isinstance(follow, bool):
            raise AssertionError("MCP 图片客户端参数不符合预期")
        return real_client(timeout=timeout, follow_redirects=follow, transport=transport)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", factory)

    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, PNG)
        await _send(ctx, "https://cdn.example/a.png")
        return "说明"

    out = asyncio.run(_call(_draw, "probe_redirect"))
    assert seen == ["https://cdn.example/a.png"]
    assert isinstance(out, ToolResult)
    text = _result_text(out)
    assert "说明" in text
    assert location in text
    assert _image_payloads(out) == [PNG]


def test_webp_data_uri_roundtrips_to_image_bytes(rm_stub: _RmStub) -> None:
    uri = "data:image/webp;base64," + base64.b64encode(WEBP).decode()

    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, uri)
        return "webp"

    out = asyncio.run(_call(_draw, "probe_webp_data"))
    assert _image_payloads(out) == [WEBP]
    assert uri not in _result_text(out)


def test_non_image_data_uri_does_not_echo_the_payload(rm_stub: _RmStub) -> None:
    uri = "data:image/png;base64," + base64.b64encode(PDF).decode()

    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, uri)
        return "不是图"

    out = asyncio.run(_call(_draw, "probe_data_pdf"))
    assert isinstance(out, str)
    assert "不是图" in out
    assert "非图片二进制未附带" in out
    assert uri not in out
    assert base64.b64encode(PDF).decode() not in out


def test_duplicate_send_text_appears_once_and_distinct_text_stays(rm_stub: _RmStub) -> None:
    async def _same(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, "同一句")
        return "同一句"

    assert asyncio.run(_call(_same, "probe_same")) == "同一句"

    async def _diff(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, "旁白")
        return "结果"

    assert asyncio.run(_call(_diff, "probe_diff")) == "旁白\n结果"


def test_plain_file_uri_stays_text(rm_stub: _RmStub) -> None:
    uri = "file:///C:/secret-mcp.png"

    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, uri)
        return "ok"

    assert asyncio.run(_call(_draw, "probe_file_text")) == f"{uri}\nok"


def test_file_uri_image_message_is_not_attached(rm_stub: _RmStub) -> None:
    uri = "file:///C:/no-such-mcp-image.png"

    async def _draw(ctx: RunContext[ToolContext]) -> str:
        await _send(ctx, Message("image", uri))
        return "文字还在"

    out = asyncio.run(_call(_draw, "probe_file_msg"))
    assert isinstance(out, str)
    assert uri in out
    assert "文字还在" in out
    assert "取回失败未附带" in out


class _BridgeRm:
    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        rid = f"{prefix}{self._n:04d}"
        self._n += 1
        return rid

    def register(self, data: Union[str, bytes]) -> str:
        return self._next("img_")

    def register_audio(self, data: Union[str, bytes]) -> str:
        return self._next("aud_")

    def register_video(self, data: Union[str, bytes]) -> str:
        return self._next("vid_")


def test_trigger_output_splits_mcp_notes_from_chat_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _BridgeRm()
    monkeypatch.setattr(trigger_bridge.RM, "register", stub.register)
    monkeypatch.setattr(trigger_bridge.RM, "register_audio", stub.register_audio)
    monkeypatch.setattr(trigger_bridge.RM, "register_video", stub.register_video)

    async def _cmd(bot: trigger_bridge.MockBot, ev: Event) -> None:
        await bot.send(PNG)
        await bot.send(Message("record", b"aud"))
        await bot.send(Message("video", b"vid"))
        await bot.send("说明")

    ev = Event()
    ev.user_id = "u_probe"
    ev.bot_id = "bot_probe"
    ev.user_type = "direct"
    real = Bot(_Bot("bot_probe"), ev)
    mcp_extra: Dict[str, object] = {"source": "mcp_server"}
    mcp_text = asyncio.run(trigger_bridge.run_trigger_via_mockbot(real, ev, _cmd, mcp_extra))
    assert "说明" in mcp_text
    assert "send_message_by_ai" not in mcp_text
    assert "资源ID" not in mcp_text
    assert "未附带音频字节" in mcp_text
    assert "未附带视频字节" in mcp_text
    ids = mcp_extra["mcp_image_ids"]
    assert isinstance(ids, list)
    assert len(ids) == 1
    assert isinstance(ids[0], str)
    assert ids[0] not in mcp_text

    chat_extra: Dict[str, object] = {"source": "chat"}
    chat_text = asyncio.run(trigger_bridge.run_trigger_via_mockbot(real, ev, _cmd, chat_extra))
    assert "send_message_by_ai" in chat_text
    assert "资源ID" in chat_text
    assert "mcp_image_ids" not in chat_extra
