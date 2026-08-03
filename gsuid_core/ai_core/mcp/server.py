"""
MCP Server 模块 — 将框架 `_TOOL_REGISTRY`（@ai_tools 整库）对外暴露为 MCP 服务

启用后，外部 MCP 客户端（Claude Desktop / Cursor 等）可连接并调用全部已注册的 AI 工具。

传输模式:
- ``http``（默认）: 挂载到主 FastAPI（默认端口 8765）的 ``/api/mcp``，
  Streamable HTTP；与主服务 API 同端口、不同 endpoint。
- ``stdio``: 进程 stdin/stdout 跑 MCP JSON-RPC（本地子进程场景）。

鉴权（框架 **不 import 任何插件**）:
- 可选静态 ``mcp_server_api_key``
- 插件经 ``register_mcp_token_verifier`` 注册的 Bearer 校验器
- 二者任一存在即强制鉴权；均不存在时为显式开放（开发模式）
- 工具执行时把 AccessToken.claims 写入 Event / ToolContext.extra

配置（``MCP_SERVER_CONFIG`` → ``data/ai_core/mcp_server_config.json``）:
- enable_mcp_server
- mcp_server_transport: ``http`` | ``stdio``
- mcp_server_path: HTTP 挂载路径（默认 ``/api/mcp``）
- mcp_server_api_key: 静态服务钥（可与插件校验器并存）
"""

from __future__ import annotations

import hmac
import asyncio
import inspect
import contextlib
from typing import Any, Dict, List, Callable, Optional, Awaitable

from fastmcp import FastMCP
from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.usage import RunUsage
from starlette.routing import Mount
from fastmcp.server.auth import AccessToken, AuthProvider
from pydantic_ai.models.test import TestModel

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.server import on_core_shutdown
from gsuid_core.ai_core.models import ToolBase, ToolContext

# ─── 常量 ───────────────────────────────────────────────────────────────────

_JSON_TYPE_MAP: Dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

_DEFAULT_MCP_PATH = "/api/mcp"
# 与 Event.user_pm 字段默认一致：6=最低权限；升权须 claims 显式给出
_DEFAULT_USER_PM = 6

# 插件注册的 Bearer 校验器：成功返回 claims（须含 user_id），失败返回 None。
McpTokenVerifier = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]
_mcp_token_verifiers: List[McpTokenVerifier] = []


def _callable_label(fn: Callable[..., Any]) -> str:
    """日志用短名；避免 getattr 兜底。"""
    if inspect.isfunction(fn) or inspect.ismethod(fn):
        return fn.__name__
    return repr(fn)


def register_mcp_token_verifier(verifier: McpTokenVerifier) -> None:
    """注册 MCP Bearer 校验器（由插件调用；可多次注册，按序短路成功）。"""
    if verifier in _mcp_token_verifiers:
        return
    _mcp_token_verifiers.append(verifier)
    logger.info(
        t(
            "log.mcp.mcp_server_token_verifier_register",
            p0=_callable_label(verifier),
        )
    )


def unregister_mcp_token_verifier(verifier: McpTokenVerifier) -> None:
    """移除已注册的校验器。"""
    if verifier in _mcp_token_verifiers:
        _mcp_token_verifiers.remove(verifier)


def clear_mcp_token_verifiers() -> None:
    """清空全部插件校验器（测试 / 关闭用）。"""
    _mcp_token_verifiers.clear()


def _json_schema_type_to_python(param_schema: Dict[str, Any]) -> type:
    """JSON Schema 属性 → Python 注解；复杂联合类型退回 object。"""
    if "type" in param_schema:
        raw = param_schema["type"]
        if isinstance(raw, list):
            non_null = [x for x in raw if x != "null"]
            if non_null and isinstance(non_null[0], str) and non_null[0] in _JSON_TYPE_MAP:
                return _JSON_TYPE_MAP[non_null[0]]
            return object
        if isinstance(raw, str) and raw in _JSON_TYPE_MAP:
            return _JSON_TYPE_MAP[raw]
        return object
    return object


