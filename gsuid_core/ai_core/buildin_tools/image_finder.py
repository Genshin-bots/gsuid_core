"""render_agent 配图：按配置顺序逐个来源搜图，下载校验后登记成 ``img_`` 句柄。

来源只按查询词检索，不内置任何业务词表；搜什么由 render_agent 按事实包里的实体决定。
写进 HTML 的句柄都已在本机解码成功，渲染时不会再遇到死链。
"""

import re
import html
import json
import time
import asyncio
import hashlib
from io import BytesIO
from typing import TypedDict
from pathlib import Path
from dataclasses import dataclass

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.buildin_tools.html_render_tools import has_transparency

SOURCE_PLUGIN = "插件图库"
SOURCE_TAVILY = "Tavily"
SOURCE_FIRECRAWL = "Firecrawl"
SOURCE_SO360 = "360"
IMAGE_SOURCES: tuple[str, ...] = (SOURCE_PLUGIN, SOURCE_TAVILY, SOURCE_FIRECRAWL, SOURCE_SO360)

_MAX_QUERIES = 10
_MAX_PER_QUERY = 4
_MAX_SIDE = 1280
_MIN_SHORT_SIDE = 240
_MIN_LONG_SIDE = 400
_MIN_ASPECT = 0.4
_MAX_ASPECT = 2.8
_MAX_DOWNLOAD_BYTES = 8_000_000
_DOWNLOAD_TIMEOUT = 8.0
_DOWNLOAD_CONCURRENCY = 8
_PLUGIN_MIN_SCORE = 0.5
# RM 句柄 30 分钟过期，缓存必须更短，否则会交出已失效的 img_
_CACHE_TTL_SECONDS = 1200.0
_CACHE_MAX = 128

_SO360_URL = "https://image.so.com/j"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TAG_RE = re.compile(r"<[^>]+>")
_QUERY_SPLIT_RE = re.compile(r"[\s,，、·/|]+")


@dataclass(frozen=True)
class ImageCandidate:
    """一张待校验的图：``url`` / ``path`` 二选一，``fallback_url`` 在原图失败时再试。"""

    title: str
    source: str
    url: str = ""
    fallback_url: str = ""
    path: str = ""


@dataclass(frozen=True)
class FoundImage:
    resource_id: str
    width: int
    height: int
    title: str
    source: str


class _NormalizedImage(TypedDict):
    data: bytes
    width: int
    height: int


_cache: dict[str, tuple[float, list[FoundImage]]] = {}


def _clean_title(raw: str) -> str:
    text = html.unescape(_TAG_RE.sub("", raw))
    text = "".join(ch for ch in text if ch.isprintable() and ch != "\ufffd")
    return " ".join(text.split())[:28]


def _int_field(item: dict[str, object], key: str) -> int:
    if key not in item:
        return 0
    raw = item[key]
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


def _str_field(item: dict[str, object], key: str) -> str:
    raw = item[key] if key in item else ""
    return raw.strip() if isinstance(raw, str) else ""


def remote_image_candidate(
    url: str,
    title: str,
    width: int,
    height: int,
    source: str,
) -> ImageCandidate | None:
    """已知宽高不合格就丢掉；宽高未知（0）留给下载后再量。"""
    if not url.startswith(("http://", "https://")):
        return None
    if width > 0 and height > 0 and not size_acceptable(width, height):
        return None
    return ImageCandidate(title=_clean_title(title), source=source, url=url)


def size_acceptable(width: int, height: int) -> bool:
    """太小、太扁、太长的图放进卡片里都难看，直接不要。"""
    if width <= 0 or height <= 0:
        return False
    if min(width, height) < _MIN_SHORT_SIDE or max(width, height) < _MIN_LONG_SIDE:
        return False
    return _MIN_ASPECT <= width / height <= _MAX_ASPECT


def parse_so360_payload(text: str, limit: int) -> list[ImageCandidate]:
    """解析 360 图片搜索 JSON（Content-Type 是 javascript，正文是 JSON）。"""
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict) or "list" not in payload:
        return []
    items = payload["list"]
    if not isinstance(items, list):
        return []
    out: list[ImageCandidate] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item: dict[str, object] = {str(k): v for k, v in raw.items()}
        img = _str_field(item, "img")
        thumb = _str_field(item, "thumb")
        if not img.startswith(("http://", "https://")):
            img, thumb = thumb, ""
        if not img.startswith(("http://", "https://")):
            continue
        w, h = _int_field(item, "width"), _int_field(item, "height")
        if w and h and not size_acceptable(w, h):
            continue
        out.append(
            ImageCandidate(
                title=_clean_title(_str_field(item, "title")),
                source=SOURCE_SO360,
                url=img,
                fallback_url=thumb if thumb.startswith(("http://", "https://")) else "",
            )
        )
        if len(out) >= limit:
            break
    return out


