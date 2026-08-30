"""插件 Web 页面注册表 + 插件 API 前缀助手。

插件作者的统一收口::

    from gsuid_core.webconsole.plugin_page import register_plugin_page, PluginAPI

    register_plugin_page(
        title="抽卡与角色管理",
        static_dir="web",
        page_id="console",
        title_i18n={"en-US": "Gacha & Agents", "ja-JP": "ガチャとエージェント"},
    )

    api = PluginAPI()  # 默认前缀 /api/<plugin_id>，默认 require_auth


    @api.get("/players")
    async def list_players():
        return api_ok([])

静态页由框架挂到 ``/plugin-pages/<plugin_id>/<page_id>/``，Hub 的 /plugins 页
据此显示跳转按钮。API 仍走 ``/api/...``。
"""

from __future__ import annotations

import re
import sys
from enum import Enum
from typing import TypeVar, Callable, TypedDict
from pathlib import Path
from dataclasses import dataclass

from fastapi import Depends

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.webconsole.mount_app import get_caller_plugin_name

F = TypeVar("F", bound=Callable[..., object])

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_RESERVED_PLUGIN_IDS = frozenset({"_sdk", "_hub", "api"})
_LOCALE_CANON = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "zh-CN": "zh-CN",
    "en": "en-US",
    "en-us": "en-US",
    "en_us": "en-US",
    "en-US": "en-US",
    "ja": "ja-JP",
    "ja-jp": "ja-JP",
    "ja_jp": "ja-JP",
    "ja-JP": "ja-JP",
}
HUB_LOCALES = ("zh-CN", "en-US", "ja-JP")
DEFAULT_CONFIRM = {
    "zh-CN": "即将打开插件提供的页面。确认后侧边栏会收起，并以嵌入方式打开。",
    "en-US": "Open the page provided by this plugin. The sidebar will collapse and the page will be embedded.",
    "ja-JP": "プラグインが提供するページを開きます。サイドバーは折りたたまれ、埋め込み表示されます。",
}
ALLOWED_STATIC_SUFFIXES = frozenset(
    {
        ".html",
        ".htm",
        ".js",
        ".mjs",
        ".css",
        ".json",
        ".map",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".txt",
        ".wasm",
        ".webmanifest",
    }
)


class PluginPagePublic(TypedDict):
    id: str
    plugin: str
    plugin_id: str
    path: str
    title: dict[str, str]
    description: dict[str, str]
    confirm_message: dict[str, str]
    icon: str


class ApiOk(TypedDict):
    status: int
    msg: str
    data: object


class ApiFail(TypedDict):
    status: int
    msg: str
    data: None


@dataclass
class PluginPageSpec:
    plugin: str
    plugin_id: str
    page_id: str
    static_dir: Path
    index: str
    title: dict[str, str]
    description: dict[str, str]
    confirm_message: dict[str, str]
    icon: str
    locales_dir: Path | None


_PAGES: dict[tuple[str, str], PluginPageSpec] = {}
_BY_PLUGIN: dict[str, list[tuple[str, str]]] = {}


def api_ok(data: object, msg: str = "ok") -> ApiOk:
    return {"status": 0, "msg": msg, "data": data}


def api_fail(msg: str, status: int = 1) -> ApiFail:
    return {"status": status, "msg": msg, "data": None}


def slugify_plugin_id(name: str) -> str:
    """Plugins.name / 目录名 → URL 段。只保留 [a-z0-9_-]。"""
    raw = name.strip().strip("_").lower()
    out: list[str] = []
    for ch in raw:
        if ch.isalnum() or ch in "_-":
            out.append(ch)
        else:
            out.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(out)).strip("-")
    if not slug:
        raise ValueError(f"plugin id from {name!r} is empty")
    if slug[0].isdigit():
        slug = f"p-{slug}"
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid plugin id {slug!r}")
    if slug in _RESERVED_PLUGIN_IDS:
        raise ValueError(f"plugin id {slug!r} is reserved")
    return slug


def canon_locale(lang: str) -> str | None:
    if lang in _LOCALE_CANON:
        return _LOCALE_CANON[lang]
    key = lang.replace("_", "-")
    if key in _LOCALE_CANON:
        return _LOCALE_CANON[key]
    low = key.lower()
    if low in _LOCALE_CANON:
        return _LOCALE_CANON[low]
    return None


