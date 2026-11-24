"""
MCP Server 模块 — 将框架 `_TOOL_REGISTRY`（@ai_tools 整库）对外暴露为 MCP 服务

启用后，外部 MCP 客户端（Claude Desktop / Cursor 等）可连接并调用全部已注册的 AI 工具。

传输模式:
- ``http``（默认）: 挂载到主 FastAPI（默认端口 8765）的 ``/api/mcp``，
  Streamable HTTP；与主服务 API 同端口、不同 endpoint。
  ``/api/mcp`` 与 ``/api/mcp/`` 都直接进入，不发 307。
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
import time
import base64
import asyncio
import inspect
import contextlib
from typing import Dict, List, Literal, Mapping, Callable, Optional, Awaitable, TypeGuard
from urllib.parse import urljoin

import anyio
import httpx2
import mcp.types as mcp_types
from fastmcp import FastMCP
from pydantic_ai import RunContext, ToolReturn
from starlette.types import Send, Scope, ASGIApp, Receive
from pydantic_ai.usage import RunUsage
from fastmcp.tools.base import ToolResult
from fastmcp.server.auth import AccessToken, AuthProvider
from pydantic_ai.messages import BinaryContent
from fastmcp.utilities.types import Image as FastMcpImage
from pydantic_ai.models.test import TestModel

from gsuid_core.bot import Bot, _Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message as GsMessage
from gsuid_core.server import on_core_shutdown
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.utils.path_safety import reject_outbound_http_url
from gsuid_core.utils.image.image_io import unwrap_image_url
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.mcp.server_http import (
    _ASGICallable,
    _HttpExchange,
    _is_fresh_asgi,
    _McpSlashMount,
    _McpHttpRuntime as _McpHttpRuntime,
    _snapshot_inner,
    _close_lifespan_cm,
    _capture_mcp_failure,
    _remove_mounted_path,
    _respond_mcp_unavailable,
    _finish_open_http_response,
)

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

_SYSTEM_MCP_USER_IDS: frozenset[str] = frozenset({"", "mcp_client", "mcp_service", "mcp_anonymous"})
_HTTP_SESSION_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-mcp-group-id", "group_id"),
    ("x-mcp-bot-id", "bot_id"),
    ("x-mcp-user-id", "user_id"),
)
_MSG_TYPE_LABELS: Dict[str, str] = {
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "node": "[合并转发]",
}

# 插件注册的 Bearer 校验器：成功返回 claims（须含 user_id），失败返回 None。
McpTokenVerifier = Callable[[str], Awaitable[Optional[Mapping[str, object]]]]
_mcp_token_verifiers: List[McpTokenVerifier] = []

#: 插件在构造 MCP Event 后补会话字段（如 group_id / user_pm）。同步、廉价。
McpEventEnricher = Callable[[Event], None]
_mcp_event_enrichers: List[McpEventEnricher] = []

#: 导出白名单：任一过滤器返回 True 即导出；一个都不注册则导出全部（兼容旧部署）。
McpExportFilter = Callable[[str, str, ToolBase], bool]
_mcp_export_filters: List[McpExportFilter] = []


def _callable_label(fn: Callable[..., object]) -> str:
    """日志用短名；避免 getattr 兜底。"""
    if inspect.isfunction(fn) or inspect.ismethod(fn):
        return fn.__name__
    return repr(fn)


def register_mcp_token_verifier(verifier: McpTokenVerifier) -> None:
    """注册 MCP Bearer 校验器（由插件调用；可多次注册，按序短路成功）。"""
    if verifier in _mcp_token_verifiers:
        return
    _mcp_token_verifiers.append(verifier)
    logger.info(t("log.mcp.mcp_server_token_verifier_register", p0=_callable_label(verifier)))


def unregister_mcp_token_verifier(verifier: McpTokenVerifier) -> None:
    """移除已注册的校验器。"""
    if verifier in _mcp_token_verifiers:
        _mcp_token_verifiers.remove(verifier)


def clear_mcp_token_verifiers() -> None:
    """清空全部插件校验器（测试 / 关闭用）。"""
    _mcp_token_verifiers.clear()


def register_mcp_event_enricher(fn: McpEventEnricher) -> None:
    """注册 MCP Event 补全器：每次 tools/call 构造 Event 后由插件补会话字段。"""
    if fn in _mcp_event_enrichers:
        return
    _mcp_event_enrichers.append(fn)
    logger.info(t("log.mcp.mcp_server_event_enricher_register", p0=_callable_label(fn)))


def unregister_mcp_event_enricher(fn: McpEventEnricher) -> None:
    if fn in _mcp_event_enrichers:
        _mcp_event_enrichers.remove(fn)


def register_mcp_export_filter(fn: McpExportFilter) -> None:
    """注册 MCP 工具导出过滤器。任一过滤器 True 即导出；全未注册则导出全部。"""
    if fn in _mcp_export_filters:
        return
    _mcp_export_filters.append(fn)
    logger.info(t("log.mcp.mcp_server_export_filter_register", p0=_callable_label(fn)))


def unregister_mcp_export_filter(fn: McpExportFilter) -> None:
    if fn in _mcp_export_filters:
        _mcp_export_filters.remove(fn)


def _should_export_tool(export_name: str, category: str, tool_base: ToolBase) -> bool:
    if not _mcp_export_filters:
        return True
    return any(fn(export_name, category, tool_base) for fn in _mcp_export_filters)


def _json_schema_type_to_python(param_schema: Mapping[str, object]) -> type:
    """JSON Schema 属性 → Python 注解；复杂联合类型退回 object。"""
    if "type" not in param_schema:
        return object
    raw = param_schema["type"]
    if isinstance(raw, list):
        non_null = [x for x in raw if x != "null"]
        if non_null and isinstance(non_null[0], str) and non_null[0] in _JSON_TYPE_MAP:
            return _JSON_TYPE_MAP[non_null[0]]
        return object
    if isinstance(raw, str) and raw in _JSON_TYPE_MAP:
        return _JSON_TYPE_MAP[raw]
    return object


def _token_matches_api_key(token: str, api_key: str) -> bool:
    """常量时间比较静态 api_key。"""
    if not api_key:
        return False
    left = token.encode("utf-8")
    right = api_key.encode("utf-8")
    return len(left) == len(right) and hmac.compare_digest(left, right)


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
            claims={"auth": "anonymous", "user_id": "mcp_anonymous", "user_pm": _DEFAULT_USER_PM},
        )

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        required = _auth_is_required(self._api_key)

        if not token:
            return None if required else self._anonymous_token("")

        if _token_matches_api_key(token, self._api_key):
            return AccessToken(
                token=token,
                client_id="mcp_service",
                scopes=["mcp:tools"],
                claims={"auth": "api_key", "user_id": "mcp_service", "user_pm": 0},
            )

        for verifier in list(_mcp_token_verifiers):
            try:
                claims = await verifier(token)
            except Exception as e:
                # 插件校验器异常不得拖垮鉴权链；记日志后试下一个
                logger.warning(t("log.mcp.mcp_server_token_verifier_fail", p0=_callable_label(verifier), e=e))
                continue
            if not isinstance(claims, dict):
                continue
            if "user_id" not in claims:
                logger.warning(t("log.mcp.mcp_server_token_verifier_missing_user_id"))
                continue
            user_id = str(claims["user_id"])
            client_id = str(claims["client_id"]) if "client_id" in claims else f"user:{user_id}"
            out_claims: Dict[str, object] = dict(claims)
            if "auth" not in out_claims:
                out_claims["auth"] = "plugin"
            # 缺 user_pm 时写入最低权限，避免隐式 master
            if "user_pm" not in out_claims:
                out_claims["user_pm"] = _DEFAULT_USER_PM
            return AccessToken(token=token, client_id=client_id, scopes=["mcp:tools"], claims=out_claims)

        if not required:
            return self._anonymous_token(token)

        logger.warning(t("log.mcp.mcp_server_bearer_token_verification_fail"))
        return None


# ─── 全局状态 ───────────────────────────────────────────────────────────────

_mcp_server: Optional[FastMCP] = None
_server_task: Optional[asyncio.Task[None]] = None
_exported_tool_count: int = 0
_http_mounted: bool = False
# 常驻任务进出每一代 lifespan。请求任务不得调用 __aexit__
_mcp_lifespan_cm: _McpHttpRuntime | None = None
_mcp_mount_path: Optional[str] = None


def _identity_from_access_token() -> Dict[str, object]:
    """从当前 MCP 请求的 AccessToken.claims 取身份（结构由插件校验器约定）。"""
    # fastmcp 依赖仅在请求上下文中可用；stdio / 无上下文时回落匿名
    from fastmcp.server.dependencies import get_access_token

    try:
        at = get_access_token()
    except (LookupError, RuntimeError):
        at = None

    if at is None:
        return {"auth": "none", "user_id": "mcp_client", "user_pm": _DEFAULT_USER_PM}

    out: Dict[str, object] = dict(at.claims) if isinstance(at.claims, dict) else {}
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
    - ``X-MCP-Bot-Id`` → Event.bot_id（一般**不必传**；默认见 _build_run_context）
    - ``X-MCP-User-Id`` → Event.user_id（可信网关或开放模式下指定调用方用户 ID）
    """
    out: Dict[str, str] = {}
    from fastmcp.server.dependencies import get_http_request

    try:
        req = get_http_request()
    except (LookupError, RuntimeError):
        return out
    if req is None:
        return out
    headers = req.headers
    # Starlette Headers 大小写不敏感；用 in + [] 避免 .get 兜底风格
    for hdr, field in _HTTP_SESSION_HEADERS:
        if hdr in headers:
            out[field] = str(headers[hdr])
    return out