def normalize_image(data: bytes) -> _NormalizedImage | None:
    """解码 → 摆正 EXIF 方向 → 校验尺寸 → 缩到 1280 边长；不透明图出 JPEG。"""
    # 只兜外部图片解码失败（损坏字节 / HTML 错误页 / 超大图）
    try:
        im = Image.open(BytesIO(data))
        im.load()
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError):
        return None
    im = ImageOps.exif_transpose(im)
    if not size_acceptable(im.width, im.height):
        return None
    if max(im.size) > _MAX_SIDE:
        im.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.LANCZOS)
    buf = BytesIO()
    if has_transparency(im):
        im.save(buf, format="PNG", optimize=True)
    else:
        im.convert("RGB").save(buf, format="JPEG", quality=86, optimize=True)
    return {"data": buf.getvalue(), "width": im.width, "height": im.height}


async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    if not url:
        return None
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400 or not resp.content or len(resp.content) > _MAX_DOWNLOAD_BYTES:
        return None
    return resp.content


@to_thread
def _read_local(path: str) -> bytes | None:
    p = Path(path)
    if not p.is_file() or p.stat().st_size > _MAX_DOWNLOAD_BYTES:
        return None
    return p.read_bytes()


_normalize_in_thread = to_thread(normalize_image)


def plugin_image_matches(query: str, described: str) -> bool:
    """向量分数挡不住「同游戏另一个角色」，所以对象词必须出现在图片描述里。

    查询约定「范围词 对象词…」：插件图的描述通常不带范围词，只要求第一个词之后的词全部出现。
    """
    words = [w for w in _QUERY_SPLIT_RE.split(query.lower()) if w]
    required = words[1:] if len(words) > 1 else words
    text = described.lower()
    return bool(required) and all(w in text for w in required)


def _payload_text(payload: dict[str, object]) -> str:
    parts: list[str] = []
    for key in ("content", "plugin", "path"):
        raw = payload[key] if key in payload else ""
        if isinstance(raw, str):
            parts.append(Path(raw).stem if key == "path" else raw)
    tags = payload["tags"] if "tags" in payload else []
    if isinstance(tags, list):
        parts.extend(tag for tag in tags if isinstance(tag, str))
    return " ".join(parts)


async def _plugin_candidates(query: str, limit: int) -> list[ImageCandidate]:
    from gsuid_core.ai_core.rag import search_images

    points = await search_images(query=query, limit=limit * 3)
    out: list[ImageCandidate] = []
    for point in points:
        raw = point.payload
        if raw is None or point.score < _PLUGIN_MIN_SCORE or "path" not in raw:
            continue
        payload: dict[str, object] = {str(k): v for k, v in raw.items()}
        path = payload["path"]
        if not isinstance(path, str) or not path:
            continue
        if not plugin_image_matches(query, _payload_text(payload)):
            continue
        content = payload["content"] if "content" in payload else ""
        title = _clean_title(content) if isinstance(content, str) else ""
        out.append(ImageCandidate(title=title, source=SOURCE_PLUGIN, path=path))
    return out


def _tavily_keys() -> list[str]:
    from gsuid_core.ai_core.web_search.tavily_search import _get_api_key_pool

    return _get_api_key_pool()


def _tavily_image_urls(query: str, api_key: str) -> list[ImageCandidate]:
    from tavily import TavilyClient

    response: object = TavilyClient(api_key=api_key).search(
        query=query,
        max_results=3,
        search_depth="basic",
        include_images=True,
        include_image_descriptions=True,
    )
    if not isinstance(response, dict) or "images" not in response:
        return []
    images = response["images"]
    if not isinstance(images, list):
        return []
    out: list[ImageCandidate] = []
    for raw in images:
        url, desc = "", ""
        if isinstance(raw, str):
            url = raw.strip()
        elif isinstance(raw, dict):
            item: dict[str, object] = {str(k): v for k, v in raw.items()}
            url, desc = _str_field(item, "url"), _str_field(item, "description")
        if url.startswith(("http://", "https://")):
            out.append(ImageCandidate(title=_clean_title(desc), source=SOURCE_TAVILY, url=url))
    return out


