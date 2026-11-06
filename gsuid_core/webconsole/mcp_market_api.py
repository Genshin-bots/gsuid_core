"""MCP 广场 API（mcpmarket.cn 中文源 + ModelScope 搜索/详情/安装预览 + logo 本地代理缓存）。

- 目录双源聚合：mcpmarket.cn ``/api/servers``（原生中文、真实分页、官方直连/平台代跑）
  与 ModelScope ``/openapi/v1/mcp/servers``，进程内 TTL 缓存；只收有可安装变体的服务。
  魔搭首页不可达才回落 ``MCP_PRESETS`` 卡片（``source="preset"``、``degraded=true``）。
- 安装预览只规范化上游变体并做冲突/风险标注，**不落盘**；
  真正安装仍走 ``POST /api/ai/mcp`` → ``register_single_mcp_server`` 同一条路径。
- logo 一律经本地代理 ``GET /api/ai/mcp/market/logo?u=...`` 缓存到
  ``data/ai_core/mcp_market/logo/``；下载失败返回 404，绝不影响主流程。
"""

import re
import json
import time
import shutil
import asyncio
import hashlib
import ipaddress
from typing import Any, Set, Dict, List, Callable, Optional, Coroutine, TypedDict
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qsl, quote_plus, urlunparse

import httpx
from fastapi import Query, Depends, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin
from gsuid_core.ai_core.mcp.transport import (
    MCP_TRANSPORT_STDIO,
    detect_mcp_transport,
    is_http_mcp_transport,
)
from gsuid_core.ai_core.mcp.mcp_presets import MCP_PRESETS
from gsuid_core.ai_core.mcp.config_manager import mcp_config_manager, parse_mcp_json_server
from gsuid_core.utils.plugins_config.models import GSC, GsIntConfig, GsStrConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from ._api_tags import MCP_CONFIG

MCP_MARKET_CONFIG: Dict[str, GSC] = {
    "endpoint": GsStrConfig(
        title="MCP 广场上游端点",
        desc="ModelScope MCP 服务广场 API 端点",
        data="https://www.modelscope.cn",
        options=["https://www.modelscope.cn", "https://modelscope.ai"],
    ),
    "api_token": GsStrConfig(
        title="MCP 广场 API Token",
        desc="ModelScope API Token，可空（公开接口）",
        data="",
        secret=True,
    ),
    "cache_ttl_sec": GsIntConfig(
        "MCP 广场缓存 TTL(秒)",
        "广场列表/详情进程内缓存时间",
        600,
        options=[60, 300, 600, 1800],
    ),
    "request_timeout_sec": GsIntConfig(
        "MCP 广场请求超时(秒)",
        "调用上游接口超时",
        10,
        options=[5, 10, 20, 30],
    ),
}

mcp_market_config = StringConfig(
    "MCP 广场配置",
    get_res_path("ai_core") / "mcp_market_config.json",
    MCP_MARKET_CONFIG,
)

PRESET_SOURCE = "preset"
MODELSCOPE_SOURCE = "modelscope"

# 详情进程内缓存：key → (写入时间戳, 响应 data)
_DETAIL_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
# 上游不支持排序：并发拉前 N 页本地排序，封顶 N*100 条
# （每条还要取详情判可安装，目录越大首载越慢；匿名窗口实际只有 1 页）
_CATALOG_MAX_PAGES = 3
_CATALOG_PAGE_SIZE = 100
# 未配 token 时上游列表只允许 page_number*page_size <= 100（超出 403）；
# 配置 api_token 后按不限窗口处理（上游对认证请求放开深翻页）
_ANON_LIST_WINDOW = 100

# 上游分类枚举（ModelScope 分类表；返回给前端做筛选下拉）
MARKET_CATEGORIES: List[str] = [
    "browser-automation",
    "developer-tools",
    "location-services",
    "search",
    "finance",
    "knowledge-and-memory",
    "entertainment-and-media",
    "calendar-management",
    "travel-and-transportation",
    "communication",
    "research-and-data",
    "art-and-culture",
    "databases",
    "version-control",
    "file-systems",
    "image-and-video-processing",
    "os-automation",
    "cloud-platforms",
    "monitoring",
    "note-taking",
    "social-media",
    "ai-media",
    "skill-management",
    "AIGC",
    "other",
]


class _MarketEntryRequired(TypedDict):
    qualified_name: str
    source: str


class MarketEntry(_MarketEntryRequired, total=False):
    name: str
    chinese_name: str
    description: str
    categories: List[str]
    author: str
    logo_url: str
    view_count: int
    tags: List[str]


class MarketVariant(TypedDict, total=False):
    label: str
    name: str
    transport: str
    command: str
    args: List[str]
    env: Dict[str, str]
    url: str
    headers: Dict[str, str]
    requires_checkout: bool


class MarketToolParam(TypedDict, total=False):
    name: str
    type: str
    required: bool
    description: str


class MarketTool(TypedDict, total=False):
    name: str
    description: str
    params: List[MarketToolParam]


# 合并目录缓存：(endpoint|search|category|hosted) → (ts, entries, 上游total, truncated, degraded)
_CATALOG_CACHE: Dict[str, tuple[float, List[MarketEntry], int, bool, bool]] = {}


def _has_market_token() -> bool:
    token = mcp_market_config.get_config("api_token").data
    return isinstance(token, str) and bool(token)


# ---- stdio 本机运行时检测 ----

# 已知运行时家族：可执行文件名（去扩展名、小写）→ 前端指引 key
_RUNTIME_KINDS: Dict[str, str] = {
    "node": "nodejs",
    "npx": "nodejs",
    "npm": "nodejs",
    "uv": "uv",
    "uvx": "uv",
    "docker": "docker",
    "docker-compose": "docker",
    "python": "python",
    "python3": "python",
    "py": "python",
    "pipx": "pipx",
    "deno": "deno",
    "bun": "bun",
    "bunx": "bun",
}


def check_stdio_runtime(command: str) -> Dict[str, Any]:
    """检测 stdio 命令运行时是否可用：PATH 解析 + 已知运行时归类。"""
    cmd = command.strip()
    exe = cmd.split()[0] if cmd else ""
    resolved = shutil.which(exe) if exe else None
    # 绝对/相对路径形态：which 查不到时按文件存在兜底
    if resolved is None and ("/" in exe or "\\" in exe):
        path = Path(exe).expanduser()
        if path.exists():
            resolved = str(path)
    base = Path(exe).stem.lower() if exe else ""
    return {
        "command": exe,
        "available": resolved is not None,
        "resolved": resolved,
        "runtime": _RUNTIME_KINDS.get(base, "unknown"),
    }


# ---- logo 本地代理 ----

_LOGO_DIR: Path = get_res_path("ai_core") / "mcp_market" / "logo"
_LOGO_MAX_BYTES = 1024 * 1024
_CONTENT_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}
_LOGO_FETCHING: set[str] = set()
_LOGO_FAILED: set[str] = set()


def _logo_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]


def logo_proxy_url(upstream: str) -> str:
    """把上游 logo URL 换成本地代理 URL（前端 <img> 直接用）。"""
    return f"/api/ai/mcp/market/logo?u={quote(upstream, safe='')}"


def ensure_logo_cached(upstream: str) -> None:
    """尽力后台预取 logo 到本地缓存；任何失败都吞掉。"""
    url = upstream.strip()
    if not url.startswith(("https://", "http://")) or url in _LOGO_FAILED:
        return
    key = _logo_key(url)
    if (_LOGO_DIR / f"{key}.json").exists() or url in _LOGO_FETCHING:
        return
    _LOGO_FETCHING.add(url)
    task = asyncio.create_task(_download_logo(url, key))
    task.add_done_callback(lambda _task: _LOGO_FETCHING.discard(url))