def _parse_user_pm(raw: object) -> int:
    """claims.user_pm → int；非法或缺失用最低权限。

    claims 来自 JSON，只有 int / str 两种数字形态；其余类型一律落最低权限（fail-closed），
    宁可少权限也不要把无法判定的值当成有效权限位。
    """
    if raw is None:
        return _DEFAULT_USER_PM
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return _DEFAULT_USER_PM
    return _DEFAULT_USER_PM


def _create_mock_event(
    text: str = "",
    command: str = "",
    *,
    user_id: str = "mcp_client",
    user_pm: int = _DEFAULT_USER_PM,
    bot_id: str = "",
    group_id: str = "",
) -> Event:
    """MCP 调用用的模拟 Event。bot_id / group_id 未给时不替插件填业务身份。"""
    ev = Event()
    ev.text = text
    ev.command = command
    ev.raw_text = f"{command} {text}".strip()
    ev.user_pm = user_pm
    ev.user_id = user_id
    ev.bot_self_id = "MCP_Server"
    if bot_id:
        ev.bot_id = bot_id
    if group_id:
        ev.group_id = group_id
        ev.user_type = "group"
    else:
        ev.user_type = "direct"
    return ev


MAX_MCP_IMAGES: int = 4
# base64 编码约膨胀 4/3，2MB 二进制 ≈ 2.7MB 文本；再乘 MAX_MCP_IMAGES 就是单次响应的上界
MAX_MCP_IMAGE_BYTES: int = 2 * 1024 * 1024