_tavily_image_urls_async = to_thread(_tavily_image_urls)


async def _tavily_candidates(query: str, limit: int) -> list[ImageCandidate]:
    keys = _tavily_keys()
    if not keys:
        return []
    found = await _tavily_image_urls_async(query, keys[0])
    return found[: limit * 3]


def _firecrawl_keys() -> list[str]:
    from gsuid_core.ai_core.web_search.firecrawl_search import _get_api_key_pool

    return _get_api_key_pool()


def _firecrawl_image_urls(query: str, api_key: str | None, limit: int) -> list[ImageCandidate]:
    """走 v2 同步客户端。4.49.3 的 Firecrawl()/AsyncFirecrawl() 会先建 v1，没 Key 就抛错。"""
    from firecrawl.v2.types import SearchResultImages
    from firecrawl.v2.client import FirecrawlClient

    client = FirecrawlClient(api_key=api_key) if api_key else FirecrawlClient()
    response = client.search(query, sources=["images"], limit=max(limit * 3, 1))
    images = response.images
    if images is None:
        return []
    out: list[ImageCandidate] = []
    for item in images:
        if not isinstance(item, SearchResultImages):
            continue
        width = item.image_width if isinstance(item.image_width, int) else 0
        height = item.image_height if isinstance(item.image_height, int) else 0
        cand = remote_image_candidate(
            item.image_url if item.image_url else "",
            item.title if item.title else "",
            width,
            height,
            SOURCE_FIRECRAWL,
        )
        if cand is None:
            continue
        out.append(cand)
        if len(out) >= limit * 3:
            break
    return out


_firecrawl_image_urls_async = to_thread(_firecrawl_image_urls)


async def _firecrawl_candidates(query: str, limit: int) -> list[ImageCandidate]:
    keys = _firecrawl_keys()
    found = await _firecrawl_image_urls_async(query, keys[0] if keys else None, limit)
    return found[: limit * 3]


async def _so360_candidates(client: httpx.AsyncClient, query: str, limit: int) -> list[ImageCandidate]:
    resp = await client.get(
        _SO360_URL,
        params={"q": query, "src": "srp", "pn": str(limit * 3), "sn": "0"},
        headers={"Referer": "https://image.so.com/"},
    )
    if resp.status_code >= 400:
        return []
    return parse_so360_payload(resp.text, limit * 3)


async def _candidates_from(
    source: str,
    client: httpx.AsyncClient,
    query: str,
    limit: int,
) -> list[ImageCandidate]:
    # 外部来源（向量库 / 第三方接口）出错只记日志，换下一个来源，不打断出图
    try:
        if source == SOURCE_PLUGIN:
            return await _plugin_candidates(query, limit)
        if source == SOURCE_TAVILY:
            return await _tavily_candidates(query, limit)
        if source == SOURCE_FIRECRAWL:
            return await _firecrawl_candidates(query, limit)
        if source == SOURCE_SO360:
            return await _so360_candidates(client, query, limit)
    except Exception as e:
        logger.warning(t("log.ai.find_images_source_fail", source=source, query=query, e=e))
    return []