def _token_matches_api_key(token: str, api_key: str) -> bool:
    """常量时间比较静态 api_key。"""
    if not api_key:
        return False
    left = token.encode("utf-8")
    right = api_key.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _auth_is_required(api_key: str) -> bool:
    """静态 key 或任意插件校验器存在时，鉴权为强制。"""
    return bool(api_key) or bool(_mcp_token_verifiers)


# ─── Auth（仅 api_key + 插件注册校验器；框架不 import 插件） ────────────────


class BearerTokenAuth(AuthProvider):
    """Bearer：静态 api_key，或 ``register_mcp_token_verifier`` 提供的校验。

    - key 与校验器**均无** → 显式开放（开发模式），匿名身份 ``user_pm=6``
    - 任一存在 → 必须通过对应校验，失败返回 ``None``
    """

    def __init__(self, api_key: str = "") -> None:
        super().__init__()
        self._api_key = api_key

    def _anonymous_token(self, token: str = "") -> AccessToken:
        return AccessToken(
            token=token,
            client_id="mcp_anonymous",
            scopes=["mcp:tools"],
            claims={
                "auth": "anonymous",
                "user_id": "mcp_anonymous",
                "user_pm": _DEFAULT_USER_PM,
            },
        )

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        required = _auth_is_required(self._api_key)

        if not token:
            if not required:
                return self._anonymous_token("")
            return None

        if _token_matches_api_key(token, self._api_key):
            return AccessToken(
                token=token,
                client_id="mcp_service",
                scopes=["mcp:tools"],
                claims={
                    "auth": "api_key",
                    "user_id": "mcp_service",
                    "user_pm": 0,
                },
            )

        for verifier in list(_mcp_token_verifiers):
            try:
                claims = await verifier(token)
            except Exception as e:
                # 插件校验器异常不得拖垮鉴权链；记日志后试下一个
                logger.warning(
                    t(
                        "log.mcp.mcp_server_token_verifier_fail",
                        p0=_callable_label(verifier),
                        e=e,
                    )
                )
                continue
            if not isinstance(claims, dict):
                continue
            if "user_id" not in claims:
                logger.warning(t("log.mcp.mcp_server_token_verifier_missing_user_id"))
                continue
            user_id = str(claims["user_id"])
            if "client_id" in claims:
                client_id = str(claims["client_id"])
            else:
                client_id = f"user:{user_id}"
            out_claims: Dict[str, Any] = dict(claims)
            if "auth" not in out_claims:
                out_claims["auth"] = "plugin"
            # 缺 user_pm 时写入最低权限，避免隐式 master
            if "user_pm" not in out_claims:
                out_claims["user_pm"] = _DEFAULT_USER_PM
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=["mcp:tools"],
                claims=out_claims,
            )

        if not required:
            return self._anonymous_token(token)

        logger.warning(t("log.mcp.mcp_server_bearer_token_verification_fail"))
        return None


# ─── 全局状态 ───────────────────────────────────────────────────────────────

_mcp_server: Optional[FastMCP] = None
_server_task: Optional[asyncio.Task[None]] = None
_exported_tool_count: int = 0
_http_mounted: bool = False
_mcp_http_app: Any = None
_mcp_lifespan_cm: Any = None
_mcp_mount_path: Optional[str] = None


def _identity_from_access_token() -> Dict[str, Any]:
    """从当前 MCP 请求的 AccessToken.claims 取身份（结构由插件校验器约定）。"""
    # fastmcp 依赖仅在请求上下文中可用；stdio / 无上下文时回落匿名
    from fastmcp.server.dependencies import get_access_token

    try:
        at = get_access_token()
    except LookupError:
        return {
            "auth": "none",
            "user_id": "mcp_client",
            "user_pm": _DEFAULT_USER_PM,
        }
    except RuntimeError:
        return {
            "auth": "none",
            "user_id": "mcp_client",
            "user_pm": _DEFAULT_USER_PM,
        }

    if at is None:
        return {
            "auth": "none",
            "user_id": "mcp_client",
            "user_pm": _DEFAULT_USER_PM,
        }

    if isinstance(at.claims, dict):
        claims = at.claims
    else:
        claims = {}
    out: Dict[str, Any] = dict(claims)
    if "auth" not in out:
        out["auth"] = "unknown"
    if "user_id" not in out:
        out["user_id"] = at.client_id or "mcp_client"
    if "user_pm" not in out:
        out["user_pm"] = _DEFAULT_USER_PM
    return out