MessageType = GsMessage | List[GsMessage] | List[str] | str | bytes

# http(s) 由 Core 拉取；file:// 留在文本里，Core 不读适配器磁盘
_IMAGE_REF_PREFIXES = ("link://", "base64://", "data:image/", "http://", "https://")
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_FETCH_TIMEOUT_SECONDS = 12


def _sniff_image_format(data: bytes) -> Literal["png", "jpeg", "gif", "webp"] | None:
    """利用魔数判断图片格式（png/jpeg/gif/webp），无法识别则返回 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"GIF8"):
        return "gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "webp"
    return None


class _McpImageTooLarge(Exception):
    """解码后或读取中超过单张上限。"""


class _McpImageRejected(Exception):
    def __init__(self, ref: str) -> None:
        super().__init__(ref)
        self.ref = ref


def _short_image_ref(ref: str) -> str:
    text = unwrap_image_url(ref)
    if text.startswith("data:image/"):
        return "data:image"
    return text[:117] + "..." if len(text) > 120 else text


def _decode_data_image(uri: str) -> bytes:
    header, sep, payload = uri.partition(",")
    if sep != "," or ";base64" not in header.lower():
        raise ValueError("data uri 缺少 base64 载荷")
    return base64.b64decode(payload)


def _extra_str_list(extra: Mapping[str, object], key: str) -> List[str]:
    if key in extra:
        raw = extra[key]
        if isinstance(raw, list):
            return [str(x) for x in raw]
    return []


def _mutable_extra_list(extra: Dict[str, object], key: str) -> List[object]:
    if key not in extra or not isinstance(extra[key], list):
        fresh: List[object] = []
        extra[key] = fresh
        return fresh
    found = extra[key]
    assert isinstance(found, list)
    return found


def _append_distinct(parts: List[str], incoming: str) -> None:
    piece = incoming.strip()
    if piece and piece not in parts:
        parts.append(piece)


async def _read_capped(resp: httpx2.Response) -> bytes:
    chunks: List[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > MAX_MCP_IMAGE_BYTES:
            raise _McpImageTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_mcp_image(url: str) -> bytes:
    """拉取 http(s) 图片。超限、拒绝的主机和过多跳转都不把整段下完。"""
    current = url
    timeout = httpx2.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _hop in range(4):
            if reject_outbound_http_url(current) is not None:
                raise _McpImageRejected(current)
            try:
                async with asyncio.timeout(_FETCH_TIMEOUT_SECONDS):
                    async with client.stream("GET", current) as resp:
                        if resp.status_code in _REDIRECT_STATUS:
                            if "location" not in resp.headers:
                                raise _McpImageRejected(current)
                            current = urljoin(current, resp.headers["location"])
                            continue
                        resp.raise_for_status()
                        return await _read_capped(resp)
            except _McpImageRejected:
                raise
            except (TimeoutError, httpx2.HTTPError, httpx2.InvalidURL) as exc:
                # 这一跳的地址才是客户端能核对的引用，资源 ID 对不上原图
                raise _McpImageRejected(current) from exc
    raise _McpImageRejected(url)


async def _load_registered_image(resource_id: str) -> tuple[bytes, str]:
    """返回字节和可展示的引用。引用为空表示登记的就是字节或 base64。"""
    raw = RM.peek(resource_id)
    ref = ""
    if isinstance(raw, bytes):
        data = raw
    else:
        text = unwrap_image_url(raw)
        if text.startswith("file://"):
            raise _McpImageRejected(text)
        if text.startswith("base64://"):
            data = base64.b64decode(text[len("base64://") :])
        elif text.startswith("data:image/"):
            data = _decode_data_image(text)
        elif text.startswith(("http://", "https://")):
            if reject_outbound_http_url(text) is not None:
                raise _McpImageRejected(text)
            try:
                data = await _fetch_mcp_image(text)
            except _McpImageRejected:
                raise
            except (TimeoutError, httpx2.HTTPError, httpx2.InvalidURL) as exc:
                # 拉取函数被中断时仍把原地址写进结果，不退回资源 ID
                raise _McpImageRejected(text) from exc
            ref = text
        else:
            raise _McpImageRejected(text)
    if len(data) > MAX_MCP_IMAGE_BYTES:
        raise _McpImageTooLarge()
    return data, ref


def _extract_messages_for_mcp(message: MessageType) -> tuple[List[str], List[str]]:
    """从发送的消息中提取文本和 RM 资源 ID。魔数与外链校验在组装结果时做。"""
    texts: List[str] = []
    image_ids: List[str] = []

    def _process(item: GsMessage | str | bytes) -> None:
        if isinstance(item, bytes):
            image_ids.append(RM.register(item))
        elif isinstance(item, str):
            if item.strip().startswith(_IMAGE_REF_PREFIXES):
                image_ids.append(RM.register(item))
            else:
                texts.append(item)
        elif isinstance(item, GsMessage):
            if item.type == "image" and isinstance(item.data, (bytes, str)):
                image_ids.append(RM.register(item.data))
            elif item.type in ("text", "markdown", "template_markdown") and item.data is not None:
                texts.append(str(item.data))
            elif item.type == "at":
                texts.append(f"@{item.data}")
            elif item.type in _MSG_TYPE_LABELS:
                texts.append(_MSG_TYPE_LABELS[item.type])

    if isinstance(message, (list, tuple)):
        for sub in message:
            _process(sub)
    else:
        _process(message)

    return texts, image_ids


class _McpBot(_Bot):
    """无连接。出站内容只进本次 MCP 结果，不写历史，也不入发送队列。"""

    def __init__(self, _id: str, extra: Dict[str, object]) -> None:
        super().__init__(_id, ws=None)
        self._extra = extra

    async def target_send(
        self,
        message: MessageType,
        *args: object,
        **kwargs: object,
    ) -> Optional[List[str]]:
        texts, image_ids = _extract_messages_for_mcp(message)
        if texts:
            target_texts = _mutable_extra_list(self._extra, "mcp_texts")
            target_texts.extend(texts)
        if image_ids:
            target_images = _mutable_extra_list(self._extra, "mcp_image_ids")
            for iid in image_ids:
                if iid not in target_images:
                    target_images.append(iid)
        return None


def _create_mock_bot(ev: Event, extra: Optional[Dict[str, object]] = None) -> Bot:
    """MCP 调用用的模拟 Bot（拦截出站消息并转为 MCP 资源）。"""
    _bot = _McpBot(
        str(ev.bot_id or "MCP_Server"),
        extra if extra is not None else {"source": "mcp_server", "mcp_image_ids": [], "mcp_texts": []},
    )
    return Bot(_bot, ev)


def _resolve_delegated_user_pm(user_id: str) -> int:
    from gsuid_core.config import core_config

    masters = {str(m).strip() for m in core_config.get_config("masters")}
    superusers = {str(s).strip() for s in core_config.get_config("superusers")}

    if user_id in masters:
        return 0
    if user_id in superusers:
        return 1
    return _DEFAULT_USER_PM


def _clean_delegated_user_id(raw: str) -> str:
    val = raw.strip()
    return "" if val in _SYSTEM_MCP_USER_IDS else val


def _ident_str(ident: Dict[str, object], key: str, default: str = "") -> str:
    if key in ident and ident[key] is not None:
        return str(ident[key]).strip()
    return default


def _pick_session_field(key: str, sess: Dict[str, str], ident: Dict[str, object]) -> str:
    if key in sess and sess[key].strip():
        return sess[key].strip()
    return _ident_str(ident, key)


def _resolve_mcp_identity(
    ident: Dict[str, object],
    sess: Dict[str, str],
    arg_user_id: str = "",
) -> tuple[str, int, str, str]:
    auth_mode = _ident_str(ident, "auth", "unknown")
    user_id = _ident_str(ident, "user_id", "mcp_client")

    arg_uid = _clean_delegated_user_id(arg_user_id)
    header_uid = _clean_delegated_user_id(sess["user_id"]) if "user_id" in sess else ""
    delegated_uid = arg_uid or header_uid

    if auth_mode in ("api_key", "anonymous", "none") and delegated_uid:
        user_id = delegated_uid

    # 工具入参 arg_uid 由 LLM 填写，固定降为普通用户权限，仅请求头可查 masters
    if auth_mode == "api_key" and delegated_uid:
        if arg_uid and arg_uid != header_uid:
            user_pm = _DEFAULT_USER_PM
        else:
            user_pm = _resolve_delegated_user_pm(delegated_uid)
    elif "user_pm" in ident:
        user_pm = _parse_user_pm(ident["user_pm"])
    else:
        user_pm = _DEFAULT_USER_PM

    explicit_bot_id = _pick_session_field("bot_id", sess, ident)
    group_id = _pick_session_field("group_id", sess, ident)
    return (user_id, user_pm, explicit_bot_id, group_id)


def _build_run_context(tool_name: str, arg_user_id: str = "") -> RunContext[ToolContext]:
    """构造 RunContext：claims + 可选 HTTP 会话头 / 工具参数（不盲猜平台，未传则为 ""）。"""
    ident = _identity_from_access_token()
    sess = _http_session_overrides()
    user_id, user_pm, explicit_bot_id, group_id = _resolve_mcp_identity(ident, sess, arg_user_id=arg_user_id)

    bot_id = explicit_bot_id.split(":")[0] if explicit_bot_id else ""

    fake_ev = _create_mock_event(
        command=tool_name,
        user_id=user_id,
        user_pm=user_pm,
        bot_id=bot_id,
        group_id=group_id,
    )
    if explicit_bot_id:
        fake_ev.real_bot_id = explicit_bot_id
    for enrich in _mcp_event_enrichers:
        enrich(fake_ev)

    # 框架键盖在 claims 之后，校验器同名键不能改 source / 出图桶
    passthrough_sess = {k: v for k, v in sess.items() if k not in ("user_id", "bot_id", "group_id")}
    extra: Dict[str, object] = {
        **ident,
        **passthrough_sess,
        "user_id": user_id,
        "user_pm": user_pm,
        "source": "mcp_server",
        "mcp_image_ids": [],
        "mcp_texts": [],
    }
    if bot_id:
        extra["bot_id"] = bot_id
    if group_id:
        extra["group_id"] = group_id
    mock_bot = _create_mock_bot(fake_ev, extra)
    deps = ToolContext(bot=mock_bot, ev=fake_ev, extra=extra)
    return RunContext(
        deps=deps,
        model=TestModel(),
        usage=RunUsage(),
        tool_name=tool_name,
    )


def _is_tool_return(item: object) -> TypeGuard[ToolReturn[object]]:
    """判断对象是否为 pydantic_ai 的 ToolReturn。"""
    return isinstance(item, ToolReturn)


def _format_tool_result(result: object) -> str:
    """把 @ai_tools 返回值收成 MCP 可传的纯文本。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if _is_tool_return(result):
        parts: List[str] = []
        if result.return_value is not None:
            parts.append(str(result.return_value))
        content = result.content
        if content is not None:
            if isinstance(content, str):
                parts.append(content)
            else:
                for item in content:
                    if isinstance(item, BinaryContent):
                        continue
                    parts.append(str(item))
        if parts:
            return "\n".join(parts)
        return ""
    return str(result)


