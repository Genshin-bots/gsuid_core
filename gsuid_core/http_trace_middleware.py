"""纯 ASGI 中间件：为 `/api` HTTP 请求绑定 http_trace_id。不读 body。"""

from __future__ import annotations

import json
import time
import uuid
import asyncio
from typing import Optional
from urllib.parse import parse_qsl

from starlette.types import Send, Scope, ASGIApp, Message, Receive

import gsuid_core.logger as _gslog
from gsuid_core.models import HttpTraceContext
from gsuid_core.utils.secret_mask import mask_mapping, is_secret_key_name

_PATH_MAX = 2048
_QUERY_MAX = 512
_IP_MAX = 64
_REQUEST_ID_MAX = 128
_CONTENT_LENGTH_DIGITS_MAX = 16
_PREVIEW_BYTES = 8192
_PREVIEW_CHARS = 4096

_API_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})
_QUERY_SECRET_EXTRA = frozenset({"token", "access_token", "code", "key", "api_key"})
# 控制台轮询 / 自指 / 探活 / 读图：记下来没有排障价值，只会淹没业务 /api。
_EXCLUDE_PREFIXES = (
    "/api/traces",
    "/api/http-traces",
    "/api/logs",
    "/api/dashboard",
    "/api/version",
)
_EXCLUDE_EXACT_OR_PREFIX = ("/api/v1/agent/chat/stream",)
_EXCLUDE_EXACT = frozenset({"/api/system/health", "/api/v1/agent/health"})
# 仅 GET：壳子、看板刷新、静态读图。同路径 POST/PUT 仍记。
_GET_EXCLUDE_EXACT = frozenset(
    {
        "/api/auth/me",
        "/api/auth/pubkey",
        "/api/auth/admin/exists",
        "/api/brand",
        "/api/brand/icon",
        "/api/theme/config",
        "/api/theme/presets",
        "/api/assets/preview",
        "/api/system/info",
        "/api/plugins/list",
        "/api/plugin-pages",
        "/api/persona/list",
        "/api/persona/config/global",
        "/api/persona/heartbeat/status",
        "/api/ai/wizard/status",
        "/api/ai/kanban/board",
        "/api/ai/approvals/list",
        "/api/live-chat/bootstrap",
        "/api/live-chat/state",
        "/api/scheduler/jobs",
        "/api/git-update/status",
        "/api/ai/budget/overview",
    }
)
_GET_EXCLUDE_PREFIXES = (
    "/api/auth/avatar",
    "/api/plugins/icon",
    "/api/getImage",
    "/api/image",
    "/api/meme/image",
    "/api/ops",
    "/api/git-update/status",
    "/api/ai/budget/usage",
    "/api/ai/statistics",
    "/api/ai/performance",
)
_GET_PERSONA_MEDIA_PREFIX = "/api/persona/"
_GET_PERSONA_MEDIA_SUFFIXES = ("/avatar", "/image", "/audio")


def norm_http_path(raw: str) -> str:
    path = raw.rstrip("/") or "/"
    if len(path) > _PATH_MAX:
        return path[:_PATH_MAX]
    return path


def is_api_http_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _runtime_mcp_path() -> str:
    from gsuid_core.ai_core.mcp.server import _DEFAULT_MCP_PATH, _normalize_mcp_path
    from gsuid_core.ai_core.configs.ai_config import mcp_server_config

    raw = mcp_server_config.get_config("mcp_server_path").data
    text = str(raw) if raw else _DEFAULT_MCP_PATH
    return _normalize_mcp_path(text)


def _matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _is_get_persona_media(path: str) -> bool:
    if not path.startswith(_GET_PERSONA_MEDIA_PREFIX):
        return False
    for suffix in _GET_PERSONA_MEDIA_SUFFIXES:
        if path.endswith(suffix):
            return True
    return False


def is_http_trace_excluded(path: str, method: str = "GET") -> bool:
    if path in _EXCLUDE_EXACT:
        return True
    for prefix in _EXCLUDE_PREFIXES:
        if _matches_prefix(path, prefix):
            return True
    for prefix in _EXCLUDE_EXACT_OR_PREFIX:
        if _matches_prefix(path, prefix):
            return True
    mcp = _runtime_mcp_path()
    if _matches_prefix(path, mcp):
        return True
    if method == "GET":
        if path in _GET_EXCLUDE_EXACT:
            return True
        for prefix in _GET_EXCLUDE_PREFIXES:
            if _matches_prefix(path, prefix):
                return True
        if _is_get_persona_media(path):
            return True
    return False


