"""
MCP Server 模块 — 将框架的 to_ai 触发器对外暴露为 MCP 服务

启用后，外部 MCP 客户端（如 Claude Desktop、Cursor 等）可通过 SSE 或 stdio 协议
连接到本框架，调用所有注册了 `to_ai` 参数的触发器函数。

架构:
1. 框架启动时，所有带 `to_ai` 的触发器已注册到 `_MCP_TRIGGER_REGISTRY`
2. 本模块读取注册表，为每个触发器创建对应的 MCP Tool
3. 使用 fastmcp.FastMCP 创建 MCP Server
4. 根据配置选择 SSE（HTTP）或 stdio 传输协议启动服务

配置项（独立的 MCP Server 子配置 `MCP_SERVER_CONFIG`，存储于
`data/ai_core/mcp_server_config.json`，调用入口为 `mcp_server_config`）:
- enable_mcp_server: 是否启用 MCP Server（默认 False）
- mcp_server_transport: 传输协议 "sse" | "stdio"（默认 "sse"）
- mcp_server_port: SSE 监听端口（默认 8766），监听地址复用框架 HOST 配置
- mcp_server_api_key: Bearer Token 认证密钥（留空则不启用认证）
"""

import asyncio
import contextlib
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, AuthProvider
from fastmcp.utilities.types import Image

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.server import on_core_shutdown
from gsuid_core.ai_core.trigger_bridge import (
    _AI_CALL_CONTEXT,
    _MCP_TRIGGER_REGISTRY,
    MockBot,
)
from gsuid_core.utils.resource_manager import RM