async def _build_tool_result(
    raw: object,
    image_ids: List[str],
    run_ctx: Optional[RunContext[ToolContext]] = None,
) -> ToolResult | str:
    """构建标准 MCP ToolResult 或纯文本字符串。若无图片则保持旧行为返回纯文本，有图片则返回 ToolResult。"""
    text = _format_tool_result(raw)

    all_texts: List[str] = []
    if run_ctx is not None:
        for t_str in _extra_str_list(run_ctx.deps.extra, "mcp_texts"):
            _append_distinct(all_texts, t_str)
    _append_distinct(all_texts, text)

    loaded: List[tuple[bytes, str]] = []
    if _is_tool_return(raw) and raw.content and not isinstance(raw.content, str):
        for item in raw.content:
            if isinstance(item, BinaryContent):
                loaded.append((item.data, ""))

    failed_refs: List[str] = []
    skipped_refs: List[str] = []
    valid_images: List[tuple[bytes, Literal["png", "jpeg", "gif", "webp"]]] = []
    skipped_binary = 0
    has_oversized = False
    has_overcounted = False

    def _take(data: bytes, ref: str) -> None:
        nonlocal skipped_binary, has_oversized
        if len(data) > MAX_MCP_IMAGE_BYTES:
            has_oversized = True
            return
        fmt = _sniff_image_format(data)
        if fmt is None:
            skipped_binary += 1
            if ref:
                skipped_refs.append(_short_image_ref(ref))
            return
        valid_images.append((data, fmt))

    for img_bytes, ref in loaded:
        if len(valid_images) >= MAX_MCP_IMAGES:
            has_overcounted = True
            break
        _take(img_bytes, ref)

    if image_ids and len(valid_images) >= MAX_MCP_IMAGES:
        has_overcounted = True
    elif len(valid_images) < MAX_MCP_IMAGES:
        for iid in image_ids:
            if len(valid_images) >= MAX_MCP_IMAGES:
                has_overcounted = True
                break
            try:
                data, ref = await _load_registered_image(iid)
            except _McpImageTooLarge:
                has_oversized = True
                continue
            except (TimeoutError, httpx2.HTTPError, httpx2.InvalidURL, ValueError, _McpImageRejected) as e:
                # 外链超时、拒绝或解码失败都是外部输入，不能丢掉已经拿到的文本和图片
                logger.warning(t("log.mcp.mcp_server_retrieve_image_resource_fail", rid=iid, e=e))
                shown = e.ref if isinstance(e, _McpImageRejected) else iid
                failed_refs.append(_short_image_ref(shown))
                continue
            _take(data, ref)

    if skipped_binary:
        suffix = f": {', '.join(skipped_refs)}" if skipped_refs else ""
        all_texts.append(f"[{skipped_binary} 份非图片二进制未附带{suffix}]")
    if failed_refs:
        all_texts.append("[取回失败未附带: " + ", ".join(failed_refs) + "]")
    if has_oversized:
        all_texts.append("[图片过大未附带]")
    if has_overcounted:
        all_texts.append("[部分图片超出数量上限未附带]")

    final_text = "\n".join(all_texts)

    # 无图片走旧路径：直接返回 str，客户端拿到的 content 与 structuredContent 均与改动前一致
    if not valid_images:
        return final_text

    contents: list[mcp_types.TextContent | mcp_types.ImageContent] = []
    if final_text:
        contents.append(mcp_types.TextContent(type="text", text=final_text))
    for img_bytes, fmt in valid_images:
        contents.append(FastMcpImage(data=img_bytes, format=fmt).to_image_content())

    # 注解仍声明为 str，outputSchema 就还在；带图返回时必须补上 structuredContent，否则客户端校验 schema 失败
    return ToolResult(content=contents, structured_content={"result": final_text})