class _Verifier:
    """单次调用内共享：限并发下载，并按原始字节去重（同图不同链接只留一张）。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._sem = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)
        self._seen: set[str] = set()

    async def _accept(self, data: bytes | None) -> _NormalizedImage | None:
        if data is None:
            return None
        digest = hashlib.md5(data).hexdigest()
        # 先登记再解码：并发的同图请求在 await 期间也会被挡掉
        if digest in self._seen:
            return None
        self._seen.add(digest)
        return await _normalize_in_thread(data)

    async def verify(self, cand: ImageCandidate) -> FoundImage | None:
        async with self._sem:
            if cand.path:
                norm = await self._accept(await _read_local(cand.path))
            else:
                norm = await self._accept(await _download(self._client, cand.url))
                if norm is None and cand.fallback_url:
                    norm = await self._accept(await _download(self._client, cand.fallback_url))
        if norm is None:
            return None
        rid = RM.register(norm["data"])
        return FoundImage(rid, norm["width"], norm["height"], cand.title, cand.source)


def _configured_sources() -> list[str]:
    raw = ai_config.get_config("render_image_sources").data
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, str) and s in IMAGE_SOURCES]


def _cache_get(key: str) -> list[FoundImage] | None:
    hit = _cache[key] if key in _cache else None
    if hit is None or time.monotonic() - hit[0] > _CACHE_TTL_SECONDS:
        return None
    return hit[1]


def _cache_put(key: str, found: list[FoundImage]) -> None:
    if len(_cache) >= _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]
    _cache[key] = (time.monotonic(), found)


async def _find_for_query(
    query: str,
    need: int,
    sources: list[str],
    client: httpx.AsyncClient,
    verifier: _Verifier,
) -> list[FoundImage]:
    key = f"{need}|{query}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    found: list[FoundImage] = []
    for source in sources:
        cands = await _candidates_from(source, client, query, need)
        # 多验几张备选：外链常有失效 / 防盗链
        for i in range(0, len(cands), need + 2):
            batch = cands[i : i + need + 2]
            results = await asyncio.gather(*(verifier.verify(c) for c in batch))
            found.extend(r for r in results if r is not None)
            if len(found) >= need:
                break
        if len(found) >= need:
            break
    found = found[:need]
    if found:
        _cache_put(key, found)
    logger.info(t("log.ai.find_images_query_done", query=query, p0=len(found), p1=need))
    return found


def _orientation(img: FoundImage) -> str:
    ratio = img.width / img.height
    if ratio >= 1.2:
        return "横图"
    if ratio <= 0.83:
        return "竖图"
    return "方图"


def format_found(results: list[tuple[str, list[FoundImage]]]) -> str:
    lines = ['配图（已下载校验，30 分钟内有效；写 <img src="img_xxx">，放进带圆角的图片容器）：']
    for query, found in results:
        if not found:
            lines.append(f"【{query}】没找到能用的图。换更具体的词再搜一次，或这一节改用 icon: 图标。")
            continue
        lines.append(f"【{query}】")
        for img in found:
            caption = f" 「{img.title}」" if img.title else ""
            lines.append(f"- {img.resource_id} {_orientation(img)} {img.width}×{img.height}{caption}")
    return "\n".join(lines)


@ai_tools(category="media", capability_domain="资料出图", code_callable=False)
async def find_images(
    ctx: RunContext[ToolContext],
    queries: list[str],
    per_query: int = 2,
) -> str:
    """为出图找配图：一次传入多个搜索词，返回已下载校验的 ``img_`` 句柄。

    每个主要对象（景点 / 菜品 / 角色 / 英雄 / 公司 / 产品 / 场景）一个词，范围词在前、对象名在后，
    例如「台州 神仙居」「温岭 嵌糕」「原神 芙宁娜」「英雄联盟 亚索 皮肤」「贵州茅台 酒厂」。
    来源按配置依次尝试：插件图库 → Firecrawl（没 Key 走免费档）→ 360 图片搜索 → Tavily（配了 Key 时）。
    只用于找图，不要拿它查事实。

    Args:
        ctx: 工具执行上下文
        queries: 搜索词列表，最多 10 个；头图词放第一个
        per_query: 每个词要几张，默认 2，最多 4

    Returns:
        每个词对应的 ``img_`` 句柄、横竖、尺寸和图注；没找到时给出替代建议
    """
    _ = ctx
    from gsuid_core.ai_core.configs.ai_config import render_embed_images_enabled

    if not render_embed_images_enabled():
        return "配图已关闭。不要再调 find_images。页头用 gs-hero plain，对象用图标，不要留空图框。"
    cleaned = list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:_MAX_QUERIES]
    if not cleaned:
        return "⚠️ queries 不能为空：每个主要对象传一个具体的搜索词。"
    sources = _configured_sources()
    if not sources:
        return "配图来源已在配置里关闭（render_image_sources 为空）。这次用 icon: 图标和渐变头图排版。"
    need = min(max(per_query, 1), _MAX_PER_QUERY)
    async with httpx.AsyncClient(
        timeout=_DOWNLOAD_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
    ) as client:
        verifier = _Verifier(client)
        found_lists = await asyncio.gather(*(_find_for_query(q, need, sources, client, verifier) for q in cleaned))
    paired = list(zip(cleaned, found_lists))
    deps = ctx.deps
    if isinstance(deps, ToolContext):
        deps.extra["find_images_ran"] = True
        deps.extra["find_images_found"] = any(found for _query, found in paired)
    return format_found(paired)
