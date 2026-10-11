"""网页控制台跨域。

只放行 ``core_config.web_cors_origins`` 里的源。前端自定义 API Host 会带
``credentials: include``，所以不能回 ``Access-Control-Allow-Origin: *``，
必须回显请求里的那个 Origin，并带 ``Access-Control-Allow-Credentials: true``。
名单为空时不改响应，同源 ``/app/`` 与开发代理保持原样。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.types import Send, Scope, ASGIApp, Message, Receive

_ALLOW_METHODS = b"GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
# RFC 7230 token
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+\-.0-9A-Z^_`a-z|~]+$")
_DEFAULT_ALLOW_HEADERS = b"authorization, content-type"
_MAX_REQUEST_HEADERS = 32
_MAX_ORIGIN_LEN = 256


def normalize_cors_origin(value: str) -> str | None:
    """收成浏览器 Origin：``scheme://host[:port]``。路径、默认端口、``*`` 都丢掉。"""
    text = value.strip()
    if not text or len(text) > _MAX_ORIGIN_LEN:
        return None
    if any(ch in text for ch in "\r\n\x00"):
        return None
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname.lower()
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    return f"{scheme}://{netloc}"


def parse_web_cors_origins(raw: object) -> frozenset[str]:
    if isinstance(raw, str):
        items: list[object] = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        return frozenset()
    origins: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        origin = normalize_cors_origin(item)
        if origin is not None:
            origins.add(origin)
    return frozenset(origins)


def load_web_cors_origins() -> frozenset[str]:
    from gsuid_core.config import core_config

    return parse_web_cors_origins(core_config.get_config("web_cors_origins"))


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1").strip()
    return ""


def _allow_headers_value(raw: str) -> bytes:
    parts: list[str] = []
    for piece in raw.split(","):
        token = piece.strip()
        if not token or not _HEADER_TOKEN.fullmatch(token):
            continue
        parts.append(token.lower())
        if len(parts) >= _MAX_REQUEST_HEADERS:
            break
    if not parts:
        return _DEFAULT_ALLOW_HEADERS
    return ", ".join(parts).encode("ascii")


def _apply_cors_headers(headers: list[tuple[bytes, bytes]], origin: str) -> None:
    origin_bytes = origin.encode("ascii")

    def upsert(name: bytes, value: bytes) -> None:
        for index, (key, _) in enumerate(headers):
            if key.lower() == name:
                headers[index] = (key, value)
                return
        headers.append((name, value))

    upsert(b"access-control-allow-origin", origin_bytes)
    upsert(b"access-control-allow-credentials", b"true")
    for index, (key, value) in enumerate(headers):
        if key.lower() != b"vary":
            continue
        existing = value.decode("latin-1")
        names = {part.strip().lower() for part in existing.split(",") if part.strip()}
        if "origin" not in names:
            headers[index] = (key, f"{existing}, Origin".encode("latin-1"))
        return
    headers.append((b"vary", b"Origin"))


class WebCorsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        allowed = load_web_cors_origins()
        if not allowed:
            await self.app(scope, receive, send)
            return

        origin = normalize_cors_origin(_header(scope, b"origin"))
        if origin is None or origin not in allowed:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        requested = _header(scope, b"access-control-request-method").upper()
        if method == "OPTIONS" and requested:
            if requested not in _ALLOWED_METHODS:
                await self.app(scope, receive, send)
                return
            allow_headers = _allow_headers_value(_header(scope, b"access-control-request-headers"))
            header_list: list[tuple[bytes, bytes]] = [
                (b"access-control-allow-methods", _ALLOW_METHODS),
                (b"access-control-allow-headers", allow_headers),
                (b"access-control-max-age", b"600"),
            ]
            _apply_cors_headers(header_list, origin)
            await send({"type": "http.response.start", "status": 204, "headers": header_list})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = message.get("headers") or []
                headers = [(key, value) for key, value in raw]
                _apply_cors_headers(headers, origin)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)