def _docstring_declares_user_id(doc: str) -> bool:
    """判断 docstring 的 Args 块是否声明了 user_id 参数。"""
    in_args = False
    args_indent = 0
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if not in_args:
            if stripped in ("Args:", "Arguments:", "Parameters:"):
                in_args = True
                args_indent = indent
            continue
        if indent <= args_indent and stripped.endswith(":"):
            in_args = False
            continue
        head, sep, _ = stripped.partition(":")
        if sep == ":" and head.split("(")[0].strip() == "user_id":
            return True
    return False


def _build_ai_tool_handler(
    tool_base: ToolBase,
    category: str,
) -> Callable[..., Awaitable[ToolResult | str]]:
    """按 ToolBase 的 function_schema 动态生成 MCP handler（签名对客户端可见）。"""
    tool = tool_base.tool
    tool_name = tool_base.name
    description = tool_base.description or tool_name
    json_schema = tool.function_schema.json_schema

    raw_props = json_schema["properties"] if isinstance(json_schema, dict) and "properties" in json_schema else None
    properties: Dict[str, object] = raw_props if isinstance(raw_props, dict) else {}
    raw_req = json_schema["required"] if isinstance(json_schema, dict) and "required" in json_schema else None
    required_fields = [x for x in raw_req if isinstance(x, str)] if isinstance(raw_req, list) else []

    annotations: Dict[str, object] = {}
    params: List[inspect.Parameter] = []

    for param_name, raw_schema in properties.items():
        param_schema: Mapping[str, object] = raw_schema if isinstance(raw_schema, dict) else {}
        py_type = _json_schema_type_to_python(param_schema)
        has_default = "default" in param_schema
        if param_name in required_fields and not has_default:
            ann: object = py_type
            default_val: object = inspect.Parameter.empty
        else:
            ann = Optional[py_type]
            default_val = param_schema["default"] if has_default else None
        annotations[param_name] = ann
        params.append(
            inspect.Parameter(
                param_name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=ann,
                default=default_val,
            )
        )

    inject_user_id = "user_id" not in properties and _docstring_declares_user_id(description)
    if inject_user_id:
        annotations["user_id"] = Optional[str]
        params.append(
            inspect.Parameter(
                "user_id",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=Optional[str],
                default=None,
            )
        )

    async def handler(**kwargs: object) -> ToolResult | str:
        call_args = dict(kwargs)
        arg_user_id = ""
        if inject_user_id:
            raw_uid = call_args.pop("user_id", None)
            if isinstance(raw_uid, str):
                arg_user_id = raw_uid
        logger.info(
            t(
                "log.mcp.mcp_server_calling_ai_tool",
                tool_name=tool_name,
                category=category,
                call_args=repr(call_args)[:500],
            )
        )
        run_ctx = _build_run_context(tool_name, arg_user_id=arg_user_id)
        try:
            raw = await tool.function(run_ctx, **call_args)
        except Exception as e:
            logger.error(t("log.mcp.mcp_server_ai_tool_call_fail", tool_name=tool_name, e=e))
            # 工具中途抛错时前面 bot.send 的图已经生成，直接丢弃等于白跑一趟
            raw = f"❌ 工具 [{tool_name}] 执行异常: {e}"
        image_ids = _extra_str_list(run_ctx.deps.extra, "mcp_image_ids")
        return await _build_tool_result(raw, image_ids, run_ctx)

    handler.__name__ = tool_name
    handler.__doc__ = description
    handler.__qualname__ = f"mcp_server.{tool_name}"
    handler.__module__ = "gsuid_core.ai_core.mcp.server"
    # 下面两行是给 fastmcp 看的「假面」：注解留在 str，outputSchema 与 structuredContent 才不会被抽掉
    # （报成 ToolResult 会让 fastmcp 判定为不可序列化，纯文本工具的 schema 也一起丢）
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
        if not _should_export_tool(export_name, category, tool_base):
            continue
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
            logger.error(t("log.mcp.mcp_server_register_name_tool_fail", tool_name=export_name, e=e))

    _exported_tool_count = registered_count
    logger.info(t("log.mcp.mcp_server_registered_ai_tools_register", registered_count=registered_count, p0=len(tools)))
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