def _sniff_image_format(data: bytes) -> str:
    """从图片二进制的 magic bytes 推断格式，未知则默认 png。

    供 MCP Server 把触发器产出的图片转成 MCP 图片 content 时填充 mime 用。
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return "png"


class BearerTokenAuth(AuthProvider):
    """简单的 Bearer Token 认证提供者。

    通过配置的 API Key 验证请求中的 Bearer Token。
    如果 api_key 为空字符串，则所有请求都通过（不启用认证）。
    """

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self._api_key = api_key

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """验证 Bearer Token。

        Args:
            token: 请求中的 Token 字符串

        Returns:
            AccessToken 如果验证通过，None 如果验证失败
        """
        if not self._api_key:
            # 未配置 API Key，所有请求通过
            return AccessToken(
                token=token,
                client_id="mcp_client",
                scopes=["mcp:tools"],
            )

        if token == self._api_key:
            return AccessToken(
                token=token,
                client_id="mcp_client",
                scopes=["mcp:tools"],
            )

        logger.warning(t("🌐 [MCP Server] Bearer Token 验证失败"))
        return None


# 全局 MCP Server 实例
_mcp_server: Optional[FastMCP] = None
# 后台运行的 server 任务
_server_task: Optional[asyncio.Task] = None


def _create_mock_event(text: str, command: str, user_pm: int = 0) -> Event:
    """创建一个模拟的 Event 对象，用于 MCP 工具调用触发器。

    Args:
        text: 用户输入文本（触发器的参数）
        command: 触发器命令关键字
        user_pm: 权限等级，默认 0（master），MCP 调用拥有最高权限

    Returns:
        模拟的 Event 对象
    """
    ev = Event()
    ev.text = text
    ev.command = command
    ev.raw_text = f"{command} {text}".strip()
    ev.user_pm = user_pm
    ev.user_id = "mcp_client"
    ev.bot_id = "MCP"
    ev.bot_self_id = "MCP_Server"
    ev.user_type = "direct"
    return ev


def _create_mock_bot() -> Bot:
    """创建一个模拟的 Bot 对象，用于 MCP 工具调用触发器。

    返回一个 Bot 实例（高层包装器），其 send 方法会被 MockBot 拦截。
    Bot 是高层包装器，包装 _Bot + Event，供插件/触发器使用；
    _Bot 是底层实现，管理 WebSocket 连接和消息队列。
    """
    from gsuid_core.bot import _Bot

    _bot = _Bot("MCP_Server")
    mock_ev = _create_mock_event("", "")
    return Bot(_bot, mock_ev)


def _build_mcp_tool_handler(
    tool_name: str,
    trigger_info: Dict[str, Any],
):
    """为单个触发器创建 MCP Tool 处理函数。

    Args:
        tool_name: 工具名称（即触发器函数名）
        trigger_info: 触发器注册信息

    Returns:
        异步处理函数，接受 text 参数并返回执行结果
    """
    func = trigger_info["func"]
    primary_keyword = trigger_info["primary_keyword"]
    trigger_type = trigger_info["trigger_type"]
    to_ai_doc = trigger_info["to_ai_doc"]

    async def handler(text: str = "", image_id: str = "", audio_id: str = ""):
        """MCP 工具处理函数：调用触发器并返回结果。

        Args:
            text: 传递给触发器的文本参数
            image_id: 可选，参考图片的资源ID
            audio_id: 可选，参考音频的资源ID

        Returns:
            纯文本（str），或在触发器产出图片时返回 文本 + 图片 的 content 列表
            （FastMCP 会分别转成 TextContent / ImageContent 回传给外部客户端）。
        """
        import re

        # 创建模拟对象
        mock_bot = _create_mock_bot()
        fake_ev = _create_mock_event(text, primary_keyword)

        # 允许 MCP 客户端传入资源 ID
        if image_id:
            fake_ev.image_id = image_id
        if audio_id:
            fake_ev.audio_id = audio_id

        # 如果触发器类型是 regex，模拟 regex 匹配
        if trigger_type == "regex":
            match = re.search(primary_keyword, text)
            if match:
                fake_ev.regex_dict = match.groupdict()
                fake_ev.regex_group = match.groups()
                fake_ev.command = "|".join(g if g is not None else "" for g in match.groups())
            else:
                fake_ev.regex_dict = {}
                fake_ev.regex_group = ()
                fake_ev.command = text

        # 准备收集上下文
        call_ctx: Dict[str, Any] = {
            "texts": [],
            "image_ids": [],
            "audio_ids": [],
            "video_ids": [],
            "bot_messages": [],
        }

        token = _AI_CALL_CONTEXT.set(call_ctx)
        mock = MockBot(mock_bot, call_ctx)

        try:
            logger.info(
                t(
                    "🌐 [MCP Server] 调用触发器 [{primary_keyword}], text={text}",
                    primary_keyword=primary_keyword,
                    text=repr(text),
                )
            )
            await func(mock, fake_ev)
        except Exception as e:
            logger.error(
                t("🌐 [MCP Server] 触发器 [{primary_keyword}] 执行异常: {e}", primary_keyword=primary_keyword, e=e)
            )
            return f"❌ 触发器执行异常: {e}"
        finally:
            _AI_CALL_CONTEXT.reset(token)

        # 组装返回值
        # 文本部分：ai_return() 写入的文字 + bot.send 拦截到的文字
        text_parts: List[str] = []
        text_parts.extend(call_ctx["texts"])
        text_parts.extend(call_ctx["bot_messages"])

        # 图片部分：从 RM 取回二进制，转成 MCP 图片 content，外部客户端即可直接收到图片。
        # 取回失败（如已过 TTL / 解码失败）则退回文字描述，绝不让单张图把整次调用炸掉。
        image_blocks: List[Image] = []
        for rid in call_ctx["image_ids"]:
            try:
                data = await RM.get(rid)
                image_blocks.append(Image(data=data, format=_sniff_image_format(data)))
            except Exception as e:
                logger.warning(t("🌐 [MCP Server] 取回图片资源 {rid} 失败，退回文字描述: {e}", rid=rid, e=e))
                text_parts.append(f"[图片资源 {rid} 取回失败: {e}]")

        # 音频/视频：MCP 客户端对其支持参差，暂仍以文字描述 + 资源ID 返回
        if call_ctx.get("audio_ids"):
            audio_count = len(call_ctx["audio_ids"])
            id_list = ", ".join(call_ctx["audio_ids"])
            text_parts.append(f"[已生成 {audio_count} 个音频，资源ID: {id_list}]")

        if call_ctx.get("video_ids"):
            video_count = len(call_ctx["video_ids"])
            id_list = ", ".join(call_ctx["video_ids"])
            text_parts.append(f"[已生成 {video_count} 个视频，资源ID: {id_list}]")

        # 有图片：返回 文本 + 图片 的 content 列表（FastMCP 分别转成 TextContent / ImageContent）
        if image_blocks:
            blocks: List[Any] = []
            if text_parts:
                blocks.append("\n".join(text_parts))
            blocks.extend(image_blocks)
            return blocks

        # 无图片：保持原有纯文本返回
        if text_parts:
            return "\n".join(text_parts)

        return f"✅ 命令 [{primary_keyword}] 已执行完成。"

    # 设置函数元数据
    handler.__name__ = tool_name
    handler.__doc__ = to_ai_doc
    handler.__qualname__ = f"mcp_server.{tool_name}"
    handler.__module__ = "gsuid_core.ai_core.mcp.server"

    return handler


def _create_mcp_server(auth: Optional[BearerTokenAuth] = None) -> FastMCP:
    """创建 MCP Server 实例，并注册所有 to_ai 触发器为 MCP 工具。

    Args:
        auth: 可选的认证提供者，用于 Bearer Token 验证

    Returns:
        配置好的 FastMCP 实例
    """
    server = FastMCP(
        name="GsCore",
        instructions=(
            "GsCore 框架的 MCP Server，暴露所有注册了 to_ai 的触发器工具。"
            "这些工具来自框架的各个插件，可以通过 text 参数传入指令来调用。"
        ),
        auth=auth,
    )

    trigger_registry = _MCP_TRIGGER_REGISTRY
    if not trigger_registry:
        logger.warning(t("🌐 [MCP Server] 没有发现任何 to_ai 触发器，MCP Server 将不注册任何工具"))
        return server

    registered_count = 0
    for tool_name, trigger_info in trigger_registry.items():
        try:
            handler = _build_mcp_tool_handler(tool_name, trigger_info)
            server.tool(handler)
            registered_count += 1
            logger.debug(
                t(
                    "🌐 [MCP Server] 注册工具: {tool_name} (触发器: {p0}, 插件: {p1})",
                    tool_name=tool_name,
                    p0=trigger_info["primary_keyword"],
                    p1=trigger_info["plugin_name"],
                )
            )
        except Exception as e:
            logger.error(t("🌐 [MCP Server] 注册工具 {tool_name} 失败: {e}", tool_name=tool_name, e=e))

    logger.info(
        t(
            "🌐 [MCP Server] 已注册 {registered_count}/{p0} 个触发器工具",
            registered_count=registered_count,
            p0=len(trigger_registry),
        )
    )
    return server


async def _start_mcp_server() -> None:
    """启动 MCP Server（后台任务）。"""
    global _mcp_server

    from gsuid_core.config import core_config
    from gsuid_core.ai_core.configs.ai_config import (
        mcp_server_config,
    )

    # 检查是否启用
    enable = mcp_server_config.get_config("enable_mcp_server").data
    if not enable:
        logger.info(t("🌐 [MCP Server] MCP Server 未启用，跳过启动"))
        return

    transport = mcp_server_config.get_config("mcp_server_transport").data
    port = int(mcp_server_config.get_config("mcp_server_port").data)
    api_key = mcp_server_config.get_config("mcp_server_api_key").data

    # 复用框架的 HOST 配置
    core_host = core_config.get_config("HOST").lower()
    if core_host in ("all", "none", "dual", ""):
        host = "0.0.0.0"
    else:
        host = core_host

    # 创建认证提供者
    auth: Optional[BearerTokenAuth] = None
    if api_key:
        auth = BearerTokenAuth(api_key)
        logger.info(t("🌐 [MCP Server] 已启用 Bearer Token 认证"))
    else:
        logger.warning(t("🌐 [MCP Server] 未配置 API Key，MCP Server 不启用认证"))

    # 创建服务器并注册工具
    _mcp_server = _create_mcp_server(auth=auth)

    if transport == "sse":
        logger.info(t("🌐 [MCP Server] 启动 SSE 模式 MCP Server @ {host}:{port}", host=host, port=port))
        try:
            await _mcp_server.run_async(
                transport="sse",
                host=host,
                port=port,
            )
        except Exception as e:
            logger.error(t("🌐 [MCP Server] MCP Server 启动失败: {e}", e=e))
    elif transport == "stdio":
        logger.info(t("🌐 [MCP Server] 启动 stdio 模式 MCP Server"))
        try:
            await _mcp_server.run_async(transport="stdio")
        except Exception as e:
            logger.error(t("🌐 [MCP Server] MCP Server 启动失败: {e}", e=e))
    else:
        logger.error(t("🌐 [MCP Server] 不支持的传输协议: {transport}", transport=transport))


async def _shutdown_mcp_server() -> None:
    """关闭 MCP Server。"""
    global _mcp_server, _server_task

    if _server_task and not _server_task.done():
        _server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _server_task
        logger.info(t("🌐 [MCP Server] MCP Server 任务已取消"))

    _mcp_server = None
    _server_task = None
    logger.info(t("🌐 [MCP Server] MCP Server 已关闭"))


def get_mcp_server() -> Optional[FastMCP]:
    """获取当前的 MCP Server 实例。"""
    return _mcp_server


def get_mcp_trigger_count() -> int:
    """获取已注册的 MCP 触发器数量。"""
    return len(_MCP_TRIGGER_REGISTRY)


# ─── 启动/关闭钩子 ──────────────────────────────────────────────────────────


async def init_mcp_server():
    """框架启动时启动 MCP Server（在 MCP 工具注册之后执行）。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.info(t("🔌 [MCP] AI总开关已关闭，跳过MCP Server启动"))
        return

    global _server_task
    _server_task = asyncio.create_task(_start_mcp_server())


@on_core_shutdown(priority=10)
async def _on_shutdown():
    """框架关闭时关闭 MCP Server。"""
    await _shutdown_mcp_server()