def _http_session_overrides() -> Dict[str, str]:
    """从 HTTP 请求头读取框架通用会话覆盖（无业务域语义）。

    支持头（均可选）:
    - ``X-MCP-Group-Id`` → Event.group_id（插件侧会话键）
    - ``X-MCP-Bot-Id`` → Event.bot_id（插件虚拟 bot 标识）
    """
    out: Dict[str, str] = {}
    from fastmcp.server.dependencies import get_http_request

    try:
        req = get_http_request()
    except LookupError:
        return out
    except RuntimeError:
        return out
    if req is None:
        return out
    headers = req.headers
    # Starlette Headers 大小写不敏感；用 in + [] 避免 .get 兜底风格
    if "x-mcp-group-id" in headers:
        out["group_id"] = str(headers["x-mcp-group-id"])
    if "x-mcp-bot-id" in headers:
        out["bot_id"] = str(headers["x-mcp-bot-id"])
    return out


def _parse_user_pm(raw: Any) -> int:
    """claims.user_pm → int；非法或缺失用最低权限。"""
    if raw is None:
        return _DEFAULT_USER_PM
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_USER_PM


def _create_mock_event(
    text: str = "",
    command: str = "",
    *,
    user_id: str = "mcp_client",
    user_pm: int = _DEFAULT_USER_PM,
    bot_id: str = "MCP",
    group_id: str = "",
) -> Event:
    """MCP 调用用的模拟 Event。"""
    ev = Event()
    ev.text = text
    ev.command = command
    ev.raw_text = f"{command} {text}".strip()
    ev.user_pm = user_pm
    ev.user_id = user_id
    ev.bot_id = bot_id
    ev.bot_self_id = "MCP_Server"
    ev.user_type = "direct"
    if group_id:
        ev.group_id = group_id
    return ev


def _create_mock_bot(ev: Event) -> Bot:
    """MCP 调用用的模拟 Bot（无真实 WS）。"""
    from gsuid_core.bot import _Bot

    _bot = _Bot(str(ev.bot_id or "MCP_Server"))
    return Bot(_bot, ev)


def _build_run_context(tool_name: str) -> RunContext[ToolContext]:
    """构造 RunContext：claims + 可选 HTTP 会话头。"""
    ident = _identity_from_access_token()
    sess = _http_session_overrides()
    if "user_id" in ident:
        user_id = str(ident["user_id"])
    else:
        user_id = "mcp_client"
    if "user_pm" in ident:
        user_pm = _parse_user_pm(ident["user_pm"])
    else:
        user_pm = _DEFAULT_USER_PM

    if "bot_id" in sess:
        bot_id = sess["bot_id"]
    elif "bot_id" in ident:
        bot_id = str(ident["bot_id"])
    else:
        bot_id = "MCP"

    if "group_id" in sess:
        group_id = sess["group_id"]
    elif "group_id" in ident:
        group_id = str(ident["group_id"])
    else:
        group_id = ""

    fake_ev = _create_mock_event(
        command=tool_name,
        user_id=user_id,
        user_pm=user_pm,
        bot_id=bot_id,
        group_id=group_id,
    )
    mock_bot = _create_mock_bot(fake_ev)
    extra: Dict[str, Any] = {"source": "mcp_server", **ident, **sess}
    deps = ToolContext(bot=mock_bot, ev=fake_ev, extra=extra)
    return RunContext(
        deps=deps,
        model=TestModel(),
        usage=RunUsage(),
        tool_name=tool_name,
    )


