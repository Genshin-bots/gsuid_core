"""
MCP 客户端核心模块

提供通用的 MCP 客户端功能，用于连接和调用 MCP 服务器。
基于 fastmcp 实现，支持 stdio / sse / Streamable HTTP 三种传输。

设计原则：
- 每次调用时建立连接、执行操作、断开连接（无状态模式）
- 支持通过代码配置连接参数（command, args, env）— stdio 模式
- 支持通过 URL 和请求头连接远程服务器（url, headers）— sse / streamable_http
- 完全异步，兼容项目的 async 架构
"""

import re
import sys
import shutil
import asyncio
from typing import TYPE_CHECKING, Any, Union
from pathlib import Path
from dataclasses import field, dataclass

from fastmcp import Client
from mcp.types import TextContent, ImageContent, ResourceLink, EmbeddedResource
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.ai_core.mcp.transport import (
    MCP_TRANSPORT_SSE,
    MCP_TRANSPORT_STDIO,
    MCP_TRANSPORT_STREAMABLE_HTTP,
    resolve_mcp_transport,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.mcp.config_manager import MCPConfig

# 资源句柄仅存于主进程，stdio 子进程无法访问需在入参前物化为 data URI
_RESOURCE_HANDLE_RE = re.compile(
    r"^(?:img_[0-9a-f]{16}|res_[0-9a-f]{12})(?:\.(?:png|jpe?g|webp|gif))?$",
    re.IGNORECASE,
)

# stdio 服务默认工作目录：data/ai_core/mcp_stdio/{服务名}——隔离各服务落盘的
# 临时文件/缓存/小库，避免写进 Core 进程目录。显式 cwd 优先。
_STDIO_WORKSPACE_DIR = "mcp_stdio"


def _sanitize_dir_name(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().strip(".")
    return safe or "default"


def default_stdio_cwd(name: str) -> Path:
    """stdio 服务默认工作目录（自动创建）。显式配置了 cwd 时不用它。"""
    path = get_res_path("ai_core") / _STDIO_WORKSPACE_DIR / _sanitize_dir_name(name)
    path.mkdir(parents=True, exist_ok=True)
    return path


# 源码检出型服务浅克隆超时：--depth 1 一般几秒，给足网络余量
_CHECKOUT_TIMEOUT_S = 180

# stdio 连接+握手超时：npx/uvx 首次运行要下载包，但不能无限等（前端请求 60s 上限）
_CONNECT_TIMEOUT_S = 45

# 参数里「指项目目录」的旗标：检出后把旗标值改写为实际工作目录。
# 不含 --project：dotnet/uv 的 --project 值可为 .csproj 文件，相对路径由 cwd 解析即可。
_CHECKOUT_DIR_FLAGS = ("--directory", "--cwd", "-C")

# 上游 README 常见的仓库占位前缀（path/to/repo/…），检出后改写为真实路径
_CHECKOUT_PLACEHOLDER_RE = re.compile(r"path[/\\]to[/\\](.+)$", re.IGNORECASE)


def _resolve_checkout_workdir(name: str, cwd: str) -> "tuple[Path, bool]":
    """解析检出工作目录（显式 cwd 优先，否则默认目录），返回 (目录, 是否为空待克隆)。"""
    workdir = Path(cwd).expanduser() if cwd.strip() else default_stdio_cwd(name)
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir, not any(workdir.iterdir())


async def ensure_mcp_source_checkout(name: str, source_url: str, cwd: str = "") -> "tuple[Path, str]":
    """源码检出型 stdio：把 source_url 浅克隆进工作目录（显式 cwd 优先，否则默认目录）。

    目录已非空时直接复用（幂等），返回 (workdir, 错误描述)；空串表示就绪。
    """
    workdir, needs_clone = _resolve_checkout_workdir(name, cwd)
    if not needs_clone:
        return workdir, ""
    if shutil.which("git") is None:
        return workdir, "系统未安装 git，无法克隆源码仓库"
    logger.info(t("log.mcp.checkout_begin", p0=name, p1=source_url))
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--depth",
        "1",
        source_url,
        str(workdir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CHECKOUT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        err = f"克隆超时（{_CHECKOUT_TIMEOUT_S}s）"
        logger.warning(t("log.mcp.checkout_failed", p0=name, p1=err))
        return workdir, err
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        err = f"git clone 失败: {tail[-1] if tail else proc.returncode}"
        logger.warning(t("log.mcp.checkout_failed", p0=name, p1=err))
        return workdir, err
    logger.info(t("log.mcp.checkout_done", p0=name, p1=str(workdir)))
    return workdir, ""


async def ensure_checkout_for_config(config: "MCPConfig") -> str:
    """配置级自愈：源码检出型 stdio 服务在连接前确保源码已克隆。返回 "" 或错误描述。"""
    if config.get_transport() != MCP_TRANSPORT_STDIO:
        return ""
    meta = config.market_meta
    if not meta.get("requires_checkout"):
        return ""
    source_url = meta.get("source_url", "")
    if not source_url:
        return "该服务需要克隆源码运行，但配置缺少仓库地址"
    workdir, err = await ensure_mcp_source_checkout(config.name, source_url, config.cwd)
    if err:
        return err
    note = repair_checkout_script(config.args, workdir)
    if note:
        logger.info(t("log.mcp.checkout_repaired", p0=config.name, p1=note))
    return ""


# 检出仓库入口脚本的常见上游遗漏：定义了 NAME = FastMCP(...) 但通篇没有 .run(/
# __main__——python x.py 定义完工具即退出，握手报 Connection closed（如
# SyncTravel-skills 的 scripts/mcp_server.py）。检测后自动补标准启动守卫（幂等，
# 文件已含任何 run 调用或 __main__ 块则不动）
_FASTMCP_OBJ_RE = re.compile(r"^(\w+)\s*=\s*FastMCP\(", re.MULTILINE)


def repair_checkout_script(args: list, workdir: Path) -> str:
    """检出型 .py 入口缺启动守卫时补 ``if __name__ == "__main__": X.run()``。

    返回修复描述（未命中/无需修复返回空串）；只处理 FastMCP 形态，
    低层 Server / 其他框架不猜不改。
    """
    for resolved in rewrite_checkout_args(args, workdir):
        if not isinstance(resolved, str) or not resolved.lower().endswith(".py"):
            continue
        path = Path(resolved)
        if not path.is_absolute():
            path = workdir / path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if ".run(" in text or "__main__" in text:
            return ""
        m = _FASTMCP_OBJ_RE.search(text)
        if not m:
            continue
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(f'\nif __name__ == "__main__":\n    {m.group(1)}.run()\n')
        except OSError:
            continue
        return f"已为入口脚本 {path.name} 补全 FastMCP 启动守卫（上游遗漏 {m.group(1)}.run()）"
    return ""


def rewrite_checkout_args(args: list, workdir: Path) -> list:
    """检出型变体的参数改写：目录旗标值 / path-to 占位 → 实际工作目录。

    相对路径参数（scripts/x.py、./dist/index.js）由 cwd 解析，不在此改写。
    """
    out = []
    skip_next = False
    for arg in args:
        if not isinstance(arg, str):
            out.append(arg)
            continue
        if skip_next:
            out.append(str(workdir))
            skip_next = False
            continue
        if arg in _CHECKOUT_DIR_FLAGS:
            out.append(arg)
            skip_next = True
            continue
        head, sep, _ = arg.partition("=")
        if sep and head in _CHECKOUT_DIR_FLAGS:
            out.append(f"{head}={workdir}")
            continue
        m = _CHECKOUT_PLACEHOLDER_RE.search(arg)
        if m:
            # path/to/<repo>/<rel> vs path/to/<rel> 歧义：克隆已完成，按存在性消歧
            tail = m.group(1).replace("\\", "/")
            cand_full = workdir / tail
            parts = tail.split("/")
            cand_drop = workdir / "/".join(parts[1:]) if len(parts) > 1 else cand_full
            out.append(str(cand_full if cand_full.exists() else cand_drop))
            continue
        out.append(arg)
    return out


async def _materialize_resource_handle(value: str) -> str:
    """把 GsCore 资源句柄解析成 data URI，供 stdio 的 MCP 子进程直接消费。

    ``img_``（RM 内存图片）与 ``res_``（Kanban artifact）都只存在于主进程，子进程
    取不到；原样透传会被当成文件路径去找，下游只会回一句「图片文件不存在」。

    Args:
        value: 单个参数值

    Returns:
        解析后的 data URI；不是资源句柄、或句柄取不出来时原样返回
    """
    if not _RESOURCE_HANDLE_RE.match(value):
        return value
    # Agent 会给句柄补图片扩展名，RM / artifact 里存的是裸句柄
    handle = re.sub(r"\.(?:png|jpe?g|webp|gif)$", "", value, flags=re.IGNORECASE)

    # 延迟导入：image_reader 依赖 register / content_guard，模块级引入会形成导入环
    from gsuid_core.ai_core.buildin_tools.image_reader import _resolve_image_to_url

    uri, err = await _resolve_image_to_url(handle)
    if uri is None:
        # 解析失败原样放行避免误伤外部同形态 ID；多行错误压成单行截断入日志
        brief = " ".join((err or "未知原因").split())[:200]
        logger.warning(t("log.mcp.resource_handle_unresolved", handle=handle, e=brief))
        return value
    return uri


async def _materialize_resource_args(value: object) -> object:
    """递归把参数里的 GsCore 资源句柄换成 data URI（list / dict 里的同样处理）。"""
    if isinstance(value, str):
        return await _materialize_resource_handle(value)
    if isinstance(value, list):
        return [await _materialize_resource_args(item) for item in value]
    if isinstance(value, dict):
        return {str(key): await _materialize_resource_args(item) for key, item in value.items()}
    return value


@dataclass
class MCPToolInfo:
    """MCP 工具信息"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPToolResult:
    """MCP 工具调用结果"""

    content: list[dict[str, Any]]
    is_error: bool = False

    @property
    def text(self) -> str:
        """提取所有文本内容并拼接"""
        texts: list[str] = []
        for item in self.content:
            if "type" in item and item["type"] == "text" and "text" in item:
                text_value = item["text"]
                if isinstance(text_value, str):
                    texts.append(text_value)
        return "\n".join(texts)


@dataclass
class MCPClient:
    """
    MCP 客户端

    支持三种传输方式连接 MCP 服务器：
    - stdio: 通过 command + args + env 启动本地进程
    - sse: 通过 url + headers 连接远程 SSE 服务器（旧传输）
    - streamable_http: 通过 url + headers 连接远程 Streamable HTTP 服务器

    每次操作独立建立连接，操作完成后自动断开。

    Args:
        name: MCP 服务器名称，用于日志标识
        command: 启动命令，如 "uvx", "npx", "python" 等（stdio 模式）
        args: 命令参数列表（stdio 模式）
        env: 环境变量字典（stdio 模式）
        url: 远程服务器 URL（sse / streamable_http 模式）
        headers: HTTP 请求头字典（远程模式，如 Authorization）
        transport: 显式传输方式；空则按 url / command 推断
        cwd: 子进程工作目录（stdio 模式，源码检出型服务指向 clone 的仓库目录）；
            留空自动使用 data/ai_core/mcp_stdio/{服务名}

    Example (stdio):
        >>> client = MCPClient(
        ...     name="MiniMax",
        ...     command="uvx",
        ...     args=["minimax-coding-plan-mcp"],
        ...     env={"MINIMAX_API_KEY": "your_key"},
        ... )
        >>> tools = await client.list_tools()
        >>> result = await client.call_tool("web_search", {"query": "Python"})

    Example (streamable_http):
        >>> client = MCPClient(
        ...     name="Example",
        ...     transport="streamable_http",
        ...     url="https://example.com/mcp",
        ...     headers={"Authorization": "Bearer your_key"},
        ... )
        >>> tools = await client.list_tools()
    """

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = ""
    cwd: str = ""

    def _resolve_transport(self) -> str:
        """显式 transport 优先，否则按 url / command 推断。"""
        return resolve_mcp_transport(
            self.transport,
            url=self.url,
            command=self.command,
        )

    def _create_transport(
        self,
    ) -> Union[StdioTransport, SSETransport, StreamableHttpTransport]:
        """创建传输层（stdio / sse / streamable_http）。"""
        transport_type = self._resolve_transport()

        if transport_type == MCP_TRANSPORT_SSE:
            logger.debug(t("log.mcp.sse_transport_url", p0=self.name, p1=self.url))
            return SSETransport(
                url=self.url,
                headers=self.headers if self.headers else None,
            )
        if transport_type == MCP_TRANSPORT_STREAMABLE_HTTP:
            logger.debug(t("log.mcp.streamable_http_transport_url", p0=self.name, p1=self.url))
            return StreamableHttpTransport(
                url=self.url,
                headers=self.headers if self.headers else None,
            )
        if transport_type != MCP_TRANSPORT_STDIO:
            logger.warning(
                t(
                    "log.mcp.unknown_transport_fallback_stdio",
                    p0=self.name,
                    p1=transport_type,
                )
            )
        args = list(self.args)
        base = Path(self.command).name.lower()
        command = self.command
        # 裸 python 解析到 PATH 上的系统解释器（往往没有 mcp/pydantic 等依赖，
        # 子进程秒退报 Connection closed）——统一改写为 Core 自己的解释器，
        # 它必然带 MCP 依赖。显式路径（含 / \ 分隔符）保留用户指定语义
        if base in ("python", "python3", "python.exe", "python3.exe", "py", "py.exe") and not (
            "/" in self.command or "\\" in self.command
        ):
            command = sys.executable
        # npx 不带 -y 时首次安装未知包会在 stdin 等 "Ok to proceed?"，非 TTY 下直接卡死握手
        if base in ("npx", "npx.cmd", "npx.exe") and not any(
            a in ("-y", "--yes") or a.startswith("--yes=") for a in args
        ):
            args.insert(0, "-y")
        return StdioTransport(
            command=command,
            args=args,
            env=self.env if self.env else None,
            cwd=self.cwd.strip() or str(default_stdio_cwd(self.name)),
        )

    @staticmethod
    def _truncate_args(arguments: dict[str, Any] | None, max_len: int = 100) -> dict[str, Any]:
        """
        截断参数中的长字符串值，避免 base64 等大段数据污染日志

        Args:
            arguments: 工具调用参数字典
            max_len: 单个值的最大显示长度

        Returns:
            截断后的参数字典副本
        """
        if not arguments:
            return {}
        from gsuid_core.ai_core.mcp.pii import mask_credential_forms

        # 凭据形态值先打码：wrapper 注入的 credential_params 会走到这里，
        # token/api_key 类参数值绝不能明文进日志（截断不够——短 token 不截断）。
        _CRED_KEY_RE = re.compile(r"token|api_?key|app_?key|secret|password|credential|auth", re.IGNORECASE)
        truncated: dict[str, Any] = {}
        for key, value in arguments.items():
            if isinstance(value, str):
                if _CRED_KEY_RE.search(key):
                    truncated[key] = "****"
                elif len(value) > max_len:
                    truncated[key] = f"{value[:max_len]}...[截断, 总长={len(value)}]"
                else:
                    truncated[key] = mask_credential_forms(value)
            else:
                truncated[key] = value
        return truncated

    async def list_tools(self) -> list[MCPToolInfo]:
        """
        列出 MCP 服务器提供的所有工具

        Returns:
            工具信息列表

        Raises:
            连接或通信失败时抛出异常
        """
        transport = self._create_transport()
        client = Client(transport)

        logger.info(t("log.mcp.connecting_server_fetching_list", p0=self.name))

        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT_S):
                async with client:
                    raw_tools = await client.list_tools()
        except TimeoutError as e:
            raise TimeoutError(
                f"连接超时：{_CONNECT_TIMEOUT_S}s 内未完成握手（stdio 首次运行需下载包，可稍后重试）"
            ) from e
        finally:
            await client.close()  # 幂等；超时/取消路径兜底回收 stdio 子进程

        tools: list[MCPToolInfo] = []
        for tool in raw_tools:
            schema = tool.inputSchema
            tools.append(
                MCPToolInfo(
                    name=tool.name,
                    description=tool.description if tool.description else "",
                    input_schema=schema if schema else {},
                )
            )
        logger.info(t("log.mcp.tool_fetched_tools", p0=self.name, p1=len(tools)))
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> MCPToolResult:
        """
        调用 MCP 服务器上的指定工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数字典

        Returns:
            工具调用结果

        Raises:
            连接或调用失败时抛出异常
        """
        transport = self._create_transport()
        client = Client(transport, init_timeout=_CONNECT_TIMEOUT_S)

        # 截断过长的参数值，避免 base64 等大段数据污染日志
        truncated_args = self._truncate_args(arguments)
        logger.info(
            t(
                "log.mcp.calling_name_args_truncated_2",
                p0=self.name,
                tool_name=tool_name,
                truncated_args=truncated_args,
            )
        )

        # stdio 子进程无法访问主进程资源句柄需物化；远程传输原生认句柄故跳过
        if isinstance(transport, StdioTransport) and arguments:
            materialized = await _materialize_resource_args(arguments)
            if isinstance(materialized, dict):
                arguments = materialized

        try:
            async with client:
                # FastMCP 3 默认 raise_on_error=True，会把工具错误直接抛成 ToolError；
                # 本项目统一用 MCPToolResult.is_error 处理失败路径，故显式关闭。
                result = await client.call_tool(
                    name=tool_name,
                    arguments=arguments or {},
                    raise_on_error=False,
                )
        finally:
            await client.close()  # 幂等；取消路径兜底回收 stdio 子进程

        # 将 CallToolResult 转换为统一格式
        content_list: list[dict[str, Any]] = []
        for item in result.content:
            if isinstance(item, TextContent):
                content_list.append({"type": "text", "text": item.text})
            elif isinstance(item, ImageContent):
                content_list.append(
                    {
                        "type": "image",
                        "data": item.data,
                        "mimeType": item.mimeType,
                    }
                )
            elif isinstance(item, (ResourceLink, EmbeddedResource)):
                content_list.append({"type": "resource", "text": str(item)})
            else:
                content_list.append({"type": "text", "text": str(item)})

        tool_result = MCPToolResult(
            content=content_list,
            is_error=result.is_error,
        )

        logger.info(
            t(
                "log.mcp.name_call_fail_content",
                p0=self.name,
                tool_name=tool_name,
                p1=tool_result.is_error,
                p2=len(tool_result.text),
            )
        )

        return tool_result
