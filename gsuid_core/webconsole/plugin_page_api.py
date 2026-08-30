"""插件页面静态资源 + 元数据 API。"""

from __future__ import annotations

import json
from typing import Any, Dict
from pathlib import Path

from fastapi import Depends, Request
from fastapi.responses import Response, HTMLResponse, JSONResponse

from gsuid_core.data_store import DIST_PATH, DIST_EX_PATH
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.webconsole.plugin_page import (
    get_plugin_page,
    list_plugin_pages,
    resolve_page_file,
)
from gsuid_core.webconsole.static_serve import static_file_response

from ._api_tags import PLUGIN_PAGES

_SDK_NAME = "gshub-plugin.js"

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json",
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
    ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
}


def _mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _MIME:
        return _MIME[suffix]
    return "application/octet-stream"


def _dist_version(dist: Path) -> tuple[int, ...]:
    vf = dist / "version.json"
    if not vf.is_file():
        return (0,)
    raw = json.loads(vf.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "version" not in raw:
        return (0,)
    ver = raw["version"]
    if not isinstance(ver, str):
        return (0,)
    nums: list[int] = []
    for piece in ver.split("."):
        if not piece.isdigit():
            break
        nums.append(int(piece))
    return tuple(nums) if nums else (0,)


def resolve_hub_sdk_path(
    bundled: Path | None = None,
    extra: Path | None = None,
) -> Path | None:
    """Hub ``pnpm build`` 把 public/gshub-plugin.js 拷进 dist。选与 /app 相同的那份。"""
    dist = bundled if bundled is not None else DIST_PATH
    dist_ex = extra if extra is not None else DIST_EX_PATH
    bundled_file = dist / _SDK_NAME
    extra_file = dist_ex / _SDK_NAME
    bundled_ok = bundled_file.is_file()
    extra_ok = extra_file.is_file()
    if extra_ok and bundled_ok:
        if _dist_version(dist_ex) > _dist_version(dist):
            return extra_file
        return bundled_file
    if extra_ok:
        return extra_file
    if bundled_ok:
        return bundled_file
    return None


@app.get("/api/plugin-pages", summary="列出已挂载的插件页面", tags=PLUGIN_PAGES)
async def api_list_plugin_pages(
    request: Request,
    _user: Dict[str, Any] = Depends(require_auth),
) -> dict[str, object]:
    return {"status": 0, "msg": "ok", "data": list_plugin_pages()}


@app.get("/plugin-pages/_sdk/gshub-plugin.js", summary="插件页 i18n/鉴权 SDK", tags=PLUGIN_PAGES)
async def serve_plugin_sdk(request: Request) -> Response:
    sdk = resolve_hub_sdk_path()
    if sdk is None:
        return HTMLResponse("SDK missing; rebuild gsuid_hub into webconsole/dist", status_code=404)
    return static_file_response(
        sdk,
        rel_path=_SDK_NAME,
        accept_encoding=request.headers.get("accept-encoding"),
        media_type="application/javascript; charset=utf-8",
    )


@app.get("/plugin-pages/{plugin_id}/{page_id}", include_in_schema=False)
@app.get("/plugin-pages/{plugin_id}/{page_id}/", include_in_schema=False)
async def serve_plugin_page_index(plugin_id: str, page_id: str, request: Request) -> Response:
    return await serve_plugin_page_file(plugin_id, page_id, "", request)


@app.get("/plugin-pages/{plugin_id}/{page_id}/{rest:path}", include_in_schema=False)
async def serve_plugin_page_file(
    plugin_id: str,
    page_id: str,
    rest: str,
    request: Request,
) -> Response:
    spec = get_plugin_page(plugin_id, page_id)
    if spec is None:
        return JSONResponse({"status": 1, "msg": "plugin page not found", "data": None}, status_code=404)
    path = resolve_page_file(spec, rest)
    if path is None:
        return JSONResponse({"status": 1, "msg": "file not found", "data": None}, status_code=404)
    rel = rest or path.name
    return static_file_response(
        path,
        rel_path=rel,
        accept_encoding=request.headers.get("accept-encoding"),
        media_type=_mime(path),
    )
