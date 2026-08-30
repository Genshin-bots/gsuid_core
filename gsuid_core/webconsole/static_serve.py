"""Hub dist 静态资源：预压缩协商、长期缓存、缺失资源不要回落成 HTML。"""

from __future__ import annotations

import mimetypes
from typing import Optional
from pathlib import Path

from fastapi import Request, APIRouter
from fastapi.responses import Response, FileResponse, HTMLResponse

from gsuid_core.utils.path_safety import PathEscapeError, safe_join

MIME_TYPES = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".cjs": "application/javascript",
    ".css": "text/css",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
    ".otf": "font/otf",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".txt": "text/plain",
    ".wasm": "application/wasm",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
}

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_NO_CACHE = "no-cache"
CACHE_SHORT = "public, max-age=3600"

_NO_CACHE_NAMES = frozenset({"index.html", "version.json"})
_HTML_SUFFIXES = frozenset({".html", ".htm"})
_ENCODING_EXT = (("br", ".br"), ("gzip", ".gz"))


def get_mime_type(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    mime_type = MIME_TYPES.get(suffix)
    if mime_type is None:
        mime_type, _ = mimetypes.guess_type(str(file_path))
    return mime_type or "application/octet-stream"


def cache_control_for_relpath(rel_path: str) -> str:
    normalized = rel_path.replace("\\", "/").lstrip("/")
    name = normalized.rsplit("/", 1)[-1].lower()
    suffix = Path(name).suffix.lower()
    if name in _NO_CACHE_NAMES or suffix in _HTML_SUFFIXES:
        return CACHE_NO_CACHE
    if normalized.startswith("assets/"):
        return CACHE_IMMUTABLE
    return CACHE_SHORT


def parse_accept_encoding(header: Optional[str]) -> dict[str, float]:
    if not header:
        return {}
    result: dict[str, float] = {}
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, param = part.partition(";")
        name = name.strip().lower()
        q = 1.0
        param = param.strip().lower()
        if param.startswith("q="):
            try:
                q = float(param[2:].strip())
            except ValueError:
                q = 0.0
        result[name] = q
    return result


def select_encoded_file(file_path: Path, accept_encoding: Optional[str]) -> tuple[Path, Optional[str]]:
    q = parse_accept_encoding(accept_encoding)
    for encoding, ext in _ENCODING_EXT:
        if q.get(encoding, 0.0) <= 0:
            continue
        encoded = Path(str(file_path) + ext)
        if encoded.is_file():
            return encoded, encoding
    return file_path, None


def should_spa_fallback(rel_path: str) -> bool:
    suffix = Path(rel_path.replace("\\", "/")).suffix.lower()
    if not suffix or suffix in _HTML_SUFFIXES:
        return True
    return suffix not in MIME_TYPES


def static_file_headers(
    rel_path: str,
    content_encoding: Optional[str] = None,
) -> dict[str, str]:
    headers = {
        "Cache-Control": cache_control_for_relpath(rel_path),
        "Vary": "Accept-Encoding",
    }
    if content_encoding:
        headers["Content-Encoding"] = content_encoding
    return headers


def static_file_response(
    file_path: Path,
    *,
    rel_path: str,
    accept_encoding: Optional[str],
    media_type: Optional[str] = None,
) -> FileResponse:
    serve_path, encoding = select_encoded_file(file_path, accept_encoding)
    return FileResponse(
        path=str(serve_path),
        media_type=media_type or get_mime_type(file_path),
        headers=static_file_headers(rel_path, encoding),
    )


def build_frontend_router(dist_path: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_model=None)
    @router.get("/{path:path}", response_model=None)
    async def serve_frontend(request: Request, path: str = "") -> Response:
        accept_encoding = request.headers.get("accept-encoding")
        if not path or path == "/":
            index_path = dist_path / "index.html"
            if index_path.exists():
                return static_file_response(
                    index_path,
                    rel_path="index.html",
                    accept_encoding=accept_encoding,
                    media_type="text/html",
                )
            return HTMLResponse("Not Found", status_code=404)

        try:
            file_path = safe_join(dist_path, path)
        except PathEscapeError:
            return HTMLResponse("Not Found", status_code=404)

        if file_path.exists() and file_path.is_file():
            return static_file_response(
                file_path,
                rel_path=path,
                accept_encoding=accept_encoding,
            )

        if not should_spa_fallback(path):
            return HTMLResponse("Not Found", status_code=404)

        index_path = dist_path / "index.html"
        if index_path.exists():
            return static_file_response(
                index_path,
                rel_path="index.html",
                accept_encoding=accept_encoding,
                media_type="text/html",
            )
        return HTMLResponse("Not Found", status_code=404)

    return router