def _i18n_map(
    default: str, extra: dict[str, str] | None, *, fallback_locale: dict[str, str] | None = None
) -> dict[str, str]:
    base = fallback_locale if fallback_locale is not None else {loc: default for loc in HUB_LOCALES}
    out = dict(base)
    if default:
        out["zh-CN"] = default
        if not extra:
            for loc in HUB_LOCALES:
                if loc not in out or not out[loc]:
                    out[loc] = default
    if extra:
        for k, v in extra.items():
            if not isinstance(k, str) or not isinstance(v, str) or not v:
                continue
            canon = canon_locale(k)
            if canon is None:
                continue
            out[canon] = v
    for loc in HUB_LOCALES:
        if loc not in out or not out[loc]:
            out[loc] = default or out.get("zh-CN", "")
    return out


def _resolve_plugin_name(raw: str | None) -> str:
    if not raw:
        raise ValueError("cannot infer plugin name; pass plugin=")
    from gsuid_core.sv import SL

    if raw in SL.plugins:
        return raw
    stripped = raw.strip("_")
    if stripped in SL.plugins:
        return stripped
    target = raw.lower()
    stripped_l = stripped.lower()
    for name in SL.plugins:
        nl = name.lower()
        if nl == target or nl == stripped_l:
            return name
    return stripped or raw


def _caller_file() -> Path:
    frame = sys._getframe(2)
    return Path(frame.f_code.co_filename).resolve()


def _resolve_static_dir(static_dir: str | Path, caller_file: Path) -> Path:
    p = Path(static_dir)
    if not p.is_absolute():
        p = (caller_file.parent / p).resolve()
    else:
        p = p.resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"plugin page static_dir not found: {p}")
    return p


def register_plugin_page(
    *,
    title: str,
    static_dir: str | Path,
    page_id: str = "main",
    plugin: str | None = None,
    description: str = "",
    confirm_message: str = "",
    title_i18n: dict[str, str] | None = None,
    description_i18n: dict[str, str] | None = None,
    confirm_message_i18n: dict[str, str] | None = None,
    index: str = "index.html",
    icon: str = "",
    locales_dir: str | Path | None = None,
) -> PluginPageSpec:
    """登记一个插件前端页。``static_dir`` 相对调用方文件所在目录。

    ``page_id`` 会成为 URL ``/plugin-pages/<plugin_id>/<page_id>/``。
    同一插件重复登记同一 ``page_id`` 会覆盖（便于热重载）。
    """
    inferred = plugin if plugin is not None else get_caller_plugin_name()
    plugin_name = _resolve_plugin_name(inferred)
    plugin_id = slugify_plugin_id(plugin_name)
    pid = page_id.strip().lower()
    if not _SLUG_RE.match(pid) or pid.startswith("_"):
        raise ValueError(f"invalid page_id {page_id!r}")
    caller = _caller_file()
    root = _resolve_static_dir(static_dir, caller)
    idx = Path(index).name
    if not idx or idx.startswith(".") or Path(idx).suffix.lower() not in {".html", ".htm"}:
        raise ValueError(f"invalid index file {index!r}")
    index_path = root / idx
    if not index_path.is_file():
        raise FileNotFoundError(f"plugin page index not found: {index_path}")

    loc_dir: Path | None
    if locales_dir is None:
        cand = root / "locales"
        loc_dir = cand if cand.is_dir() else None
    else:
        lp = Path(locales_dir)
        loc_dir = lp if lp.is_absolute() else (caller.parent / lp).resolve()
        if not loc_dir.is_dir():
            loc_dir = None

    spec = PluginPageSpec(
        plugin=plugin_name,
        plugin_id=plugin_id,
        page_id=pid,
        static_dir=root,
        index=idx,
        title=_i18n_map(title, title_i18n),
        description=_i18n_map(description, description_i18n),
        confirm_message=_i18n_map(confirm_message, confirm_message_i18n, fallback_locale=dict(DEFAULT_CONFIRM)),
        icon=icon.strip(),
        locales_dir=loc_dir,
    )
    key = (plugin_id, pid)
    _PAGES[key] = spec
    owned = _BY_PLUGIN.setdefault(plugin_name, [])
    if key not in owned:
        owned.append(key)
    logger.info(
        t(
            "log.webconsole.plugin_page_registered",
            plugin=plugin_name,
            plugin_id=plugin_id,
            page_id=pid,
        )
    )
    return spec


def unregister_plugin_pages(plugin_name: str) -> int:
    """卸掉某插件登记的全部页面。热重载时由框架调用。"""
    removed = 0
    names = [plugin_name, plugin_name.strip("_")]
    keys: list[tuple[str, str]] = []
    for stored, items in list(_BY_PLUGIN.items()):
        if stored.lower() in {n.lower() for n in names} or stored.strip("_").lower() == plugin_name.strip("_").lower():
            keys.extend(items)
            _BY_PLUGIN.pop(stored, None)
    for key in keys:
        if key in _PAGES:
            _PAGES.pop(key)
            removed += 1
    return removed