def _truncate_query(text: str) -> str:
    if len(text) <= _QUERY_MAX:
        return text
    cut = text[:_QUERY_MAX]
    dangling = cut.endswith("%") or (len(cut) >= 2 and cut[-2] == "%")
    if dangling:
        amp = cut.rfind("&")
        if amp >= 0:
            return cut[:amp]
    return cut


def redact_query_string(query_string: bytes) -> str:
    if not query_string:
        return ""
    raw = query_string.decode("latin-1")
    parts: list[str] = []
    for key, value in parse_qsl(raw, keep_blank_values=True):
        lowered = key.lower()
        if is_secret_key_name(key) or lowered in _QUERY_SECRET_EXTRA:
            parts.append(f"{key}=****")
        else:
            parts.append(f"{key}={value}")
    return _truncate_query("&".join(parts))


def _header_value(scope: Scope, name: bytes) -> Optional[bytes]:
    headers = scope["headers"]
    if not isinstance(headers, list):
        return None
    for item in headers:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        h_name = item[0]
        h_value = item[1]
        if h_name == name:
            if isinstance(h_value, (bytes, bytearray)):
                return bytes(h_value)
            return None
    return None


def _content_length(scope: Scope) -> Optional[int]:
    raw = _header_value(scope, b"content-length")
    if raw is None:
        return None
    text = raw.decode("latin-1")
    if not text.isdigit() or len(text) > _CONTENT_LENGTH_DIGITS_MAX:
        return None
    return int(text)


def _client_request_id(scope: Scope) -> Optional[str]:
    raw = _header_value(scope, b"x-request-id")
    if raw is None:
        return None
    text = raw.decode("latin-1")
    if not text or len(text) > _REQUEST_ID_MAX:
        return None
    if not text.isprintable():
        return None
    return text


def _client_ip(scope: Scope) -> str:
    if "client" not in scope or scope["client"] is None:
        return ""
    client = scope["client"]
    if not isinstance(client, (tuple, list)) or len(client) < 1:
        return ""
    host = client[0]
    if not isinstance(host, str):
        return ""
    if len(host) > _IP_MAX:
        return host[:_IP_MAX]
    return host


def _user_from_scope(scope: Scope) -> tuple[Optional[str], Optional[str]]:
    from gsuid_core.webconsole.web_api import verify_token

    raw_auth = _header_value(scope, b"authorization")
    authorization = raw_auth.decode("latin-1") if raw_auth is not None else None
    token_q: Optional[str] = None
    if "query_string" in scope and isinstance(scope["query_string"], (bytes, bytearray)):
        qs = bytes(scope["query_string"]).decode("latin-1")
        for key, value in parse_qsl(qs, keep_blank_values=True):
            if key == "token":
                token_q = value
                break
    rec = verify_token(authorization, token_q)
    if rec is None:
        return None, None
    if "user" not in rec:
        return None, None
    user = rec["user"]
    if not isinstance(user, dict):
        return None, None
    uid: Optional[str] = None
    name: Optional[str] = None
    if "id" in user and isinstance(user["id"], str):
        uid = user["id"]
    if "name" in user and isinstance(user["name"], str):
        name = user["name"]
    return uid, name


def _build_context(scope: Scope) -> HttpTraceContext:
    trace_id = str(uuid.uuid4())
    method_raw = scope["method"] if "method" in scope else "GET"
    method = str(method_raw).upper()
    if method not in _API_METHODS:
        method = str(method_raw).upper()
    path_raw = scope["path"] if "path" in scope else "/"
    qs = scope["query_string"] if "query_string" in scope else b""
    query_bytes = bytes(qs) if isinstance(qs, (bytes, bytearray)) else b""
    user_id, user_name = _user_from_scope(scope)
    return HttpTraceContext(
        trace_id=trace_id,
        short_id=trace_id[:8],
        method=method,
        path=norm_http_path(str(path_raw)),
        client_ip=_client_ip(scope),
        user_id=user_id,
        user_name=user_name,
        start_time=time.perf_counter(),
        start_ts=time.time(),
        content_length=_content_length(scope),
        query_redacted=redact_query_string(query_bytes),
        client_request_id=_client_request_id(scope),
    )