def _write_logo_files(key: str, ext: str, content: bytes, meta: dict[str, str]) -> None:
    _LOGO_DIR.mkdir(parents=True, exist_ok=True)
    (_LOGO_DIR / f"{key}{ext}").write_bytes(content)
    (_LOGO_DIR / f"{key}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _sniff_image(content: bytes, content_type: str) -> Optional[tuple[str, str]]:
    """按魔数识别图片 → (扩展名, media_type)；上游 CDN 常把 PNG 发成 octet-stream。"""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if content[:4] == b"\x00\x00\x01\x00":
        return ".ico", "image/x-icon"
    if content.lstrip()[:1] == b"<" and b"<svg" in content[:512].lower():
        return ".svg", "image/svg+xml"
    if content_type.startswith("image/"):
        return _CONTENT_EXT.get(content_type, ".img"), content_type
    return None


async def _download_logo(url: str, key: str) -> None:
    """下载 logo 落盘（图片 + meta sidecar）；确定失败记 _LOGO_FAILED，绝不抛出。"""
    try:
        timeout = float(mcp_market_config.get_config("request_timeout_sec").data or 10)
        async with httpx.AsyncClient(timeout=httpx.Timeout(min(timeout, 10.0)), follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or len(resp.content) > _LOGO_MAX_BYTES:
            _LOGO_FAILED.add(url)
            return
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        sniffed = _sniff_image(resp.content, content_type)
        if sniffed is None:
            _LOGO_FAILED.add(url)
            return
        ext, media_type = sniffed
        meta = {"url": url, "content_type": media_type, "file": f"{key}{ext}"}
        await asyncio.to_thread(_write_logo_files, key, ext, resp.content, meta)
    except Exception as e:
        # 瞬时网络错误不进失败集：下次 ensure/请求还会重试
        logger.debug(t("log.mcp.market_logo_fetch_failed", e=e))


# ---- 上游数据规范化（不可信外部输入：逐字段 isinstance，坏记录整条跳过）----


def _str_field(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) else ""


def _str_list(raw: dict[str, Any], key: str) -> List[str]:
    value = raw.get(key)
    if not isinstance(value, list):
        return []
    return [x.strip() for x in value if isinstance(x, str) and x.strip()]


def _normalize_market_item(raw: Any) -> Optional[MarketEntry]:
    """上游单条记录 → MarketEntry；缺 qualified_name 即丢弃。"""
    if not isinstance(raw, dict):
        return None
    qualified = _str_field(raw, "id") or _str_field(raw, "qualified_name")
    if not qualified:
        return None
    entry: MarketEntry = {"qualified_name": qualified, "source": MODELSCOPE_SOURCE}
    entry["name"] = _str_field(raw, "name") or qualified
    for key in ("chinese_name", "description", "author", "logo_url"):
        value = _str_field(raw, key)
        if value:
            entry[key] = value
    entry["categories"] = _str_list(raw, "categories")
    tags = _str_list(raw, "tags")
    if tags:
        entry["tags"] = tags
    view_count = raw.get("view_count")
    entry["view_count"] = view_count if isinstance(view_count, int) else 0
    return entry


def _extract_items(payload: Any) -> List[Any]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    for key in ("mcp_server_list", "servers", "list", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _extract_total(payload: Any, fallback: int) -> int:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload["data"]
        for key in ("total_count", "total"):
            value = data.get(key)
            if isinstance(value, int):
                return value
    return fallback


def _normalize_market_detail(raw: Any) -> Optional[Dict[str, Any]]:
    """上游详情 → {entry 字段 + variants + readme + source_url + default_variant}。"""
    item = _normalize_market_item(raw)
    if item is None or not isinstance(raw, dict):
        return None
    detail: Dict[str, Any] = dict(item)

    variants: List[MarketVariant] = []
    # 远程变体优先（operational_urls 是上游托管的在线端点；认证响应可能是单数 operational_url）
    op_urls = raw.get("operational_urls")
    if not isinstance(op_urls, list):
        single = raw.get("operational_url")
        op_urls = [single] if isinstance(single, (str, dict)) else []
    for u in op_urls:
        url = u if isinstance(u, str) else (u.get("url") if isinstance(u, dict) else "")
        if isinstance(url, str) and url.startswith("http") and _is_public_remote_url(url):
            variant = MarketVariant(
                label="remote",
                transport=detect_mcp_transport(url=url),
                url=url,
                command="",
                args=[],
                env={},
                headers={},
            )
            _normalize_remote_variant(variant)
            variants.append(variant)
    # 本地变体：server_config 里每个 mcpServers 配置是一个安装方式；
    # 上游偶发把对象序列化成 JSON 字符串，先尝试还原
    server_config = raw.get("server_config")
    if isinstance(server_config, str):
        try:
            server_config = json.loads(server_config)
        except (OSError, json.JSONDecodeError):
            server_config = None
    sc_items = (
        server_config
        if isinstance(server_config, list)
        else ([server_config] if isinstance(server_config, dict) else [])
    )
    for sc in sc_items:
        if not isinstance(sc, dict):
            continue
        servers = sc.get("mcpServers")
        if not isinstance(servers, dict) or not servers:
            continue
        srv_name, srv_raw = next(iter(servers.items()))
        draft = parse_mcp_json_server(str(srv_name), srv_raw)
        if draft is None:
            continue
        variant = MarketVariant(
            label=draft.get("transport", MCP_TRANSPORT_STDIO),
            name=str(srv_name),
            transport=draft.get("transport", MCP_TRANSPORT_STDIO),
            command=draft.get("command", ""),
            args=draft.get("args", []),
            env=draft.get("env", {}),
            url=draft.get("url", ""),
            headers=draft.get("headers", {}),
        )
        if variant.get("transport") == MCP_TRANSPORT_STDIO and _stdio_needs_checkout(
            variant.get("command", ""), variant.get("args", [])
        ):
            variant["requires_checkout"] = True
        _normalize_remote_variant(variant)
        variants.append(variant)

    variants = _drop_private_remote_variants(variants)
    detail["variants"] = variants
    detail["default_variant"] = next(
        (i for i, v in enumerate(variants) if is_http_mcp_transport(v.get("transport", ""))), 0
    )
    readme = ""
    locales = raw.get("locales")
    if isinstance(locales, dict):
        for lang in ("zh", "zh-cn", "zh_CN"):
            zh = locales.get(lang)
            if isinstance(zh, dict):
                readme = _str_field(zh, "readme")
                if readme:
                    break
    detail["readme"] = readme or _str_field(raw, "readme")
    detail["source_url"] = _str_field(raw, "source_url")
    detail["is_hosted"] = bool(raw.get("is_hosted"))
    detail["is_verified"] = bool(raw.get("is_verified"))
    qualified = _str_field(raw, "id") or _str_field(raw, "qualified_name")
    detail["page_url"] = f"{_market_endpoint()}/mcp/servers/{quote(qualified)}"
    auth_help = raw.get("auth_help")
    detail["auth_help"] = _mcpcn_text(auth_help)
    detail["obtain_url"] = (_str_field(auth_help, "obtain_url") if isinstance(auth_help, dict) else "") or _str_field(
        raw, "obtain_url"
    )
    return detail


# ---- preset 回落 ----


def _preset_entries(search: str) -> List[MarketEntry]:
    keyword = search.strip().lower()
    entries: List[MarketEntry] = []
    for preset in MCP_PRESETS:
        name = preset.get("name", "")
        desc = preset.get("description", "")
        if not isinstance(name, str) or not name:
            continue
        desc_text = desc if isinstance(desc, str) else ""
        if keyword and keyword not in name.lower() and keyword not in desc_text.lower():
            continue
        entries.append(
            MarketEntry(
                qualified_name=f"{PRESET_SOURCE}:{name}",
                name=name,
                description=desc_text,
                categories=["preset"],
                source=PRESET_SOURCE,
            )
        )
    return entries


def _preset_detail(name: str) -> Optional[Dict[str, Any]]:
    for preset in MCP_PRESETS:
        if preset.get("name") != name:
            continue
        env_template = preset.get("env_template")
        headers_template = preset.get("headers_template")
        args_raw = preset.get("args")
        transport = preset.get("transport")
        url = preset.get("url")
        variant = MarketVariant(
            label=transport if isinstance(transport, str) and transport else MCP_TRANSPORT_STDIO,
            transport=transport if isinstance(transport, str) and transport else MCP_TRANSPORT_STDIO,
            command=preset.get("command", "") if isinstance(preset.get("command"), str) else "",
            args=[a for a in args_raw if isinstance(a, str)] if isinstance(args_raw, list) else [],
            env={k: v for k, v in env_template.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(env_template, dict)
            else {},
            url=url if isinstance(url, str) else "",
            headers={k: v for k, v in headers_template.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(headers_template, dict)
            else {},
        )
        return {
            "qualified_name": f"{PRESET_SOURCE}:{name}",
            "name": name,
            "description": preset.get("description", ""),
            "categories": ["preset"],
            "source": PRESET_SOURCE,
            "variants": [variant],
            "default_variant": 0,
            "readme": "",
            "source_url": "",
            "is_hosted": False,
            "is_verified": False,
            "page_url": "",
        }
    return None


# ---- MCP市场（mcpmarket.cn）----
# 中文聚合源：原生中文数据 + 真实分页（total_servers）；官方服务给官方直连 URL
# （mcp_url + header_templates + auth_help 指引），社区服务由平台代跑远程实例，
# mcp_config 附 stdio 本地装法——两种变体都收。
MCPCN_SOURCE = "mcpmarket"
MCPCN_ENDPOINT = "https://mcpmarket.cn"
MCPCN_PREFIX = "mcpcn:"
# 上游忽略 size 参数、固定返回 12 条/页（实测 size=12..200 均返回 12）
_MCPCN_PAGE_SIZE = 12
_MCPCN_CATALOG_CAP = 3000
# 目录翻页并发度（第一页拿到 total_pages 后 gather 剩余页）
_CATALOG_PAGE_CONCURRENCY = 8
# 详情批量判定并发度（两源可安装过滤共用）
_DETAIL_CHECK_CONCURRENCY = 24
# mcpmarket /api/categories 里只对 UI 有意义、不能当过滤条件用的虚拟桶
_MCPCN_VIRTUAL_CATEGORIES = {"全部", "客户端", "直连", "最热", "最新"}
# mcpmarket 中文分类 → 魔搭英文 slug（无映射的分类下直接跳过魔搭源）
MCPCN_TO_MODELSCOPE_CATEGORY: Dict[str, str] = {
    "社交": "social-media",
    "金融": "finance",
    "开发": "developer-tools",
    "搜索": "search",
    "数据": "databases",
    "媒体": "entertainment-and-media",
    "学术研究": "research-and-data",
    "旅行": "travel-and-transportation",
    "知识百科": "knowledge-and-memory",
    "AI媒体": "ai-media",
}
MODELSCOPE_TO_MCPCN_CATEGORY: Dict[str, str] = {v: k for k, v in MCPCN_TO_MODELSCOPE_CATEGORY.items()}


# 分类表缓存（非 detail 形态，独立存）
_MCPCN_CATEGORIES_CACHE: Optional[tuple[float, List[str]]] = None


async def _fetch_mcpcn_categories() -> List[str]:
    """mcpmarket /api/categories → 中文分类表（剔除虚拟桶与小众长尾）。"""
    global _MCPCN_CATEGORIES_CACHE
    if _MCPCN_CATEGORIES_CACHE and time.time() - _MCPCN_CATEGORIES_CACHE[0] < _effective_ttl():
        return _MCPCN_CATEGORIES_CACHE[1]
    names: List[str] = []
    try:
        async with httpx.AsyncClient(timeout=_market_timeout()) as client:
            resp = await client.get(f"{MCPCN_ENDPOINT}/api/categories")
        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    count = item.get("count")
                    if (
                        isinstance(name, str)
                        and name
                        and name not in _MCPCN_VIRTUAL_CATEGORIES
                        and isinstance(count, int)
                        and count >= 10
                    ):
                        names.append(name)
    except Exception as e:
        logger.warning(t("log.mcp.market_fetch_failed", e=e))
    _MCPCN_CATEGORIES_CACHE = (time.time(), names)
    return names


# overview 分段结构 → markdown 小节标题（跟随所选语言）
_MCPCN_OVERVIEW_SECTIONS = (
    ("what_is", "是什么", "What it is"),
    ("key_features", "主要功能", "Key features"),
    ("use_cases", "使用场景", "Use cases"),
    ("where_to_use", "适用环境", "Where to use"),
    ("how_to_use", "使用方法", "How to use"),
)


def _mcpcn_overview_readme(raw: Any) -> str:
    """overview {zh:{what_is:..,key_features:..,..}, en:{..}} → 合成 markdown readme。"""
    if not isinstance(raw, dict):
        return ""
    zh = raw.get("zh")
    lang = zh if isinstance(zh, dict) and any(isinstance(v, str) and v.strip() for v in zh.values()) else None
    idx = 1
    if lang is None:
        en = raw.get("en")
        if isinstance(en, dict) and any(isinstance(v, str) and v.strip() for v in en.values()):
            lang, idx = en, 2
        else:
            return ""
    parts = []
    for key, zh_title, en_title in _MCPCN_OVERVIEW_SECTIONS:
        text = lang.get(key)
        if isinstance(text, str) and text.strip():
            parts.append(f"## {zh_title if idx == 1 else en_title}\n\n{text.strip()}")
    return "\n\n".join(parts)


def _mcpcn_text(raw: Any) -> str:
    """{en, zh} 多语言字段或纯 str → 优先中文。"""
    if isinstance(raw, dict):
        for key in ("zh", "en"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    return raw.strip() if isinstance(raw, str) else ""


def _normalize_mcpcn_item(raw: Any) -> Optional[MarketEntry]:
    """mcpmarket 列表项 → MarketEntry。"""
    if not isinstance(raw, dict):
        return None
    sid = raw.get("_id")
    if not isinstance(sid, str) or not sid:
        return None
    entry: MarketEntry = {"qualified_name": f"{MCPCN_PREFIX}{sid}", "source": MCPCN_SOURCE}
    name = raw.get("name")
    entry["name"] = name.strip() if isinstance(name, str) and name.strip() else sid
    alias = raw.get("alias")
    if isinstance(alias, str) and alias.strip():
        entry["chinese_name"] = alias.strip()
    desc = _mcpcn_text(raw.get("description"))
    if desc:
        entry["description"] = desc
    author = raw.get("by")
    if isinstance(author, str) and author.strip():
        entry["author"] = author.strip()
    logo = raw.get("logo")
    if isinstance(logo, str) and logo:
        entry["logo_url"] = logo if logo.startswith("http") else f"{MCPCN_ENDPOINT}{logo}"
    cats = raw.get("categories")
    if isinstance(cats, list):
        entry["categories"] = [c for c in cats if isinstance(c, str) and c]
    stars = raw.get("stars")
    entry["view_count"] = stars if isinstance(stars, int) else 0
    return entry


_INTERNAL_TLDS = (
    ".localhost",
    ".local",
    ".lan",
    ".internal",
    ".home",
    ".corp",
    ".test",
    ".invalid",
    ".example",
)
# RFC 2606 保留的文档占位域名（连同其子域一起拒）
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")


def _is_public_remote_url(url: str) -> bool:
    """远程端点必须是公网可解析主机的 http(s) URL 才算可安装变体。

    IP 字面量一律拒（平台代跑共享实例如 mcpmarket 的 8.217.x.x，不是提供方
    正式端点）；localhost/单标签主机名/内网 TLD/文档占位域名是「本机自跑」、
    docker 内部名或示例占位，作为市场安装方式对用户不可用。
    """
    host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    if not host or "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    if any(host == tld[1:] or host.endswith(tld) for tld in _INTERNAL_TLDS):
        return False
    return not any(host == d or host.endswith("." + d) for d in _RESERVED_DOMAINS)


def _drop_private_remote_variants(variants: List[MarketVariant]) -> List[MarketVariant]:
    """mcpServers 条目也可声明 url 型远程变体，同样需要公网可达才算可安装。"""
    return [
        v
        for v in variants
        if not (is_http_mcp_transport(v.get("transport", "")) and not _is_public_remote_url(v.get("url", "")))
    ]


# stdio 变体「源码检出型」检测：README 配置片段假设已 clone 仓库（相对脚本路径、
# 本机绝对脚本路径、npm start/dotnet run 这类需要项目目录的命令）——原样安装进程
# 起不来/连不上，且依赖未装，不属于可用的安装方式。
# 注意：/path/to/ 占位出现在「参数值」位（--vault /path/to/vault）是合法待填配置，
# 仅当它指向脚本文件（.py/.js 等）或本身是相对路径时才判检出。
_SCRIPT_FILE_RE = re.compile(r"\.(?:py|js|mjs|cjs|ts|go|sh|rb|ps1|php|pl|csproj|jar)$", re.IGNORECASE)
_REL_PATH_RE = re.compile(r"^\.{1,2}[\\/]|^[A-Za-z0-9_.\-]+[\\/][A-Za-z0-9_.\-\\/]+[\\/]")
_PROJECT_CMDS = {"make", "mvn", "mvnw", "gradle", "gradlew", "mix", "composer", "just", "task"}
_RUN_CMDS = {"npm", "pnpm", "yarn", "bun", "dotnet", "cargo", "go", "pipenv", "poetry"}
_RUN_SUBCMDS = {"start", "run", "dev", "build", "test", "serve", "watch"}
_UV_PROJECT_FLAGS = {"--directory", "--project", "--cwd"}


def _stdio_needs_checkout(command: str, args: List[Any]) -> bool:
    """stdio 配置需要先有仓库/项目目录才能跑 → 不能算可安装变体。"""
    cmd = command.strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
    if cmd.endswith(".exe"):
        cmd = cmd[:-4]
    if cmd in {"docker", "podman"}:
        # 镜像直拉：参数里的仓库路径/容器内文件/卷挂载都不是宿主机检出
        return False
    first = next(
        (a.strip().lower() for a in args if isinstance(a, str) and a.strip() and not a.strip().startswith("-")),
        "",
    )
    if cmd in _PROJECT_CMDS:
        return True
    if cmd in _RUN_CMDS and first in _RUN_SUBCMDS:
        return True
    if cmd == "uv" and first == "run" and any(isinstance(a, str) and a.strip() in _UV_PROJECT_FLAGS for a in args):
        return True
    for a in args:
        if not isinstance(a, str):
            continue
        a = a.strip()
        if not a or a.startswith("-") or " " in a or "=" in a or "://" in a or a.startswith("@"):
            continue
        if _SCRIPT_FILE_RE.search(a) or _REL_PATH_RE.match(a):
            return True
    return False


_HEADER_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# 上游把真实 header 值脱敏成 "***"——意味着「要带头」但不给值，需转成用户可填占位
_MASKED_HEADER_RE = re.compile(r"^\*+$")


def _header_env_name(header: str) -> str:
    """Authorization → AUTHORIZATION，x-api-key → X_API_KEY。"""
    return re.sub(r"[^A-Za-z0-9]+", "_", header).strip("_").upper()


def _normalize_market_headers(headers_raw: Dict[str, str]) -> tuple[Dict[str, str], Dict[str, str]]:
    """归一化上游 header：脱敏值 / ${VAR} → {VAR} 占位，并提取待填 env 键。

    - "***" 脱敏值 → 用户占位：Authorization 按主流约定给 Bearer 前缀，其余头全值占位
    - ${VAR} / {VAR} 统一为 {VAR}（与安装时 buildResolvedHeaders 替换规则一致）
    - 占位变量收进 env 驱动前端凭据输入框；否则字面量会被原样发出
    """
    headers: Dict[str, str] = {}
    env: Dict[str, str] = {}
    for name, value in headers_raw.items():
        v = value.replace("${", "{")
        if _MASKED_HEADER_RE.match(v.strip()):
            env_name = _header_env_name(name)
            headers[name] = f"Bearer {{{env_name}}}" if name.lower() == "authorization" else f"{{{env_name}}}"
            env[env_name] = ""
            continue
        headers[name] = v
        for var in _HEADER_VAR_RE.findall(v):
            env.setdefault(var, "")
    return headers, env


def _normalize_market_url(url: str) -> tuple[str, Dict[str, str]]:
    """URL 查询参数里的脱敏/占位凭据 → {ENV} 占位 + env 键。

    上游把需要用户自备的 key 脱敏拼进端点（如 ``?API_KEY=%2A%2A%2A``，
    解码即 "***"）——原样透传会发字面量必然 401。与 header 同一套占位语义：
    值已是 {VAR}/${VAR} 的沿用变量名，脱敏/占位值由参数名派生 env 键。
    占位以裸 {} 形式留在 URL 里（不百分编码），装时由前端填值替换、
    运行时由 render_client_kwargs 做绑定凭据替换。
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url, {}
    env: Dict[str, str] = {}
    parts: List[str] = []
    changed = False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        norm = value.replace("${", "{")
        var = _HEADER_VAR_RE.search(norm)
        if var or _MASKED_HEADER_RE.match(norm.strip()) or _PLACEHOLDER_RE.search(norm.strip()):
            env_name = var.group(1) if var else (_header_env_name(key) or key.upper())
            env.setdefault(env_name, "")
            parts.append(f"{quote_plus(key)}={{{env_name}}}")
            changed = True
        else:
            parts.append(f"{quote_plus(key)}={quote_plus(value)}")
    if not changed:
        return url, env
    return urlunparse(parsed._replace(query="&".join(parts))), env


def _normalize_remote_variant(variant: MarketVariant) -> None:
    """http 型变体：归一化 headers/URL 并把占位变量并入 env（驱动凭据输入框）。"""
    if not is_http_mcp_transport(variant.get("transport", "")):
        return
    hs_raw = variant.get("headers") or {}
    headers, env_add = _normalize_market_headers(hs_raw)
    variant["headers"] = headers
    url_raw = variant.get("url") or ""
    if url_raw:
        url, url_env = _normalize_market_url(url_raw)
        variant["url"] = url
        for k in url_env:
            env_add.setdefault(k, "")
    if env_add:
        env = variant.get("env") or {}
        for k in env_add:
            env.setdefault(k, "")
        variant["env"] = env


def _mcpcn_remote_variants(mcp_url: Any) -> List[MarketVariant]:
    """mcp_url.streamable_http_url/sse_url + header_templates → 远程变体。"""
    variants: List[MarketVariant] = []
    if not isinstance(mcp_url, dict):
        return variants
    headers_raw: Dict[str, str] = {}
    custom = mcp_url.get("custom_headers")
    if isinstance(custom, dict):
        headers_raw.update({k: v for k, v in custom.items() if isinstance(k, str) and isinstance(v, str)})
    templates = mcp_url.get("header_templates")
    if isinstance(templates, dict):
        for h_name, h_value in templates.items():
            if isinstance(h_name, str) and h_name and isinstance(h_value, str):
                headers_raw[h_name] = h_value
    headers, env = _normalize_market_headers(headers_raw)
    for key, label in (("streamable_http_url", "remote"), ("sse_url", "sse")):
        url = mcp_url.get(key)
        if isinstance(url, str) and url.startswith("http") and _is_public_remote_url(url):
            variant = MarketVariant(
                label=label,
                transport=detect_mcp_transport(url=url),
                url=url,
                headers=dict(headers),
                env=dict(env),
                command="",
                args=[],
            )
            _normalize_remote_variant(variant)
            variants.append(variant)
    return variants


def _mcpcn_stdio_variants(mcp_config: Any) -> List[MarketVariant]:
    """mcp_config.mcpServers → stdio 变体（同魔搭 server_config 解析）。"""
    variants: List[MarketVariant] = []
    if isinstance(mcp_config, str):
        try:
            mcp_config = json.loads(mcp_config)
        except (OSError, json.JSONDecodeError):
            mcp_config = None
    configs = mcp_config if isinstance(mcp_config, list) else ([mcp_config] if isinstance(mcp_config, dict) else [])
    for cfg in configs:
        if not isinstance(cfg, dict):
            continue
        servers = cfg.get("mcpServers")
        if not isinstance(servers, dict) or not servers:
            continue
        srv_name, srv_raw = next(iter(servers.items()))
        draft = parse_mcp_json_server(str(srv_name), srv_raw)
        if draft is None:
            continue
        variant = MarketVariant(
            label=draft.get("transport", MCP_TRANSPORT_STDIO),
            name=str(srv_name),
            transport=draft.get("transport", MCP_TRANSPORT_STDIO),
            command=draft.get("command", ""),
            args=draft.get("args", []),
            env=draft.get("env", {}),
            url=draft.get("url", ""),
            headers=draft.get("headers", {}),
        )
        if variant.get("transport") == MCP_TRANSPORT_STDIO and _stdio_needs_checkout(
            variant.get("command", ""), variant.get("args", [])
        ):
            variant["requires_checkout"] = True
        _normalize_remote_variant(variant)
        variants.append(variant)
    return _drop_private_remote_variants(variants)


def _mcpcn_tool_params(raw_params: Any) -> List[MarketToolParam]:
    """tools_enrich.parameters / JSON Schema properties → 参数列表。"""
    params: List[MarketToolParam] = []
    if isinstance(raw_params, list):
        for p in raw_params:
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            if not (isinstance(name, str) and name):
                continue
            item: MarketToolParam = {"name": name}
            ptype = p.get("type")
            if isinstance(ptype, str) and ptype:
                item["type"] = ptype
            item["required"] = bool(p.get("required"))
            desc = _mcpcn_text(p.get("description"))
            if not desc:
                zh = p.get("description_zh")
                desc = zh if isinstance(zh, str) else ""
            if desc:
                item["description"] = desc
            params.append(item)
    elif isinstance(raw_params, dict):
        props = raw_params.get("properties")
        required = raw_params.get("required")
        req_set = set(required) if isinstance(required, list) else set()
        if isinstance(props, dict):
            for name, spec in props.items():
                if not isinstance(name, str) or not isinstance(spec, dict):
                    continue
                item = MarketToolParam(name=name, required=name in req_set)
                ptype = spec.get("type")
                if isinstance(ptype, str) and ptype:
                    item["type"] = ptype
                desc = spec.get("description")
                if isinstance(desc, str) and desc:
                    item["description"] = desc
                params.append(item)
    return params


def _mcpcn_tools(raw: Dict[str, Any]) -> List[MarketTool]:
    """优先 tools_enrich（含中文 desc + 中文参数），退化为 OpenAI function 形态 tools。"""
    tools: List[MarketTool] = []
    enrich = raw.get("tools_enrich")
    if isinstance(enrich, list):
        for t in enrich:
            if not isinstance(t, dict):
                continue
            name = t.get("name")
            if not (isinstance(name, str) and name):
                continue
            item: MarketTool = {"name": name}
            desc = _mcpcn_text(t.get("description"))
            if not desc:
                zh = t.get("description_zh")
                desc = zh if isinstance(zh, str) else ""
            if desc:
                item["description"] = desc
            params = _mcpcn_tool_params(t.get("parameters"))
            if params:
                item["params"] = params
            tools.append(item)
        if tools:
            return tools
    plain = raw.get("tools")
    if isinstance(plain, list):
        for t in plain:
            if not isinstance(t, dict):
                continue
            fn = t.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not (isinstance(name, str) and name):
                continue
            item = MarketTool(name=name)
            desc = fn.get("description")
            if isinstance(desc, str) and desc:
                item["description"] = desc
            params = _mcpcn_tool_params(fn.get("parameters"))
            if params:
                item["params"] = params
            tools.append(item)
    return tools


def _normalize_mcpcn_detail(raw: Any) -> Optional[Dict[str, Any]]:
    """mcpmarket 详情 → detail dict（远程变体优先、readme、auth 指引）。"""
    entry = _normalize_mcpcn_item(raw)
    if entry is None or not isinstance(raw, dict):
        return None
    sid = raw["_id"]
    detail: Dict[str, Any] = dict(entry)
    variants = _mcpcn_remote_variants(raw.get("mcp_url"))
    variants += _mcpcn_stdio_variants(raw.get("mcp_config"))
    # 顶层 env {VAR: {en, zh}} → env_hints 展示；仅给已引用该变量的变体补位
    env_hints: Dict[str, str] = {}
    env_schema = raw.get("env")
    if isinstance(env_schema, dict):
        for var, desc in env_schema.items():
            if not isinstance(var, str) or not var:
                continue
            hint = _mcpcn_text(desc)
            if hint:
                env_hints[var] = hint
    for variant in variants:
        venv = variant.get("env", {})
        vheaders = variant.get("headers", {})
        refs = set(venv)
        for hv in vheaders.values():
            refs.update(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", hv))
        for var in env_hints:
            if var in refs:
                venv.setdefault(var, "")
    detail["variants"] = variants
    detail["default_variant"] = next(
        (i for i, v in enumerate(variants) if is_http_mcp_transport(v.get("transport", ""))), 0
    )
    detail["readme"] = _mcpcn_text(raw.get("detail_content")) or _mcpcn_overview_readme(raw.get("overview"))
    tools = _mcpcn_tools(raw)
    if tools:
        detail["tools"] = tools
    detail["source_url"] = _str_field(raw, "url")
    # is_hosted = 提供远程托管端点（区分纯 stdio 条目）；has_instance 是平台代跑子集
    detail["is_hosted"] = any(is_http_mcp_transport(v.get("transport", "")) for v in variants)
    detail["is_verified"] = bool(raw.get("by_hand") or raw.get("featured"))
    detail["page_url"] = f"{MCPCN_ENDPOINT}/server/{sid}"
    auth_help = raw.get("auth_help")
    detail["auth_help"] = _mcpcn_text(auth_help)
    detail["obtain_url"] = _str_field(auth_help, "obtain_url") if isinstance(auth_help, dict) else ""
    if env_hints:
        detail["env_hints"] = env_hints
    return detail


async def _fetch_mcpcn_catalog(search: str, category: str, hosted: bool) -> Optional[tuple[List[MarketEntry], int]]:
    """并发分页拉 mcpmarket 目录（≤cap 条）；category 直连上游，hosted 用 has_instance 过滤。"""

    def qs(page: int) -> str:
        params = f"page={page}&size={_MCPCN_PAGE_SIZE}"
        if search:
            params += f"&search={quote(search)}"
        if category:
            params += f"&category={quote(category)}"
        return params

    try:
        async with httpx.AsyncClient(timeout=_market_timeout()) as client:

            async def get_page(page: int) -> Optional[Dict[str, Any]]:
                resp = await client.get(f"{MCPCN_ENDPOINT}/api/servers?{qs(page)}")
                return resp.json() if resp.status_code == 200 else None

            first = await get_page(1)
            if not isinstance(first, dict):
                return None
            servers = first.get("servers")
            if not isinstance(servers, list):
                return None
            t_raw = first.get("total_servers")
            total = t_raw if isinstance(t_raw, int) else len(servers)
            total_pages = first.get("total_pages")
            raw_items: List[Any] = list(servers)
            need_pages = min(
                total_pages if isinstance(total_pages, int) else 1,
                -(-_MCPCN_CATALOG_CAP // _MCPCN_PAGE_SIZE),
            )
            if need_pages > 1:
                sem = asyncio.Semaphore(_CATALOG_PAGE_CONCURRENCY)

                async def guarded(page: int) -> Optional[Dict[str, Any]]:
                    async with sem:
                        return await get_page(page)

                rest = await asyncio.gather(*(guarded(p) for p in range(2, need_pages + 1)), return_exceptions=True)
                for r in rest:
                    items = r.get("servers") if isinstance(r, dict) else None
                    if isinstance(items, list):
                        raw_items.extend(items)
            entries: List[MarketEntry] = []
            for item in raw_items:
                if hosted and not (isinstance(item, dict) and item.get("has_instance")):
                    continue
                entry = _normalize_mcpcn_item(item)
                if entry is not None:
                    entries.append(entry)
                    if len(entries) >= _MCPCN_CATALOG_CAP:
                        break
            return entries, total
    except Exception as e:
        logger.warning(t("log.mcp.market_fetch_failed", e=e))
        return None


# 详情在途请求去重：预热任务 / 过滤续跑 / 详情端点并发查同一 key 时只打一次上游
_DETAIL_INFLIGHT: Dict[str, asyncio.Task] = {}


async def _detail_cached_fetch(
    cache_key: str, fetch: Callable[[], Coroutine[Any, Any, Optional[Dict[str, Any]]]]
) -> Optional[Dict[str, Any]]:
    """TTL 缓存 + 在途去重：命中直接返回；未命中并发调用共享同一个拉取协程。"""
    cached = _DETAIL_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _detail_ttl():
        return cached[1]
    task = _DETAIL_INFLIGHT.get(cache_key)
    if task is None:
        task = asyncio.create_task(fetch())
        _DETAIL_INFLIGHT[cache_key] = task
        task.add_done_callback(lambda _t: _DETAIL_INFLIGHT.pop(cache_key, None))
    try:
        return await task
    except Exception:
        return None


async def _get_mcpcn_detail(sid: str) -> Optional[Dict[str, Any]]:
    """mcpcn: 前缀详情；TTL 缓存 + logo 预取。"""
    global _detail_dirty
    cache_key = f"{MCPCN_ENDPOINT}|{MCPCN_PREFIX}{sid}"

    async def _fetch() -> Optional[Dict[str, Any]]:
        global _detail_dirty
        try:
            async with httpx.AsyncClient(timeout=_market_timeout()) as client:
                resp = await client.get(f"{MCPCN_ENDPOINT}/api/servers/{quote(sid, safe='')}")
            if resp.status_code != 200:
                return None
            detail = _normalize_mcpcn_detail(resp.json())
            if detail is not None:
                _DETAIL_CACHE[cache_key] = (time.time(), detail)
                _detail_dirty = True
                logo = detail.get("logo_url", "")
                if isinstance(logo, str) and logo:
                    ensure_logo_cached(logo)
            return detail
        except Exception as e:
            logger.warning(t("log.mcp.market_fetch_failed", e=e))
            return None

    return await _detail_cached_fetch(cache_key, _fetch)


# ---- 上游调用 ----


def _market_headers() -> Dict[str, str]:
    token = mcp_market_config.get_config("api_token").data
    return {"Authorization": f"Bearer {token}"} if isinstance(token, str) and token else {}


def _market_endpoint() -> str:
    endpoint = mcp_market_config.get_config("endpoint").data
    return (endpoint if isinstance(endpoint, str) else "").rstrip("/")


def _market_timeout() -> float:
    value = mcp_market_config.get_config("request_timeout_sec").data
    return float(value) if isinstance(value, (int, float)) else 10.0


async def _fetch_market_page(
    search: str, page: int, page_size: int, category: str, hosted: bool
) -> Optional[Dict[str, Any]]:
    """单页上游请求；失败返回 None（调用方决定回落或丢弃该页）。"""
    body: Dict[str, Any] = {"search": search, "page_number": page, "page_size": page_size}
    filt: Dict[str, Any] = {}
    if category:
        filt["category"] = category
    if hosted:
        filt["is_hosted"] = True
    if filt:
        body["filter"] = filt
    try:
        async with httpx.AsyncClient(timeout=_market_timeout()) as client:
            resp = await client.put(
                f"{_market_endpoint()}/openapi/v1/mcp/servers",
                json=body,
                headers=_market_headers(),
            )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        logger.warning(t("log.mcp.market_fetch_failed", e=e))
        return None


async def _fetch_market_catalog(search: str, category: str, hosted: bool) -> Optional[tuple[List[MarketEntry], int]]:
    """拉前若干页归一化条目供本地排序；返回 (entries, 上游total)，失败 None。"""
    first = await _fetch_market_page(search, 1, _CATALOG_PAGE_SIZE, category, hosted)
    if first is None:
        return None
    items = _extract_items(first)
    total = _extract_total(first, len(items))
    pages = min(_CATALOG_MAX_PAGES, max(1, -(-total // _CATALOG_PAGE_SIZE)))
    if not _has_market_token():
        # 匿名窗口：page*100 > 100 必 403，只剩第 1 页
        pages = min(pages, _ANON_LIST_WINDOW // _CATALOG_PAGE_SIZE)
    if pages > 1:
        rest = await asyncio.gather(
            *(_fetch_market_page(search, p, _CATALOG_PAGE_SIZE, category, hosted) for p in range(2, pages + 1)),
            return_exceptions=True,
        )
        for r in rest:
            if isinstance(r, dict):
                items.extend(_extract_items(r))
    entries = [e for e in (_normalize_market_item(i) for i in items) if e is not None]
    return entries, total


async def _fetch_market_detail(qualified_name: str) -> Optional[Dict[str, Any]]:
    """详情：配置了 api_token 时加 get_operational_url=true 取托管专属端点（401 回退普通请求）。"""
    url = f"{_market_endpoint()}/openapi/v1/mcp/servers/{quote(qualified_name, safe='')}"
    want_op_url = _has_market_token()
    try:
        async with httpx.AsyncClient(timeout=_market_timeout()) as client:
            resp = await client.get(
                f"{url}?get_operational_url=true" if want_op_url else url,
                headers=_market_headers(),
            )
            if resp.status_code in (401, 403) and want_op_url:
                resp = await client.get(url, headers=_market_headers())
        if resp.status_code != 200:
            return None
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning(t("log.mcp.market_fetch_failed", e=e))
        return None


def _cache_ttl() -> float:
    value = mcp_market_config.get_config("cache_ttl_sec").data
    return float(value) if isinstance(value, (int, float)) else 600.0


# ---- 磁盘持久化缓存（重启免重打上游）----
# 目录持久层保鲜放宽到 max(cache_ttl, 6h)——拉取本身便宜（并发翻页 ~3s）。
# 详情判定是重活（逐条请求），单独放宽到 72h：可装性变化慢，重启/日常重开不再冷启动。
_CACHE_DIR: Path = get_res_path("ai_core") / "mcp_market"
_CATALOG_CACHE_FILE = _CACHE_DIR / "catalog_cache.json"
_DETAIL_CACHE_FILE = _CACHE_DIR / "detail_cache.json"
_DEAD_URL_CACHE_FILE = _CACHE_DIR / "dead_url_cache.json"
_DISK_TTL_MIN = 21600.0  # 6h（目录）
_DETAIL_TTL_MIN = 259200.0  # 72h（详情判定）
_DETAIL_DISK_KEEP = 4000  # 详情缓存落盘最多保留条数（要能装下 cap 全量判定结果）
# 归一化规则版本：变体过滤/字段映射变化时 +1，旧版磁盘缓存（目录+详情）整体丢弃
_NORMALIZER_VERSION = 8
# 失效端点探测：活端点 24h 内不重复探；死端点 6h 后重探（502 可能是临时抖动）
_DEAD_URL_VERSION = 1
_ALIVE_TTL = 86400.0
_DEAD_TTL = 21600.0
_PROBE_TIMEOUT = 5.0
_DEAD_URL_KEEP = 5000
_DEAD_URL_CACHE: Dict[str, tuple[float, bool]] = {}  # url -> (ts, dead)
_caches_loaded = False
_detail_dirty = False
_dead_url_dirty = False


def _effective_ttl() -> float:
    return max(_cache_ttl(), _DISK_TTL_MIN)


def _detail_ttl() -> float:
    return max(_cache_ttl(), _DETAIL_TTL_MIN)


def _load_persisted_caches() -> None:
    """首次访问时把磁盘缓存灌回进程内缓存（结构逐字段校验）。"""
    global _caches_loaded
    if _caches_loaded:
        return
    _caches_loaded = True
    try:
        raw: Any = json.loads(_CATALOG_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    items = raw.get("items") if isinstance(raw, dict) else None
    if isinstance(raw, dict) and raw.get("version") == _NORMALIZER_VERSION and isinstance(items, dict):
        for key, value in items.items():
            if not (isinstance(key, str) and isinstance(value, list) and len(value) == 5):
                continue
            ts, entries, upstream_total, truncated, degraded = value
            if isinstance(ts, (int, float)) and isinstance(entries, list) and all(isinstance(e, dict) for e in entries):
                _CATALOG_CACHE[key] = (
                    float(ts),
                    entries,
                    upstream_total if isinstance(upstream_total, int) else 0,
                    bool(truncated),
                    bool(degraded),
                )
    try:
        raw = json.loads(_DETAIL_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    items = raw.get("items") if isinstance(raw, dict) else None
    if isinstance(raw, dict) and raw.get("version") == _NORMALIZER_VERSION and isinstance(items, dict):
        for key, value in items.items():
            if (
                isinstance(key, str)
                and isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], (int, float))
                and isinstance(value[1], dict)
            ):
                _DETAIL_CACHE[key] = (float(value[0]), value[1])
    try:
        raw = json.loads(_DEAD_URL_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    items = raw.get("items") if isinstance(raw, dict) else None
    if isinstance(raw, dict) and raw.get("version") == _DEAD_URL_VERSION and isinstance(items, dict):
        for key, value in items.items():
            if (
                isinstance(key, str)
                and isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], (int, float))
                and isinstance(value[1], bool)
            ):
                _DEAD_URL_CACHE[key] = (float(value[0]), value[1])


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _save_catalog_cache() -> None:
    """写穿目录缓存；清理 >4×TTL 的 scope 控制文件体积。"""
    try:
        cutoff = time.time() - _effective_ttl() * 4
        payload = {
            "version": _NORMALIZER_VERSION,
            "items": {k: v for k, v in _CATALOG_CACHE.items() if v[0] >= cutoff},
        }
        _write_json_atomic(_CATALOG_CACHE_FILE, payload)
    except OSError as e:
        logger.warning(t("log.mcp.market_fetch_failed", e=e))


def _flush_detail_cache() -> None:
    """详情缓存有脏标记才落盘（批量判定会一次写几百条，逐条写盘太贵）。"""
    global _detail_dirty
    if not _detail_dirty:
        return
    _detail_dirty = False
    try:
        cutoff = time.time() - _detail_ttl() * 4
        items = [(k, v) for k, v in _DETAIL_CACHE.items() if v[0] >= cutoff]
        items.sort(key=lambda kv: kv[1][0], reverse=True)
        payload = {
            "version": _NORMALIZER_VERSION,
            "items": dict(items[:_DETAIL_DISK_KEEP]),
        }
        _write_json_atomic(_DETAIL_CACHE_FILE, payload)
    except OSError as e:
        logger.warning(t("log.mcp.market_fetch_failed", e=e))


def _flush_dead_url_cache() -> None:
    """失效 URL 探测结果落盘（同详情缓存的脏标记写穿）。"""
    global _dead_url_dirty
    if not _dead_url_dirty:
        return
    _dead_url_dirty = False
    try:
        items = sorted(_DEAD_URL_CACHE.items(), key=lambda kv: kv[1][0], reverse=True)
        payload = {"version": _DEAD_URL_VERSION, "items": dict(items[:_DEAD_URL_KEEP])}
        _write_json_atomic(_DEAD_URL_CACHE_FILE, payload)
    except OSError as e:
        logger.warning(t("log.mcp.market_fetch_failed", e=e))


def _is_url_cached_dead(url: str) -> bool:
    """同步查缓存：URL 是否已被判失效（只看不探，过滤路径用）。"""
    rec = _DEAD_URL_CACHE.get(url)
    if not rec:
        return False
    ts, dead = rec
    return dead and (time.time() - ts < _DEAD_TTL)


async def _probe_url_alive(url: str) -> bool:
    """GET 探活：连接失败/超时/404/410/5xx → 失效；401/405/400 等算活（服务在，只是要鉴权/POST）。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_PROBE_TIMEOUT), follow_redirects=True) as client:
            resp = await client.get(url)
        return resp.status_code not in (404, 410) and resp.status_code < 500
    except (httpx.HTTPError, OSError):
        return False


async def _remote_url_alive(url: str) -> bool:
    """缓存感知探活：新鲜判定直接用缓存，过期/未知才发 GET。"""
    global _dead_url_dirty
    rec = _DEAD_URL_CACHE.get(url)
    now = time.time()
    if rec is not None:
        ts, dead = rec
        if now - ts < (_DEAD_TTL if dead else _ALIVE_TTL):
            return not dead
    alive = await _probe_url_alive(url)
    _DEAD_URL_CACHE[url] = (now, not alive)
    _dead_url_dirty = True
    return alive


def _has_live_variants(detail: Dict[str, Any]) -> bool:
    """详情是否还有「未被判失效」的可安装变体（过滤路径不发起网络）。"""
    vs = detail.get("variants")
    if not isinstance(vs, list):
        return False
    for v in vs:
        if not isinstance(v, dict):
            continue
        if is_http_mcp_transport(v.get("transport", "")):
            url = v.get("url", "")
            if isinstance(url, str) and _is_url_cached_dead(url):
                continue
        return True
    return False


# 单次请求的详情判定总时限：未判完的后台续跑填缓存，本轮先返回已判定的（下轮即完整）
_DETAIL_CHECK_BUDGET = 25.0
# 启动预热并发：后台任务，压低并发避免与启动期其他工作争抢
_WARM_CONCURRENCY = 8


def _peek_detail(qualified_name: str) -> Optional[Dict[str, Any]]:
    """只看缓存的详情判定，绝不发起网络请求。None=未判定。"""
    if qualified_name.startswith(f"{PRESET_SOURCE}:"):
        return _preset_detail(qualified_name[len(PRESET_SOURCE) + 1 :])
    endpoint = MCPCN_ENDPOINT if qualified_name.startswith(MCPCN_PREFIX) else _market_endpoint()
    cached = _DETAIL_CACHE.get(f"{endpoint}|{qualified_name}")
    if cached and time.time() - cached[0] < _detail_ttl():
        return cached[1]
    return None


async def _filter_installable(entries: List[MarketEntry]) -> tuple[List[MarketEntry], bool]:
    """只留有可安装变体的条目。返回 (存活, 判定未完)。

    已缓存判定直接分类；未判定的并发取详情但受 _DETAIL_CHECK_BUDGET 限制——
    超时未完的任务不取消，后台继续把判定写进详情缓存，本轮先不呈现这些条目，
    下个请求缓存命中后即得完整结果。
    """
    kept: List[MarketEntry] = []
    pending: List[MarketEntry] = []
    for e in entries:
        detail = _peek_detail(e["qualified_name"])
        if detail is None:
            pending.append(e)
        elif _has_live_variants(detail):
            kept.append(e)
    if not pending:
        return kept, False
    sem = asyncio.Semaphore(_DETAIL_CHECK_CONCURRENCY)

    async def check(entry: MarketEntry) -> bool:
        async with sem:
            detail = await _get_detail(entry["qualified_name"])
        return isinstance(detail, dict) and _has_live_variants(detail)

    tasks: Dict[asyncio.Task[bool], MarketEntry] = {asyncio.create_task(check(e)): e for e in pending}
    done, running = await asyncio.wait(tasks.keys(), timeout=_DETAIL_CHECK_BUDGET)
    if running:
        asyncio.create_task(_flush_detail_cache_when(running))
    for task in done:
        try:
            if task.result() is True:
                kept.append(tasks[task])
        except Exception:
            pass
    return kept, bool(running)


async def _flush_detail_cache_when(tasks: Set[asyncio.Task[bool]]) -> None:
    """后台判定任务全部落定后统一写盘一次（异常在此消费，不污染请求方）。"""
    await asyncio.gather(*tasks, return_exceptions=True)
    _flush_detail_cache()


async def warm_market_caches() -> None:
    """启动后台预热：把详情可装性判定填满缓存，用户首次打开市场不再等全量判定。

    低并发跑完整个基础目录；已判定（72h 内）的条目跳过，所以缓存全量命中时
    只剩一次目录拉取（~3s）。失败静默——首个真实请求会走限时判定兜底。
    """
    try:
        _load_persisted_caches()
        pending: List[MarketEntry] = []
        cat = await _fetch_mcpcn_catalog("", "", False)
        if cat is not None:
            pending.extend(e for e in cat[0] if _peek_detail(e["qualified_name"]) is None)
        fetched = None
        if _has_market_token():
            fetched = await _fetch_market_catalog("", "", False)
            if fetched is not None:
                pending.extend(e for e in fetched[0] if _peek_detail(e["qualified_name"]) is None)
        if pending:
            sem = asyncio.Semaphore(_WARM_CONCURRENCY)

            async def check(entry: MarketEntry) -> None:
                async with sem:
                    try:
                        await _get_detail(entry["qualified_name"])
                    except Exception:
                        pass

            await asyncio.gather(*(check(e) for e in pending), return_exceptions=True)
            _flush_detail_cache()
            logger.info(t("log.mcp.market_warm_done", n=len(pending)))
        # 失效端点扫描：目录全部条目的 http 变体 URL 低并发探活，死链进缓存
        urls: Set[str] = set()
        for entries_src in (cat, fetched):
            if entries_src is None:
                continue
            for e in entries_src[0]:
                d = _peek_detail(e["qualified_name"])
                if not isinstance(d, dict):
                    continue
                for v in d.get("variants", []):
                    if (
                        isinstance(v, dict)
                        and is_http_mcp_transport(v.get("transport", ""))
                        and isinstance(v.get("url"), str)
                        and v["url"]
                    ):
                        urls.add(v["url"])
        if urls:
            url_sem = asyncio.Semaphore(4)

            async def probe(url: str) -> bool:
                async with url_sem:
                    try:
                        return await _remote_url_alive(url)
                    except Exception:
                        return True  # 探针自身异常不误判失效

            results = await asyncio.gather(*(probe(u) for u in urls), return_exceptions=True)
            _flush_dead_url_cache()
            dead_n = sum(1 for r in results if r is False)
            logger.info(t("log.mcp.market_dead_probe_done", p0=len(urls), p1=dead_n))
    except Exception as e:
        logger.warning(t("log.mcp.market_fetch_failed", e=e))


async def _get_detail(qualified_name: str) -> Optional[Dict[str, Any]]:
    """带 TTL 缓存的详情获取（preset:/mcpcn: 前缀按源分发）。"""
    if qualified_name.startswith(f"{PRESET_SOURCE}:"):
        return _preset_detail(qualified_name[len(PRESET_SOURCE) + 1 :])
    if qualified_name.startswith(MCPCN_PREFIX):
        return await _get_mcpcn_detail(qualified_name[len(MCPCN_PREFIX) :])
    cache_key = f"{_market_endpoint()}|{qualified_name}"

    async def _fetch() -> Optional[Dict[str, Any]]:
        global _detail_dirty
        raw = await _fetch_market_detail(qualified_name)
        detail = _normalize_market_detail(raw) if raw is not None else None
        if detail is not None:
            _DETAIL_CACHE[cache_key] = (time.time(), detail)
            _detail_dirty = True
            logo = detail.get("logo_url", "")
            if isinstance(logo, str) and logo:
                ensure_logo_cached(logo)
        return detail

    return await _detail_cached_fetch(cache_key, _fetch)


# ---- 环境变量必填启发式 ----

# 上游 env 常见假值：<KEY> / sk-xxxx / your_api_key / changeme；search 判定（sk-xxxx 含 xxxx）
_PLACEHOLDER_RE = re.compile(
    r"(<[^>]+>|xxx+|your[_-]?(api[_-]?)?(key|token|secret)|changeme|placeholder)",
    re.IGNORECASE,
)


def _env_required(env: Dict[str, str]) -> List[str]:
    """看起来需要用户填的 env key：空值或明显占位符。"""
    required: List[str] = []
    for key, value in env.items():
        text = value.strip()
        if not text or _PLACEHOLDER_RE.search(text):
            required.append(key)
    return required


# ---- Endpoints ----


def _sort_market_entries(entries: List[MarketEntry], sort: str) -> None:
    if sort == "popular":
        entries.sort(key=lambda e: e.get("view_count", 0), reverse=True)
    elif sort == "name":
        entries.sort(key=lambda e: (e.get("chinese_name") or e.get("name") or "").lower())


def _preset_category_ok(entry: MarketEntry, category: str, hosted: bool) -> bool:
    cats = entry.get("categories") or []
    return (not category or category in cats) and not hosted


@app.get("/api/ai/mcp/market/entries", summary="MCP 广场列表", tags=MCP_CONFIG)
async def list_market_entries(
    _: Dict[str, Any] = Depends(require_admin),
    search: str = Query(""),
    category: str = Query(""),
    hosted: bool = Query(False),
    sort: str = Query("default"),
    source: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
) -> Dict[str, Any]:
    """目录（source 选源：all|mcpmarket|modelscope）→ 可安装过滤 → 本地排序分页。

    mcpmarket 目录本身均为可装条目，直接收录；魔搭需逐条取详情判定。
    分类为 mcpmarket 中文标签体系，选中时通过映射表同步过滤魔搭（无映射则跳过魔搭）；
    请求的所有源都失败才回落预设卡片并标记 degraded。
    """
    normalized_search = search.strip()
    normalized_category = category.strip()
    want_mcpcn = source != "modelscope"
    want_ms = source != "mcpmarket"
    scope_key = f"{_market_endpoint()}|{source}|{normalized_search}|{normalized_category}|{hosted}"

    _load_persisted_caches()
    partial = False
    cached = _CATALOG_CACHE.get(scope_key)
    if cached and time.time() - cached[0] < _effective_ttl():
        merged, upstream_total, truncated, degraded = cached[1], cached[2], cached[3], cached[4]
    else:
        # 分类映射：中文标签 → 魔搭 slug；用户选英文 slug 时反查中文
        zh_category = ""
        if normalized_category:
            if normalized_category in MCPCN_TO_MODELSCOPE_CATEGORY:
                zh_category = normalized_category
            else:
                zh_category = MODELSCOPE_TO_MCPCN_CATEGORY.get(normalized_category, "")
        # mcpmarket：中文标签直传；英文 slug 能反查才用，否则跳过（避免无过滤拉全量）
        cn_category_known = not normalized_category or normalized_category not in MARKET_CATEGORIES or bool(zh_category)
        cn_category = normalized_category if normalized_category not in MARKET_CATEGORIES else zh_category
        # 魔搭：英文 slug 直用；中文经映射，无映射跳过
        ms_category = normalized_category if normalized_category in MARKET_CATEGORIES else ""
        if not ms_category and zh_category:
            ms_category = MCPCN_TO_MODELSCOPE_CATEGORY.get(zh_category, "")
        ms_category_known = not normalized_category or bool(ms_category)

        async def _cn_flow() -> tuple[List[MarketEntry], int, bool, bool, bool]:
            """mcpmarket.cn 中文源：目录 → 详情判定可装。返回 (存活, 上游总数, 截断, 失败, 判定未完)。"""
            if not (want_mcpcn and cn_category_known):
                return [], 0, False, False, False
            mcn = await _fetch_mcpcn_catalog(normalized_search, cn_category, hosted)
            if mcn is None:
                return [], 0, False, True, False
            raw_entries, total = mcn
            kept, incomplete = await _filter_installable(raw_entries)
            return kept, total, total > len(raw_entries), False, incomplete

        async def _ms_flow() -> tuple[List[MarketEntry], int, bool, bool, bool]:
            """ModelScope 源：目录 → 详情判定可装。返回 (存活, 上游总数, 截断, 失败, 判定未完)。"""
            if not (want_ms and ms_category_known):
                return [], 0, False, False, False
            fetched = await _fetch_market_catalog(normalized_search, ms_category, hosted)
            if fetched is None:
                return [], 0, False, True, False
            raw_entries, total = fetched
            kept, incomplete = await _filter_installable(raw_entries)
            return kept, total, total > len(raw_entries), False, incomplete

        # 两源并行：各自判定预算并行消耗，总等待仍是单源量级
        cn_r, ms_r = await asyncio.gather(_cn_flow(), _ms_flow())
        mcpcn_entries, mcpcn_total, mcpcn_more, mcpcn_failed, cn_incomplete = cn_r
        ms_entries, ms_total, ms_truncated, ms_failed, ms_incomplete = ms_r
        partial = cn_incomplete or ms_incomplete
        merged = mcpcn_entries + ms_entries
        upstream_total = mcpcn_total + ms_total
        # 任一请求源失败 → degraded；过期缓存兜底（不刷新 ts，上游恢复即回新数据），仍空才回落预设卡片
        degraded = (want_mcpcn and mcpcn_failed) or (want_ms and ms_failed)
        if not merged and degraded:
            stale = _CATALOG_CACHE.get(scope_key)
            if stale:
                merged, upstream_total, truncated = stale[1], stale[2], stale[3]
            else:
                merged = [
                    e for e in _preset_entries(normalized_search) if _preset_category_ok(e, normalized_category, hosted)
                ]
                truncated = False
                _CATALOG_CACHE[scope_key] = (time.time(), merged, upstream_total, truncated, degraded)
                _save_catalog_cache()
        else:
            truncated = mcpcn_more or ms_truncated
            if not partial:
                # 判定未完的 scope 不写目录缓存：下个请求重拉目录（~3s）后判定点已大量命中详情缓存，返回即完整
                _CATALOG_CACHE[scope_key] = (time.time(), merged, upstream_total, truncated, degraded)
                _save_catalog_cache()
        _flush_detail_cache()

    categories = MARKET_CATEGORIES if source == "modelscope" else await _fetch_mcpcn_categories()
    _sort_market_entries(merged, sort)
    start = (page - 1) * page_size
    page_entries = merged[start : start + page_size]
    data = {
        "entries": page_entries,
        "total": len(merged),
        "upstream_total": upstream_total,
        "page": page,
        "page_size": page_size,
        "degraded": degraded,
        "categories": categories,
        "truncated": truncated,
        "window_limited": want_ms and not _has_market_token(),
        "partial": partial,
    }
    for entry in data["entries"]:
        logo = entry.get("logo_url", "")
        if isinstance(logo, str) and logo:
            ensure_logo_cached(logo)
    return {"status": 0, "msg": "ok", "data": data}


@app.get(
    "/api/ai/mcp/market/entries/{qualified_name:path}",
    summary="MCP 广场服务详情",
    tags=MCP_CONFIG,
)
async def get_market_entry_detail(
    qualified_name: str,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """服务详情：含全部传输变体（远程优先）、readme、source_url。"""
    _load_persisted_caches()
    detail = await _get_detail(qualified_name)
    _flush_detail_cache()
    if detail is None:
        return {"status": 1, "msg": f"未找到服务 '{qualified_name}' 或上游不可达", "data": None}
    return {"status": 0, "msg": "ok", "data": detail}


class MarketTransportsRequest(BaseModel):
    qualified_names: List[str]


@app.post("/api/ai/mcp/market/transports", summary="批量传输方式", tags=MCP_CONFIG)
async def market_transports(
    body: MarketTransportsRequest,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """卡片徽章用：每个服务的可用 transport 列表。详情缓存支撑，并发取详情（≤48 条）。"""
    _load_persisted_caches()
    sem = asyncio.Semaphore(12)
    result: Dict[str, List[str]] = {}

    async def one(name: str) -> None:
        async with sem:
            detail = await _get_detail(name)
        if detail is None:
            return
        variants = detail.get("variants")
        transports: List[str] = []
        if isinstance(variants, list):
            for v in variants:
                if isinstance(v, dict):
                    transport = v.get("transport")
                    if isinstance(transport, str) and transport and transport not in transports:
                        transports.append(transport)
        if transports:
            result[name] = transports

    await asyncio.gather(*(one(n) for n in body.qualified_names[:48] if isinstance(n, str)))
    _flush_detail_cache()
    return {"status": 0, "msg": "ok", "data": {"transports": result}}


class MarketInstallPreviewRequest(BaseModel):
    qualified_name: str
    variant_index: int = -1


@app.post("/api/ai/mcp/market/install-preview", summary="安装预览", tags=MCP_CONFIG)
async def market_install_preview(
    body: MarketInstallPreviewRequest,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """按选定变体生成安装草稿：只做规范化与标注，不落盘。"""
    _load_persisted_caches()
    detail = await _get_detail(body.qualified_name)
    _flush_detail_cache()
    if detail is None:
        return {"status": 1, "msg": f"未找到服务 '{body.qualified_name}' 或上游不可达", "data": None}
    variants_raw = detail.get("variants")
    variants = variants_raw if isinstance(variants_raw, list) else []
    if not variants:
        return {"status": 1, "msg": "该服务没有可用的安装方式", "data": None}
    default_index = detail.get("default_variant")
    index = (
        body.variant_index
        if 0 <= body.variant_index < len(variants)
        else (default_index if isinstance(default_index, int) else 0)
    )
    variant = variants[index]
    if not isinstance(variant, dict):
        return {"status": 1, "msg": "安装方式数据不合法", "data": None}

    # 该条目全部 http 变体并发探活（缓存 24h 内零开销），死链 URL 交给前端隐藏/拦截
    http_urls = [
        v["url"]
        for v in variants
        if isinstance(v, dict)
        and is_http_mcp_transport(v.get("transport", ""))
        and isinstance(v.get("url"), str)
        and v["url"]
    ]
    dead_urls: List[str] = []
    if http_urls:
        alive = await asyncio.gather(*(_remote_url_alive(u) for u in http_urls), return_exceptions=True)
        _flush_dead_url_cache()
        dead_urls = [u for u, ok in zip(http_urls, alive) if ok is not True]

    name = _str_field(variant, "name") or _str_field(detail, "name") or body.qualified_name
    market_meta: Dict[str, Any] = {
        "source": detail.get("source", PRESET_SOURCE),
        "qualified_name": detail.get("qualified_name", body.qualified_name),
    }
    for key in ("chinese_name", "logo_url"):
        value = detail.get(key)
        if isinstance(value, str) and value:
            market_meta[key] = value
    categories = detail.get("categories")
    if isinstance(categories, list):
        market_meta["categories"] = categories
    label = variant.get("label")
    if isinstance(label, str) and label:
        market_meta["variant_label"] = label
    source_url = _str_field(detail, "source_url") or _str_field(detail, "page_url")
    if source_url:
        market_meta["source_url"] = source_url
    requires_checkout = bool(variant.get("requires_checkout"))
    if requires_checkout:
        market_meta["requires_checkout"] = True

    env_raw = variant.get("env")
    env = dict(env_raw) if isinstance(env_raw, dict) else {}
    transport = variant.get("transport")
    resolved_transport = transport if isinstance(transport, str) else MCP_TRANSPORT_STDIO
    command = _str_field(variant, "command")
    # 市场详情里的归一化工具 → MCPToolDefinition 形状，作为创建时的初始目录：
    # 安装时连不上服务器也有一份清单，重启按目录注册并在调用时才连接。
    preview_tools: List[Dict[str, Any]] = []
    tools_raw = detail.get("tools")
    if isinstance(tools_raw, list):
        for tool in tools_raw:
            if not isinstance(tool, dict):
                continue
            tool_name = tool.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            params_raw = tool.get("params")
            parameters: Dict[str, Dict[str, Any]] = {}
            if isinstance(params_raw, list):
                for p in params_raw:
                    if not isinstance(p, dict):
                        continue
                    p_name = p.get("name")
                    if not isinstance(p_name, str) or not p_name:
                        continue
                    p_def: Dict[str, Any] = {}
                    p_type = p.get("type")
                    if isinstance(p_type, str) and p_type:
                        p_def["type"] = p_type
                    p_desc = p.get("description")
                    if isinstance(p_desc, str) and p_desc:
                        p_def["description"] = p_desc
                    if p.get("required") is True:
                        p_def["required"] = True
                    parameters[p_name] = p_def
            preview_tools.append(
                {
                    "name": tool_name,
                    "description": tool.get("description") if isinstance(tool.get("description"), str) else "",
                    "parameters": parameters,
                }
            )
    data = {
        "name": name,
        "transport": resolved_transport,
        "command": command,
        "args": variant.get("args", []),
        "url": variant.get("url", ""),
        "headers": variant.get("headers", {}),
        "env": env,
        "description": _str_field(detail, "description"),
        "market_meta": market_meta,
        "tools": preview_tools,
        "env_required": _env_required(env),
        "requires_checkout": requires_checkout,
        "cwd": "",
        "source_url": source_url,
        "runs_local_command": resolved_transport == MCP_TRANSPORT_STDIO,
        "runtime_check": (check_stdio_runtime(command) if resolved_transport == MCP_TRANSPORT_STDIO else None),
        "name_conflict": mcp_config_manager.name_in_use(name),
        "dead_urls": dead_urls,
    }
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/ai/mcp/market/logo", summary="MCP 服务 Logo 代理", tags=MCP_CONFIG)
async def get_mcp_market_logo(
    u: str = Query(""),
    _: Dict[str, Any] = Depends(require_auth),
) -> FileResponse:
    """按上游 URL 代理 logo：本地缓存命中直出，未命中现取；失败 404。"""
    url = u.strip()
    if not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=404, detail="invalid logo url")
    key = _logo_key(url)
    meta_path = _LOGO_DIR / f"{key}.json"
    if not meta_path.exists():
        await _download_logo(url, key)
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="logo fetch failed")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="logo meta invalid") from None
    if not isinstance(meta, dict):
        raise HTTPException(status_code=404, detail="logo meta invalid")
    file_name = meta.get("file")
    content_type = meta.get("content_type")
    img_path = _LOGO_DIR / file_name if isinstance(file_name, str) else None
    if img_path is None or not img_path.exists():
        raise HTTPException(status_code=404, detail="logo file missing")
    return FileResponse(
        img_path,
        media_type=content_type if isinstance(content_type, str) else "application/octet-stream",
        headers={"Cache-Control": "public, max-age=86400"},
    )