def clear_plugin_pages() -> None:
    _PAGES.clear()
    _BY_PLUGIN.clear()


def get_plugin_page(plugin_id: str, page_id: str) -> PluginPageSpec | None:
    key = (plugin_id.lower(), page_id.lower())
    if key in _PAGES:
        return _PAGES[key]
    return None


def spec_to_public(spec: PluginPageSpec) -> PluginPagePublic:
    return {
        "id": spec.page_id,
        "plugin": spec.plugin,
        "plugin_id": spec.plugin_id,
        "path": f"/plugin-pages/{spec.plugin_id}/{spec.page_id}/",
        "title": dict(spec.title),
        "description": dict(spec.description),
        "confirm_message": dict(spec.confirm_message),
        "icon": spec.icon,
    }


def pages_for_plugin(plugin_name: str) -> list[PluginPagePublic]:
    out: list[PluginPagePublic] = []
    target = plugin_name.lower()
    stripped = plugin_name.strip("_").lower()
    try:
        pid = slugify_plugin_id(plugin_name)
    except ValueError:
        pid = ""
    for spec in _PAGES.values():
        n = spec.plugin.lower()
        if n == target or n.strip("_") == stripped or (pid != "" and spec.plugin_id == pid):
            out.append(spec_to_public(spec))
    out.sort(key=lambda p: p["id"])
    return out


def list_plugin_pages() -> list[PluginPagePublic]:
    items = [spec_to_public(s) for s in _PAGES.values()]
    items.sort(key=lambda p: (p["plugin_id"], p["id"]))
    return items


def resolve_page_file(spec: PluginPageSpec, rel: str) -> Path | None:
    """把 URL 相对路径落到 static_dir。越界 / 非法后缀返回 None。"""
    from gsuid_core.utils.path_safety import PathEscapeError, safe_join

    cleaned = rel.strip().lstrip("/")
    if not cleaned or cleaned.endswith("/"):
        cleaned = spec.index if not cleaned else cleaned + spec.index
    try:
        path = safe_join(spec.static_dir, cleaned)
    except PathEscapeError:
        return None
    if path.is_dir():
        path = path / spec.index
    if not path.is_file():
        return None
    if path.suffix.lower() not in ALLOWED_STATIC_SUFFIXES:
        return None
    return path


class PluginAPI:
    """给插件 API 一个统一前缀 ``/api/<plugin_id>``，默认挂 ``require_auth``。

    路径必须落在 ``/api/`` 下。需要公开接口时传 ``auth=False``。
    """

    def __init__(
        self,
        plugin: str | None = None,
        *,
        prefix: str | None = None,
        auth: bool = True,
        tag: str | None = None,
    ) -> None:
        inferred = plugin if plugin is not None else get_caller_plugin_name()
        self.plugin = _resolve_plugin_name(inferred)
        self.plugin_id = slugify_plugin_id(self.plugin)
        if prefix is None:
            self.prefix = f"/api/{self.plugin_id}"
        else:
            pfx = prefix.rstrip("/")
            if not pfx.startswith("/api/"):
                raise ValueError("plugin API prefix must start with /api/")
            self.prefix = pfx
        self._deps = [Depends(require_auth)] if auth else []
        tag_item: str | Enum = tag if tag is not None else f"插件/{self.plugin}"
        self._tags: list[str | Enum] = [tag_item]

    def _join(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.prefix + path

    def _route(self, method: str, path: str) -> Callable[[F], F]:
        full = self._join(path)
        tags = self._tags
        deps = self._deps
        if method == "get":
            deco = app.get(full, tags=tags, dependencies=deps)
        elif method == "post":
            deco = app.post(full, tags=tags, dependencies=deps)
        elif method == "put":
            deco = app.put(full, tags=tags, dependencies=deps)
        elif method == "patch":
            deco = app.patch(full, tags=tags, dependencies=deps)
        elif method == "delete":
            deco = app.delete(full, tags=tags, dependencies=deps)
        else:
            raise ValueError(f"unsupported HTTP method {method}")

        def wrap(fn: F) -> F:
            return deco(fn)

        return wrap

    def get(self, path: str) -> Callable[[F], F]:
        return self._route("get", path)

    def post(self, path: str) -> Callable[[F], F]:
        return self._route("post", path)

    def put(self, path: str) -> Callable[[F], F]:
        return self._route("put", path)

    def patch(self, path: str) -> Callable[[F], F]:
        return self._route("patch", path)

    def delete(self, path: str) -> Callable[[F], F]:
        return self._route("delete", path)