def _format_tool_result(result: Any) -> str:
    """把 @ai_tools 返回值收成 MCP 可传的纯文本。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, ToolReturn):
        parts: List[str] = []
        if result.return_value is not None:
            parts.append(str(result.return_value))
        content = result.content
        if content is not None:
            if isinstance(content, str):
                parts.append(content)
            else:
                for item in content:
                    parts.append(str(item))
        if parts:
            return "\n".join(parts)
        return ""
    return str(result)


def _build_ai_tool_handler(tool_base: ToolBase, category: str) -> Any:
    """按 ToolBase 的 function_schema 动态生成 MCP handler（签名对客户端可见）。"""
    tool = tool_base.tool
    tool_name = tool_base.name
    description = tool_base.description or tool_name
    json_schema = tool.function_schema.json_schema

    properties: Dict[str, Any]
    if isinstance(json_schema, dict) and "properties" in json_schema:
        raw_props = json_schema["properties"]
        properties = raw_props if isinstance(raw_props, dict) else {}
    else:
        properties = {}

    required_fields: List[str] = []
    if isinstance(json_schema, dict) and "required" in json_schema:
        raw_req = json_schema["required"]
        if isinstance(raw_req, list):
            required_fields = [x for x in raw_req if isinstance(x, str)]

    annotations: Dict[str, Any] = {}
    params: List[inspect.Parameter] = []

    for param_name, param_schema in properties.items():
        if not isinstance(param_schema, dict):
            param_schema = {}
        py_type = _json_schema_type_to_python(param_schema)
        has_default = "default" in param_schema
        is_required = param_name in required_fields

        if is_required and not has_default:
            annotations[param_name] = py_type
            params.append(
                inspect.Parameter(
                    param_name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=py_type,
                )
            )
        else:
            default_val: Any = param_schema["default"] if has_default else None
            opt_type = Optional[py_type]
            annotations[param_name] = opt_type
            params.append(
                inspect.Parameter(
                    param_name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=opt_type,
                    default=default_val,
                )
            )

    async def handler(**kwargs: Any) -> str:
        # 原样透传；None / 默认值语义交由工具自身与 pydantic 处理
        call_args = dict(kwargs)
        logger.info(
            t(
                "log.mcp.mcp_server_calling_ai_tool",
                tool_name=tool_name,
                category=category,
                call_args=repr(call_args)[:500],
            )
        )
        run_ctx = _build_run_context(tool_name)
        try:
            raw = await tool.function(run_ctx, **call_args)
        except Exception as e:
            logger.error(
                t(
                    "log.mcp.mcp_server_ai_tool_call_fail",
                    tool_name=tool_name,
                    e=e,
                )
            )
            return f"❌ 工具 [{tool_name}] 执行异常: {e}"
        return _format_tool_result(raw)

    handler.__name__ = tool_name
    handler.__doc__ = description
    handler.__qualname__ = f"mcp_server.{tool_name}"
    handler.__module__ = "gsuid_core.ai_core.mcp.server"
    handler.__annotations__ = {**annotations, "return": str}
    setattr(handler, "__signature__", inspect.Signature(parameters=params, return_annotation=str))

    return handler


def _iter_registry_tools() -> List[tuple[str, str, ToolBase]]:
    """扁平化 `_TOOL_REGISTRY`：[(export_name, category, ToolBase), ...]。"""
    from gsuid_core.ai_core.register import get_registered_tools

    registered = get_registered_tools()
    used_names: Dict[str, str] = {}
    out: List[tuple[str, str, ToolBase]] = []

    for category, cat_tools in registered.items():
        for name, tool_base in cat_tools.items():
            export_name = name
            if export_name in used_names:
                export_name = f"{name}__{category}"
            used_names[export_name] = category
            out.append((export_name, category, tool_base))

    out.sort(key=lambda x: x[0])
    return out


def _create_mcp_server(auth: Optional[BearerTokenAuth] = None) -> FastMCP:
    """创建 FastMCP，注册启动时刻 `_TOOL_REGISTRY` 快照中的全部 @ai_tools。"""
    global _exported_tool_count

    server = FastMCP(
        name="GsCore",
        instructions=(
            "GsCore 框架 MCP Server：暴露全部已注册的 @ai_tools。"
            "请按各工具 JSON Schema 传参；鉴权使用 Authorization: Bearer <token>。"
        ),
        auth=auth,
    )

    tools = _iter_registry_tools()
    if not tools:
        logger.warning(t("log.mcp.mcp_server_ai_tools_found"))
        _exported_tool_count = 0
        return server

    registered_count = 0
    for export_name, category, tool_base in tools:
        try:
            handler = _build_ai_tool_handler(tool_base, category)
            if handler.__name__ != export_name:
                handler.__name__ = export_name
                handler.__qualname__ = f"mcp_server.{export_name}"
            server.tool(
                handler,
                name=export_name,
                description=tool_base.description or export_name,
                tags={category, tool_base.plugin},
            )
            registered_count += 1
            logger.debug(
                t(
                    "log.mcp.mcp_server_name_ai_tool_register",
                    tool_name=export_name,
                    category=category,
                    plugin=tool_base.plugin,
                )
            )
        except Exception as e:
            logger.error(
                t(
                    "log.mcp.mcp_server_register_name_tool_fail",
                    tool_name=export_name,
                    e=e,
                )
            )

    _exported_tool_count = registered_count
    logger.info(
        t(
            "log.mcp.mcp_server_registered_ai_tools_register",
            registered_count=registered_count,
            p0=len(tools),
        )
    )
    # 导出面是启动快照；热注册工具不会自动出现在 MCP 列表
    logger.info(t("log.mcp.mcp_server_export_snapshot_at_start", p0=registered_count))
    return server


def _normalize_transport(raw: str) -> str:
    """兼容旧配置 ``sse`` → ``http``（同端口挂载）。"""
    if raw in ("http", "stdio"):
        return raw
    if raw == "sse":
        logger.warning(t("log.mcp.mcp_server_sse_deprecated_use_http"))
        return "http"
    return raw


def _normalize_mcp_path(raw: str) -> str:
    path = (raw or _DEFAULT_MCP_PATH).strip() or _DEFAULT_MCP_PATH
    if not path.startswith("/"):
        path = "/" + path
    # 去掉尾斜杠，避免 // 与路由重复
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def _remove_mounted_path(main_app: Any, path: str) -> None:
    """从主 FastAPI 路由表移除指定 mount path。"""
    kept: List[Any] = []
    for route in main_app.router.routes:
        if isinstance(route, Mount) and route.path == path:
            continue
        kept.append(route)
    main_app.router.routes = kept


async def _mount_http_mcp(server: FastMCP, path: str) -> None:
    """把 Streamable HTTP MCP 挂到主 FastAPI（默认 8765），不另开端口。

    必须：
    1. ``mount`` 完整 ASGI 子应用（含 AuthenticationMiddleware）
    2. 手动进入子应用 ``lifespan``（主 app 已在跑，晚挂载不会自动跑子 lifespan）
       否则 StreamableHTTPSessionManager task group 未初始化 → 500
    """
    global _http_mounted, _mcp_http_app, _mcp_lifespan_cm, _mcp_mount_path

    if _http_mounted:
        logger.warning(t("log.mcp.mcp_server_http_already_mounted"))
        return

    from gsuid_core.app_life import app as main_app

    # 子应用内部路由挂在 "/"，对外由 mount 前缀构成完整 path（如 /api/mcp）
    mcp_app = server.http_app(
        path="/",
        transport="streamable-http",
        stateless_http=True,
    )
    main_app.mount(path, mcp_app)
    _mcp_http_app = mcp_app
    _mcp_mount_path = path

    # 主 FastAPI 的 lifespan 早已 yield；晚挂载必须手动 aenter 子 lifespan
    try:
        _mcp_lifespan_cm = mcp_app.lifespan(mcp_app)
        await _mcp_lifespan_cm.__aenter__()
    except Exception as e:
        logger.error(t("log.mcp.mcp_server_lifespan_enter_fail", e=e))
        _remove_mounted_path(main_app, path)
        _mcp_http_app = None
        _mcp_lifespan_cm = None
        _mcp_mount_path = None
        raise

    _http_mounted = True
    logger.info(t("log.mcp.mcp_server_http_mounted", path=path))


def _log_auth_mode(api_key: str) -> None:
    """启动时区分 open / 静态 key / 插件校验器 / 并存。"""
    has_key = bool(api_key)
    has_verifiers = bool(_mcp_token_verifiers)
    if has_key and has_verifiers:
        logger.info(
            t(
                "log.mcp.mcp_server_auth_static_and_plugin",
                p0=len(_mcp_token_verifiers),
            )
        )
    elif has_key:
        logger.info(t("log.mcp.mcp_server_bearer_token_authentication_ok"))
    elif has_verifiers:
        logger.info(
            t(
                "log.mcp.mcp_server_auth_plugin_only",
                p0=len(_mcp_token_verifiers),
            )
        )
    else:
        logger.warning(t("log.mcp.mcp_server_auth_open"))


async def _start_mcp_server() -> None:
    """启动 MCP Server：http 挂载主应用；stdio 后台 task。"""
    global _mcp_server

    from gsuid_core.ai_core.configs.ai_config import mcp_server_config

    enable = mcp_server_config.get_config("enable_mcp_server").data
    if not enable:
        logger.info(t("log.mcp.mcp_server_enabled_skipping_start"))
        return

    transport = _normalize_transport(str(mcp_server_config.get_config("mcp_server_transport").data))
    api_key = str(mcp_server_config.get_config("mcp_server_api_key").data or "")
    path = _normalize_mcp_path(str(mcp_server_config.get_config("mcp_server_path").data))

    auth = BearerTokenAuth(api_key)
    _log_auth_mode(api_key)

    _mcp_server = _create_mcp_server(auth=auth)

    if transport == "http":
        try:
            await _mount_http_mcp(_mcp_server, path)
        except Exception as e:
            logger.error(t("log.mcp.mcp_server_startup", e=e))
            _mcp_server = None
    elif transport == "stdio":
        logger.info(t("log.mcp.mcp_server_stdio_mode"))
        try:
            await _mcp_server.run_async(transport="stdio")
        except Exception as e:
            logger.error(t("log.mcp.mcp_server_startup", e=e))
    else:
        logger.error(
            t(
                "log.mcp.mcp_server_unsupported_transport_protocol",
                transport=transport,
            )
        )


async def _shutdown_mcp_server() -> None:
    """关闭 MCP Server，并卸下 HTTP 挂载。"""
    global _mcp_server, _server_task, _exported_tool_count, _http_mounted
    global _mcp_http_app, _mcp_lifespan_cm, _mcp_mount_path

    if _mcp_lifespan_cm is not None:
        try:
            await _mcp_lifespan_cm.__aexit__(None, None, None)
        except Exception as e:
            logger.debug(t("log.mcp.mcp_server_lifespan_exit_fail", e=e))
        _mcp_lifespan_cm = None

    if _mcp_mount_path is not None:
        from gsuid_core.app_life import app as main_app

        _remove_mounted_path(main_app, _mcp_mount_path)
        logger.info(t("log.mcp.mcp_server_http_unmounted", path=_mcp_mount_path))
        _mcp_mount_path = None

    _mcp_http_app = None

    if _server_task is not None and not _server_task.done():
        _server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _server_task
        logger.info(t("log.mcp.mcp_server_task_cancelled"))

    _mcp_server = None
    _server_task = None
    _exported_tool_count = 0
    _http_mounted = False
    logger.info(t("log.mcp.mcp_server_stopped"))


def get_mcp_server() -> Optional[FastMCP]:
    """获取当前的 MCP Server 实例。"""
    return _mcp_server


def get_mcp_exported_tool_count() -> int:
    """当前 MCP Server 已导出的 @ai_tools 数量。"""
    return _exported_tool_count


def get_mcp_trigger_count() -> int:
    """兼容旧 API：返回已导出工具数。"""
    if _exported_tool_count > 0:
        return _exported_tool_count
    from gsuid_core.ai_core.trigger_bridge import _MCP_TRIGGER_REGISTRY

    return len(_MCP_TRIGGER_REGISTRY)


# ─── 启动/关闭钩子 ──────────────────────────────────────────────────────────


async def init_mcp_server() -> None:
    """框架启动时启动 MCP Server（须在工具注册完成之后）。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.info(t("log.mcp.ai_master_switch_skipping"))
        return

    global _server_task
    # http 挂载是同步轻量；stdio 才需要常驻 task
    from gsuid_core.ai_core.configs.ai_config import mcp_server_config

    transport = _normalize_transport(str(mcp_server_config.get_config("mcp_server_transport").data))
    if transport == "stdio":
        _server_task = asyncio.create_task(_start_mcp_server())
    else:
        await _start_mcp_server()


@on_core_shutdown(priority=10)
async def _on_shutdown() -> None:
    """框架关闭时关闭 MCP Server。"""
    await _shutdown_mcp_server()