# ─── HTTP 子应用护栏：隔离取消 + 未写出响应时同请求重试 ─────────────────────

#: 重建冷却（秒）：毒化后每个失败请求都想重建，串行 + 冷却避免抖动
_MCP_REBUILD_COOLDOWN = 5.0

_mcp_rebuild_lock = asyncio.Lock()
_mcp_last_rebuild: float = 0.0


def _replacement_mcp_app(used_inner: object) -> _ASGICallable | None:
    """重建之后的内层应用。仍是这一次用的那个，或还不是 ASGI，则不能重放。"""
    runtime = _mcp_lifespan_cm
    if runtime is None:
        return None
    fresh: object = runtime.current_app()
    if fresh is None or fresh is used_inner or not _is_fresh_asgi(fresh):
        return None
    return fresh


class McpHttpGuard:
    """MCP HTTP 子应用护栏：隔离上层取消，未写出的响应在本次请求内重试。

    终身 task group 被取消时，常驻任务还停在这一代 lifespan 里，anyio 会取消它。
    它退出这一代并进入下一代。状态码先扣住，没写到客户端就结束时，同一次请求交给
    新的内层应用。正文已经发出但没有结束块时，补一个空结束块；这一代会重建，
    重放后仍不完整则只补结束块。只是写出响应失败、子应用正常返回时，不重建。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        exchange = _HttpExchange(receive, send)
        inner_before = _snapshot_inner(self._app)

        async def run_used() -> None:
            await self._app(scope, exchange.receive, exchange.send)

        with anyio.CancelScope(shield=True):
            failed = await _capture_mcp_failure(run_used)
            if exchange.client_gone:
                # 写出失败已吞掉。子应用正常返回时会话还在，不重建。
                if failed:
                    await _rebuild_http_mcp_session()
                return
            if exchange.finished and not failed:
                return
            if exchange.finished:
                await _rebuild_http_mcp_session()
                return
            if exchange.committed:
                logger.warning(t("log.mcp.mcp_server_http_incomplete"))
                await _finish_open_http_response(send)
                await _rebuild_http_mcp_session()
                return

            await _rebuild_http_mcp_session()
            fresh = _replacement_mcp_app(inner_before)
            if fresh is not None:
                logger.warning(t("log.mcp.mcp_server_http_retry"))
                exchange.begin_replay()

                async def run_fresh() -> None:
                    await fresh(scope, exchange.receive, exchange.send)

                failed = await _capture_mcp_failure(run_fresh)
                if exchange.client_gone or exchange.finished:
                    return
                if exchange.committed:
                    logger.warning(t("log.mcp.mcp_server_http_retry_incomplete"))
                    await _finish_open_http_response(send)
                    return

            if not failed:
                logger.warning(t("log.mcp.mcp_server_http_no_response"))
            try:
                await _respond_mcp_unavailable(scope, receive, send)
            except Exception as e:
                logger.debug(t("log.mcp.mcp_server_http_503_fail", e=e))


async def _rebuild_http_mcp_session() -> None:
    """保留挂载与工具，由所属任务顺序关闭旧会话并启动新会话。"""
    global _mcp_last_rebuild

    async with _mcp_rebuild_lock:
        runtime = _mcp_lifespan_cm
        if runtime is None or not _http_mounted:
            return
        now = time.monotonic()
        if now - _mcp_last_rebuild < _MCP_REBUILD_COOLDOWN:
            return
        _mcp_last_rebuild = now
        try:
            await runtime.restart()
        except Exception as error:
            # 停机已经把挂载标掉时，restart 的停止错误不必再记成重建失败。
            if _http_mounted:
                logger.error(t("log.mcp.mcp_server_http_rebuild_fail", e=error))
            return
        logger.warning(t("log.mcp.mcp_server_http_rebuilt"))


async def _mount_http_mcp(server: FastMCP, path: str) -> None:
    """就绪后挂载。lifespan 的进入、恢复和退出由同一任务负责。

    主 FastAPI 的 lifespan 早已 yield，晚挂载不会自己跑子 lifespan。子应用内部路由
    挂在 ``/``，对外路径由 mount 前缀构成（如 ``/api/mcp``）。
    """
    global _http_mounted, _mcp_lifespan_cm, _mcp_mount_path

    if _http_mounted or _mcp_lifespan_cm is not None:
        logger.warning(t("log.mcp.mcp_server_http_already_mounted"))
        return

    from gsuid_core.app_life import app as main_app

    runtime = _McpHttpRuntime(server)
    # 入口可能等待。先登记，关闭钩子才能停它。
    _mcp_lifespan_cm = runtime
    ready = False
    try:
        await runtime.start()
        ready = True
    except Exception as error:
        logger.error(t("log.mcp.mcp_server_lifespan_enter_fail", e=error))
        raise
    finally:
        if not ready:
            try:
                with anyio.CancelScope(shield=True):
                    await runtime.close()
            finally:
                if _mcp_lifespan_cm is runtime:
                    _mcp_lifespan_cm = None

    # 不用 app.mount：它的正则要求前缀后带斜杠，裸路径会被 307。
    main_app.router.routes.append(_McpSlashMount(path, app=McpHttpGuard(runtime)))
    _mcp_lifespan_cm = runtime
    _mcp_mount_path = path
    _http_mounted = True
    logger.info(t("log.mcp.mcp_server_http_mounted", path=path))


def _log_auth_mode(api_key: str) -> None:
    """启动时区分 open / 静态 key / 插件校验器 / 并存。"""
    has_key = bool(api_key)
    has_verifiers = bool(_mcp_token_verifiers)
    if has_key and has_verifiers:
        logger.info(t("log.mcp.mcp_server_auth_static_and_plugin", p0=len(_mcp_token_verifiers)))
    elif has_key:
        logger.info(t("log.mcp.mcp_server_bearer_token_authentication_ok"))
    elif has_verifiers:
        logger.info(t("log.mcp.mcp_server_auth_plugin_only", p0=len(_mcp_token_verifiers)))
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
    global _mcp_lifespan_cm, _mcp_mount_path

    # 先停所属任务。重建可能正持锁，等新一代入口就绪。
    _http_mounted = False
    cm, _mcp_lifespan_cm = _mcp_lifespan_cm, None
    await _close_lifespan_cm(cm)

    async with _mcp_rebuild_lock:
        if _mcp_mount_path is not None:
            from gsuid_core.app_life import app as main_app

            _remove_mounted_path(main_app, _mcp_mount_path)
            logger.info(t("log.mcp.mcp_server_http_unmounted", path=_mcp_mount_path))
            _mcp_mount_path = None

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
    # stdio 用 _server_task。HTTP 的常驻任务在 _McpHttpRuntime 里。
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