def _with_trace_header(message: Message, trace_id: str) -> Message:
    copied: Message = dict(message)
    raw_headers = message["headers"] if "headers" in message else []
    headers: list[tuple[bytes, bytes]] = []
    if isinstance(raw_headers, list):
        for item in raw_headers:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                name = item[0]
                value = item[1]
                if isinstance(name, (bytes, bytearray)) and isinstance(value, (bytes, bytearray)):
                    headers.append((bytes(name), bytes(value)))
    headers.append((b"x-http-trace-id", trace_id.encode("ascii")))
    copied["headers"] = headers
    return copied


def _response_content_type(message: Message) -> str:
    if "headers" not in message:
        return ""
    headers = message["headers"]
    if not isinstance(headers, list):
        return ""
    for item in headers:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        name = item[0]
        value = item[1]
        if name == b"content-type" and isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("latin-1")
    return ""


def _should_capture_preview(content_type: str) -> bool:
    low = content_type.lower()
    if "text/event-stream" in low:
        return False
    if "image/" in low or "audio/" in low or "video/" in low:
        return False
    if "octet-stream" in low or "font/" in low:
        return False
    return True


def preview_response_body(raw: bytes) -> str:
    """截断响应正文；JSON 按键名脱敏，避免把 api_key 写进 traces。"""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            text = json.dumps(mask_mapping(parsed), ensure_ascii=False)
        elif isinstance(parsed, list):
            masked: list[object] = []
            for item in parsed:
                if isinstance(item, dict):
                    masked.append(mask_mapping(item))
                else:
                    masked.append(item)
            text = json.dumps(masked, ensure_ascii=False)
    if len(text) > _PREVIEW_CHARS:
        return text[:_PREVIEW_CHARS] + "…"
    return text


def _is_event_stream(message: Message) -> bool:
    if "headers" not in message:
        return False
    headers = message["headers"]
    if not isinstance(headers, list):
        return False
    for item in headers:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        name = item[0]
        value = item[1]
        if name == b"content-type" and isinstance(value, (bytes, bytearray)):
            if b"text/event-stream" in bytes(value).lower():
                return True
    return False


class HttpTraceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path_raw = scope["path"] if "path" in scope else "/"
        path = norm_http_path(str(path_raw))
        method_raw = scope["method"] if "method" in scope else "GET"
        method = str(method_raw).upper()
        if not is_api_http_path(path) or is_http_trace_excluded(path, method):
            await self.app(scope, receive, send)
            return

        ctx = _build_context(scope)
        _gslog.bind_http_trace_context(ctx)
        _gslog.http_trace_collector.start_trace(ctx)
        finalized = False
        cancelled = False
        status_code = 500
        capture_preview = True
        body_chunks: list[bytes] = []
        body_taken = 0

        async def receive_wrapper() -> Message:
            nonlocal cancelled
            message = await receive()
            if message["type"] == "http.disconnect":
                cancelled = True
            return message

        def _finish(code: int) -> None:
            nonlocal finalized
            if finalized:
                return
            _gslog.http_trace_collector.finalize_trace(ctx.trace_id, code)
            _gslog.clear_http_trace_context()
            finalized = True

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, capture_preview, body_taken
            outgoing = message
            if message["type"] == "http.response.start":
                status_code = 499 if cancelled else int(message["status"])
                outgoing = _with_trace_header(message, ctx.trace_id)
                ctype = _response_content_type(outgoing)
                ctx.response_content_type = ctype if ctype else None
                capture_preview = _should_capture_preview(ctype)
                if _is_event_stream(outgoing):
                    _finish(status_code)
            if message["type"] == "http.response.body" and capture_preview and body_taken < _PREVIEW_BYTES:
                raw_body = message["body"] if "body" in message else b""
                if isinstance(raw_body, (bytes, bytearray)):
                    chunk = bytes(raw_body)
                    need = _PREVIEW_BYTES - body_taken
                    if need > 0 and chunk:
                        body_chunks.append(chunk[:need])
                        body_taken += min(len(chunk), need)
            await send(outgoing)
            if outgoing["type"] == "http.response.body" and (
                "more_body" not in outgoing or outgoing["more_body"] is False
            ):
                if capture_preview:
                    ctx.response_preview = preview_response_body(b"".join(body_chunks))
                _finish(499 if cancelled else status_code)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except asyncio.CancelledError:
            _finish(499)
            raise
        except BaseException:
            if not finalized:
                _finish(499 if cancelled else 500)
            raise
        finally:
            if not finalized:
                _finish(499 if cancelled else status_code)
