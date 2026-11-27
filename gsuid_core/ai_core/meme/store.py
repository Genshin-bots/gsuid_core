"""CNB 表情包市场：远程目录缓存、预览代理缓存、单分支按需克隆导入、更新、卸载与人格挂载。"""

from __future__ import annotations

import os
import re
import json
import stat
import time
import uuid
import shutil
import asyncio
import hashlib
from typing import Literal, TypedDict
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import quote
from collections.abc import Sequence

import httpx2

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH, get_res_path
from gsuid_core.utils.path_safety import (
    PathEscapeError,
    safe_join,
    is_safe_relpath,
    is_safe_filename,
    validate_install_source_url,
)
from gsuid_core.ai_core.meme.config import meme_config
from gsuid_core.ai_core.meme.library import (
    MemeLibrary,
    ext_for_mime,
    compute_meme_id,
    get_folder_path,
    write_member_payloads,
)
from gsuid_core.ai_core.meme.normalize import (
    STORE_SHORT_SIDE,
    LEGACY_SHORT_SIDE,
    prepare_meme_image,
)
from gsuid_core.ai_core.persona.bundle import BundleMemeTags
from gsuid_core.ai_core.persona.models import SUPPORTED_IMAGE_FORMATS
from gsuid_core.ai_core.meme.pack_model import MAX_PACKS_PER_PERSONA, PackStore
from gsuid_core.ai_core.persona.persona import Persona
from gsuid_core.ai_core.meme.database_model import AiMemeRecord

MEME_STORE_REPO = "https://cnb.cool/gscore-meme/gscore-meme"
MEME_STORE_CATALOG_URL = f"{MEME_STORE_REPO}/-/git/raw/main/meme_list.json"
MEME_STORE_RAW_BASE = f"{MEME_STORE_REPO}/-/git/raw"

# 可信市场仓库白名单（扩展口）：当前仅官方单仓库；后续支持多仓库市场时往此追加，
# 条目的 repo 命中白名单才允许被预览代理与克隆安装使用。
MEME_STORE_TRUSTED_REPOS: tuple[str, ...] = (MEME_STORE_REPO,)

_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

MEME_STORE_DIR = get_res_path(AI_CORE_PATH / "meme_store")
MEME_STORE_REGISTRY_PATH = MEME_STORE_DIR / "installed.json"
MEME_STORE_TMP_DIR = MEME_STORE_DIR / "tmp"
MEME_STORE_CACHE_DIR = MEME_STORE_DIR / "cache"

_CATALOG_TTL_SEC = 600.0
_CACHE_MAX_BYTES = 64 * 1024 * 1024
_HTTP_TIMEOUT_SEC = 20.0
_MAX_PREVIEW_BYTES = 8 * 1024 * 1024

_IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

AssetKind = Literal["cover", "sheet", "item"]


class CatalogPackEntry(TypedDict):
    id: str
    name: str
    series: str
    tags: list[str]
    alias: list[str]
    count: int
    size_kb: int
    repo: str
    branch: str
    commit: str
    cover: str
    sheet: str
    cover_path: str
    sheet_path: str
    has_tags: bool


class InstalledMemePack(TypedDict):
    store_id: str
    pack_id: str
    pack_name: str
    repo_url: str
    branch: str
    commit: str
    series: str
    meme_ids: list[str]
    installed_at: float


class MemeStoreTagItem(TypedDict):
    file: str
    title: str
    description: str
    ocr_text: str
    emotion_tags: list[str]
    custom_tags: list[str]
    usage_hint: str
    image_url: str


class MemeStorePackDetail(TypedDict):
    id: str
    name: str
    series: str
    count: int
    size_kb: int
    repo: str
    branch: str
    commit: str
    attribution: str
    sheet_url: str
    cover_url: str
    items: list[MemeStoreTagItem]


class MemeStoreInstallResult(TypedDict):
    store_id: str
    pack_id: str
    pack_name: str
    imported: int
    linked: int
    reused: int
    untagged: int
    rejected: int
    bound_personas: list[str]


class MemeStoreUpdateResult(TypedDict):
    store_id: str
    pack_id: str
    pack_name: str
    commit: str
    added: int
    removed: int
    total: int


class MemeStoreUninstallResult(TypedDict):
    store_id: str
    pack_id: str
    pack_name: str
    deleted_memes: int
    lost_personas: list[str]


class PackPersonasBindResult(TypedDict):
    pack_id: str
    pack_name: str
    bound_personas: list[str]


_catalog_cache: list[CatalogPackEntry] = []
_catalog_fetched_at: float = 0.0
_catalog_lock = asyncio.Lock()
_registry_lock = asyncio.Lock()


def _str_val(mapping: dict[str, object], key: str, default: str = "") -> str:
    if key not in mapping:
        return default
    raw = mapping[key]
    return raw.strip() if isinstance(raw, str) else default


def _int_val(mapping: dict[str, object], key: str, default: int = 0) -> int:
    if key not in mapping:
        return default
    raw = mapping[key]
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    return default


def _float_val(mapping: dict[str, object], key: str, default: float = 0.0) -> float:
    if key not in mapping:
        return default
    raw = mapping[key]
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        return float(raw)
    return default


def _bool_val(mapping: dict[str, object], key: str, default: bool = False) -> bool:
    if key not in mapping:
        return default
    raw = mapping[key]
    return raw if isinstance(raw, bool) else default


def _str_list_val(mapping: dict[str, object], key: str) -> list[str]:
    if key not in mapping:
        return []
    raw = mapping[key]
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _as_dict(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    return {str(k): v for k, v in raw.items()}


def _trusted_market_repo(repo: str) -> str | None:
    """市场条目仓库必须命中白名单；命中返回规范化地址，否则 None。"""
    clean = repo.strip().rstrip("/")
    if clean.lower().endswith(".git"):
        clean = clean[:-4]
    for trusted in MEME_STORE_TRUSTED_REPOS:
        if clean.lower() == trusted.rstrip("/").lower():
            return trusted.rstrip("/")
    return None


def _is_safe_ref_name(name: str) -> bool:
    """宽松 git ref 校验：禁路径穿越片段，防止拼进 raw URL 后改变路径语义。"""
    if not _SAFE_REF_RE.match(name):
        return False
    return ".." not in name and "//" not in name and not name.endswith(("/", "."))


class MemePackImportError(Exception):
    """包内图片无法解码或已损坏；消息面向控制台用户。"""


def _remove_dir_force(path: Path) -> None:
    """删除临时克隆目录；先解除只读位，兼容 Windows .git 对象文件。"""
    if not path.exists():
        return
    for item in path.rglob("*"):
        try:
            os.chmod(item, stat.S_IWRITE)
        except OSError:
            pass
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    shutil.rmtree(path)


def _trim_cache_dir(cache_dir: Path, max_bytes: int = _CACHE_MAX_BYTES) -> None:
    if not cache_dir.is_dir():
        return
    files: list[tuple[float, int, Path]] = []
    total = 0
    for item in cache_dir.rglob("*"):
        if not item.is_file():
            continue
        try:
            st = item.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, item))
        total += st.st_size
    if total <= max_bytes:
        return
    files.sort(key=lambda row: row[0])
    for _mtime, size, path in files:
        if total <= max_bytes:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


def _parse_catalog_payload(raw: object) -> list[CatalogPackEntry]:
    root = _as_dict(raw)
    if root is None or "packs" not in root:
        return []
    raw_packs = root["packs"]
    if not isinstance(raw_packs, list):
        return []
    default_repo = _str_val(root, "repo", MEME_STORE_REPO).rstrip("/")
    entries: list[CatalogPackEntry] = []
    for item in raw_packs:
        mapping = _as_dict(item)
        if mapping is None:
            continue
        pack_id = _str_val(mapping, "id")
        if not pack_id or not is_safe_filename(pack_id):
            continue
        repo_raw = _str_val(mapping, "repo", default_repo).rstrip("/")
        trusted = _trusted_market_repo(repo_raw)
        branch = _str_val(mapping, "branch", f"pack/{pack_id}")
        # 目录是远程不可信输入：仓库必须命中白名单、分支必须是安全 ref，
        # 否则预览代理与克隆会被目录内容牵引到任意主机。
        if trusted is None or not _is_safe_ref_name(branch):
            logger.warning(t("log.meme.store_catalog_entry_drop", store_id=pack_id, repo=repo_raw))
            continue
        repo = trusted
        cover_path = _str_val(mapping, "cover_path", f"covers/{pack_id}.webp")
        sheet_path = _str_val(mapping, "sheet_path", f"sheets/{pack_id}.webp")
        # 相对路径直接拼进 raw URL：拒绝穿越段，防止在可信主机内逃逸出仓库路径前缀
        if not is_safe_relpath(cover_path) or not is_safe_relpath(sheet_path):
            logger.warning(t("log.meme.store_catalog_entry_drop", store_id=pack_id, repo=repo_raw))
            continue
        entries.append(
            CatalogPackEntry(
                id=pack_id,
                name=_str_val(mapping, "name", pack_id),
                series=_str_val(mapping, "series"),
                tags=_str_list_val(mapping, "tags"),
                alias=_str_list_val(mapping, "alias"),
                count=_int_val(mapping, "count"),
                size_kb=_int_val(mapping, "size_kb"),
                repo=repo,
                branch=branch,
                commit=_str_val(mapping, "commit"),
                cover=_str_val(mapping, "cover", f"{repo}/-/git/raw/main/{cover_path}"),
                sheet=_str_val(mapping, "sheet", f"{repo}/-/git/raw/main/{sheet_path}"),
                cover_path=cover_path,
                sheet_path=sheet_path,
                has_tags=_bool_val(mapping, "has_tags", True),
            )
        )
    return entries


async def fetch_catalog(*, force_refresh: bool = False) -> list[CatalogPackEntry]:
    """从 CNB 拉取表情包市场总索引并做进程内 TTL 缓存。"""
    global _catalog_cache, _catalog_fetched_at

    now = time.monotonic()
    if not force_refresh and _catalog_cache and (now - _catalog_fetched_at) < _CATALOG_TTL_SEC:
        return list(_catalog_cache)

    async with _catalog_lock:
        now = time.monotonic()
        if not force_refresh and _catalog_cache and (now - _catalog_fetched_at) < _CATALOG_TTL_SEC:
            return list(_catalog_cache)
        async with httpx2.AsyncClient(follow_redirects=True) as client:
            response = await client.get(MEME_STORE_CATALOG_URL, timeout=_HTTP_TIMEOUT_SEC)
            response.raise_for_status()
            parsed = _parse_catalog_payload(response.json())
        _catalog_cache = parsed
        _catalog_fetched_at = time.monotonic()
        return list(_catalog_cache)


def clear_catalog_cache() -> None:
    global _catalog_cache, _catalog_fetched_at
    _catalog_cache = []
    _catalog_fetched_at = 0.0


def _read_registry_sync() -> dict[str, InstalledMemePack]:
    if not MEME_STORE_REGISTRY_PATH.is_file():
        return {}
    try:
        raw = json.loads(MEME_STORE_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    root = _as_dict(raw)
    if root is None:
        return {}
    out: dict[str, InstalledMemePack] = {}
    for store_id, val in root.items():
        mapping = _as_dict(val)
        if mapping is None:
            continue
        pack_id = _str_val(mapping, "pack_id")
        if not pack_id:
            continue
        out[store_id] = InstalledMemePack(
            store_id=_str_val(mapping, "store_id", store_id),
            pack_id=pack_id,
            pack_name=_str_val(mapping, "pack_name", store_id),
            repo_url=_str_val(mapping, "repo_url"),
            branch=_str_val(mapping, "branch", "main"),
            commit=_str_val(mapping, "commit"),
            series=_str_val(mapping, "series"),
            meme_ids=_str_list_val(mapping, "meme_ids"),
            installed_at=_float_val(mapping, "installed_at"),
        )
    return out


def _write_registry_sync(registry: dict[str, InstalledMemePack]) -> None:
    MEME_STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = MEME_STORE_REGISTRY_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, MEME_STORE_REGISTRY_PATH)


async def reconcile_installed() -> dict[str, InstalledMemePack]:
    """对齐本地安装登记与 PackStore：本地包已删则自动摘除陈旧登记。"""
    async with _registry_lock:
        registry = await asyncio.to_thread(_read_registry_sync)
        if not registry:
            return {}
        changed = False
        cleaned: dict[str, InstalledMemePack] = {}
        for store_id, item in registry.items():
            pack = await PackStore.get_pack(item["pack_id"])
            if pack is None:
                # 包被市场外路径（如表情库直接删包）删除：登记丢失前兜底清理纯市场孤儿图
                _, survivors = await cleanup_market_orphans(item["meme_ids"])
                await _refresh_survivor_payloads(survivors)
                changed = True
                continue
            if pack.name != item["pack_name"]:
                item = InstalledMemePack(
                    store_id=item["store_id"],
                    pack_id=item["pack_id"],
                    pack_name=pack.name,
                    repo_url=item["repo_url"],
                    branch=item["branch"],
                    commit=item["commit"],
                    series=item["series"],
                    meme_ids=item["meme_ids"],
                    installed_at=item["installed_at"],
                )
                changed = True
            cleaned[store_id] = item
        if changed:
            await asyncio.to_thread(_write_registry_sync, cleaned)
        return cleaned


async def _save_installed_entry(entry: InstalledMemePack) -> None:
    async with _registry_lock:
        registry = await asyncio.to_thread(_read_registry_sync)
        registry[entry["store_id"]] = entry
        await asyncio.to_thread(_write_registry_sync, registry)


async def _delete_installed_entry(store_id: str) -> None:
    async with _registry_lock:
        registry = await asyncio.to_thread(_read_registry_sync)
        if store_id in registry:
            del registry[store_id]
            await asyncio.to_thread(_write_registry_sync, registry)


def _raw_url_for_entry(entry: CatalogPackEntry, branch: str, rel_path: str) -> str:
    repo = entry["repo"].rstrip("/")
    clean_rel = rel_path.lstrip("/")
    return f"{repo}/-/git/raw/{branch}/{clean_rel}"


def _cache_token(commit: str) -> str:
    token = "".join(ch for ch in commit if ch.isalnum())[:12]
    return token or "latest"


async def _find_catalog_entry(store_id: str) -> CatalogPackEntry | None:
    if not is_safe_filename(store_id):
        return None
    entries = await fetch_catalog()
    for entry in entries:
        if entry["id"] == store_id:
            # 兜底校验：即使条目绕过了目录解析（如测试注入），不可信来源也一律不可用
            if _trusted_market_repo(entry["repo"]) is None or not _is_safe_ref_name(entry["branch"]):
                return None
            return entry
    return None


async def fetch_preview_asset(
    kind: AssetKind,
    store_id: str,
    filename: str = "",
) -> tuple[Path | None, str, str | None]:
    """代理并本地缓存 CNB 预览图（封面 / 整包一览图 / 单张表情）。"""
    entry = await _find_catalog_entry(store_id)
    if entry is None:
        return None, "application/octet-stream", "表情包市场内不存在该条目"

    ver = _cache_token(entry["commit"])
    if kind == "cover":
        remote_url = _raw_url_for_entry(entry, "main", entry["cover_path"])
        rel_cache = f"covers/{store_id}_{ver}.webp"
        mime = "image/webp"
    elif kind == "sheet":
        remote_url = _raw_url_for_entry(entry, "main", entry["sheet_path"])
        rel_cache = f"sheets/{store_id}_{ver}.webp"
        mime = "image/webp"
    else:
        if not is_safe_filename(filename):
            return None, "application/octet-stream", "文件名无效"
        suffix = Path(filename).suffix.lower()
        if suffix not in _IMAGE_MIME:
            return None, "application/octet-stream", "不支持的图片格式"
        remote_url = _raw_url_for_entry(entry, entry["branch"], filename)
        rel_cache = f"items/{store_id}_{ver}/{filename}"
        mime = _IMAGE_MIME[suffix]

    try:
        cache_path = safe_join(MEME_STORE_CACHE_DIR, rel_cache)
    except PathEscapeError:
        return None, "application/octet-stream", "缓存路径非法"

    if cache_path.is_file() and cache_path.stat().st_size > 0:
        return cache_path, mime, None

    try:
        async with httpx2.AsyncClient(follow_redirects=True) as client:
            response = await client.get(remote_url, timeout=_HTTP_TIMEOUT_SEC)
            response.raise_for_status()
            data = response.content
    except Exception as e:
        logger.warning(t("log.meme.store_asset_fetch_fail", store_id=store_id, e=e))
        return None, mime, f"获取预览资源失败: {e}"

    if not data or len(data) > _MAX_PREVIEW_BYTES:
        return None, mime, "预览资源为空或超出大小限制"

    def _write_asset() -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, cache_path)
        _trim_cache_dir(MEME_STORE_CACHE_DIR)

    await asyncio.to_thread(_write_asset)
    return cache_path, mime, None


def _parse_tags_preview_list(raw: object, store_id: str) -> list[MemeStoreTagItem]:
    if not isinstance(raw, list):
        return []
    items: list[MemeStoreTagItem] = []
    for entry in raw:
        mapping = _as_dict(entry)
        if mapping is None:
            continue
        file_name = _str_val(mapping, "file")
        if not file_name or not is_safe_filename(file_name):
            continue
        items.append(
            MemeStoreTagItem(
                file=file_name,
                title=_str_val(mapping, "title")[:64],
                description=_str_val(mapping, "description")[:512],
                ocr_text=_str_val(mapping, "ocr_text")[:256],
                emotion_tags=_str_list_val(mapping, "emotion_tags"),
                custom_tags=_str_list_val(mapping, "custom_tags"),
                usage_hint=_str_val(mapping, "usage_hint")[:512],
                image_url=f"/api/meme-store/asset/item/{store_id}/{quote(file_name, safe='')}",
            )
        )
    return items


async def fetch_pack_detail(store_id: str) -> tuple[MemeStorePackDetail | None, str | None]:
    """拉取并缓存单个市场包的 tags.json 与 manifest.json 详情（免 clone 预览全包）。"""
    entry = await _find_catalog_entry(store_id)
    if entry is None:
        return None, "表情包市场内不存在该条目"

    ver = _cache_token(entry["commit"])
    try:
        meta_cache = safe_join(MEME_STORE_CACHE_DIR, f"meta/{store_id}_{ver}.json")
    except PathEscapeError:
        return None, "缓存路径非法"

    tags_raw: object = []
    attribution = ""
    if meta_cache.is_file():
        try:
            cached_obj = _as_dict(json.loads(meta_cache.read_text(encoding="utf-8")))
            if cached_obj is not None and "tags" in cached_obj:
                tags_raw = cached_obj["tags"]
                attribution = _str_val(cached_obj, "attribution")
        except (OSError, json.JSONDecodeError):
            tags_raw = []

    if not tags_raw:
        tags_url = _raw_url_for_entry(entry, entry["branch"], "tags.json")
        manifest_url = _raw_url_for_entry(entry, entry["branch"], "manifest.json")
        try:
            async with httpx2.AsyncClient(follow_redirects=True) as client:
                tags_resp = await client.get(tags_url, timeout=_HTTP_TIMEOUT_SEC)
                tags_resp.raise_for_status()
                tags_raw = tags_resp.json()
                try:
                    manifest_resp = await client.get(manifest_url, timeout=_HTTP_TIMEOUT_SEC)
                    if manifest_resp.status_code == 200:
                        m_obj = _as_dict(manifest_resp.json())
                        if m_obj is not None:
                            attribution = _str_val(m_obj, "attribution")
                except Exception:
                    attribution = ""
        except Exception as e:
            logger.warning(t("log.meme.store_detail_fetch_fail", store_id=store_id, e=e))
            return None, f"获取表情包详情失败: {e}"

        def _write_meta() -> None:
            meta_cache.parent.mkdir(parents=True, exist_ok=True)
            payload = {"tags": tags_raw, "attribution": attribution}
            tmp = meta_cache.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, meta_cache)

        await asyncio.to_thread(_write_meta)

    return (
        MemeStorePackDetail(
            id=entry["id"],
            name=entry["name"],
            series=entry["series"],
            count=entry["count"],
            size_kb=entry["size_kb"],
            repo=entry["repo"],
            branch=entry["branch"],
            commit=entry["commit"],
            attribution=attribution,
            sheet_url=f"/api/meme-store/asset/sheet/{entry['id']}",
            cover_url=f"/api/meme-store/asset/cover/{entry['id']}",
            items=_parse_tags_preview_list(tags_raw, entry["id"]),
        ),
        None,
    )


def _load_tags_map(pack_dir: Path) -> dict[str, BundleMemeTags]:
    tags_path = pack_dir / "tags.json"
    if not tags_path.is_file():
        return {}
    try:
        raw = json.loads(tags_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, list):
        return {}
    mapping: dict[str, BundleMemeTags] = {}
    for entry in raw:
        obj = _as_dict(entry)
        if obj is None:
            continue
        file_name = _str_val(obj, "file")
        if not file_name:
            continue
        mapping[file_name] = BundleMemeTags(
            title=_str_val(obj, "title")[:64],
            description=_str_val(obj, "description")[:512],
            ocr_text=_str_val(obj, "ocr_text")[:256],
            emotion_tags=_str_list_val(obj, "emotion_tags"),
            scene_tags=_str_list_val(obj, "scene_tags"),
            custom_tags=_str_list_val(obj, "custom_tags"),
            usage_hint=_str_val(obj, "usage_hint")[:512],
            nsfw_score=_float_val(obj, "nsfw_score"),
        )
    return mapping


def _list_pack_images(pack_dir: Path) -> list[Path]:
    files = [
        p
        for p in pack_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_FORMATS and p.name != "tags.json"
    ]
    files.sort(key=lambda p: p.name)
    return files


def _find_pack_root(clone_dir: Path) -> Path | None:
    """定位克隆目录中的表情包根：优先当前目录，兼容一级子目录。"""
    if _list_pack_images(clone_dir):
        return clone_dir
    for sub in sorted(clone_dir.iterdir(), key=lambda p: p.name):
        if sub.is_dir() and not sub.name.startswith(".") and _list_pack_images(sub):
            return sub
    return None


def _read_pack_manifest_name(pack_root: Path) -> tuple[str, str]:
    manifest_path = pack_root / "manifest.json"
    if not manifest_path.is_file():
        return "", ""
    try:
        obj = _as_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return "", ""
    if obj is None:
        return "", ""
    return _str_val(obj, "name"), _str_val(obj, "series")


async def _allocate_pack_name(preferred: str, store_id: str) -> str:
    """选择安全且不撞名的本地包名；撞名（如人格仓库同名包）时追加 slug 后缀。"""
    from gsuid_core.ai_core.persona.store import is_repo_pack

    base = preferred.strip()[:64]
    if not base or not is_safe_filename(base):
        base = store_id[:64]

    candidates = [base, f"{base[:48]}-{store_id[:12]}"[:64]]
    for idx in range(2, 10):
        candidates.append(f"{base[:56]}-{idx}"[:64])

    for cand in candidates:
        if not is_safe_filename(cand):
            continue
        if is_repo_pack(cand):
            continue
        if await PackStore.get_by_name(cand) is None:
            return cand
    return f"meme-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class _PreparedImage:
    """单张包内图片的解码结果；在任何写入前产出，损坏即整体拒绝。"""

    file_name: str
    data: bytes
    mime: str
    width: int
    height: int
    meme_id: str
    raw_bytes: bytes
    raw_mime: str


async def _prepare_pack_images(pack_root: Path) -> list[_PreparedImage]:
    """读取并解码全部包内图片，不做任何写入。任一损坏即抛 MemePackImportError。"""
    images = await asyncio.to_thread(_list_pack_images, pack_root)
    prepared_items: list[_PreparedImage] = []
    for image_path in images:
        raw_bytes = await asyncio.to_thread(image_path.read_bytes)
        suffix = image_path.suffix.lower()
        raw_mime = _IMAGE_MIME[suffix] if suffix in _IMAGE_MIME else "image/jpeg"
        try:
            prepared = await asyncio.to_thread(prepare_meme_image, raw_bytes, raw_mime, short_side=STORE_SHORT_SIDE)
        except (OSError, ValueError) as e:
            raise MemePackImportError(f"图片「{image_path.name}」无法解码或已损坏") from e
        prepared_items.append(
            _PreparedImage(
                file_name=image_path.name,
                data=prepared.data,
                mime=prepared.mime,
                width=prepared.width,
                height=prepared.height,
                meme_id=compute_meme_id(prepared.data),
                raw_bytes=raw_bytes,
                raw_mime=raw_mime,
            )
        )
    return prepared_items


async def _apply_pack_import(
    prepared_items: list[_PreparedImage],
    tags_map: dict[str, BundleMemeTags],
    pack_id: str,
) -> tuple[list[str], int, int, int, int, int]:
    """把预检后的图片写入本地表情库并挂到指定 pack_id。

    Returns: (all_meme_ids, imported_new, linked_count, reused_count, untagged_count, rejected_count)
    """
    from gsuid_core.ai_core.meme.tagger import enqueue_tag

    nsfw_threshold = float(meme_config.get_config("meme_nsfw_threshold").data)

    meme_ids: list[str] = []
    imported_new = 0
    linked_count = 0
    reused_count = 0
    untagged_count = 0
    rejected_count = 0
    sync_records: list[AiMemeRecord] = []

    for item in prepared_items:
        data = item.data
        mime = item.mime
        meme_id = item.meme_id
        tags_meta = tags_map[item.file_name] if item.file_name in tags_map else BundleMemeTags()
        has_semantic = bool(tags_meta.title or tags_meta.description or tags_meta.usage_hint or tags_meta.emotion_tags)
        # 预打标内容绕过本地 VLM 管线，上游 nsfw_score 是唯一防线，超过阈值按 rejected 入库
        nsfw_hit = has_semantic and tags_meta.nsfw_score >= nsfw_threshold

        existing = await AiMemeRecord.get_by_meme_id(meme_id)
        if existing is None:
            legacy_prepared = await asyncio.to_thread(
                prepare_meme_image,
                item.raw_bytes,
                item.raw_mime,
                short_side=LEGACY_SHORT_SIDE,
                upscale=True,
            )
            legacy_id = compute_meme_id(legacy_prepared.data)
            if legacy_id != meme_id:
                legacy_record = await AiMemeRecord.get_by_meme_id(legacy_id)
                if legacy_record is not None:
                    meme_id = legacy_id
                    existing = legacy_record

        if existing is None:
            target_folder = "rejected" if nsfw_hit else "common"
            folder = get_folder_path(target_folder)
            folder.mkdir(parents=True, exist_ok=True)
            dest_name = f"{meme_id}{ext_for_mime(mime)}"
            await asyncio.to_thread((folder / dest_name).write_bytes, data)
            if nsfw_hit:
                initial_status = "rejected"
            elif has_semantic:
                initial_status = "manual"
            else:
                initial_status = "pending"
            record = AiMemeRecord(
                meme_id=meme_id,
                file_path=f"{target_folder}/{dest_name}",
                file_size=len(data),
                file_mime=mime,
                width=item.width,
                height=item.height,
                folder=target_folder,
                persona_hint="common",
                title=tags_meta.title,
                description=tags_meta.description,
                ocr_text=tags_meta.ocr_text,
                emotion_tags=list(tags_meta.emotion_tags),
                scene_tags=list(tags_meta.scene_tags),
                custom_tags=list(tags_meta.custom_tags),
                usage_hint=tags_meta.usage_hint,
                enabled=True,
                status=initial_status,
                nsfw_score=tags_meta.nsfw_score,
                pending_pack_id="" if has_semantic else pack_id,
                assign_default_pack=False,
                source_group="meme_store",
            )
            await AiMemeRecord.insert_record(record)
            imported_new += 1
            final_status = initial_status
            if nsfw_hit:
                rejected_count += 1
            elif has_semantic:
                sync_records.append(record)
            else:
                untagged_count += 1
                await enqueue_tag(meme_id)
        else:
            reused_count += 1
            final_status = existing.status
            existing_empty = not (existing.title or existing.description or existing.usage_hint)
            # 已有记录是用户资产：nsfw 高分时不覆盖元数据，交给本地打标管线裁决
            if existing_empty and has_semantic and not nsfw_hit:
                await AiMemeRecord.update_record(
                    meme_id,
                    {
                        "title": tags_meta.title,
                        "description": tags_meta.description,
                        "ocr_text": tags_meta.ocr_text,
                        "emotion_tags": list(tags_meta.emotion_tags),
                        "scene_tags": list(tags_meta.scene_tags),
                        "custom_tags": list(tags_meta.custom_tags),
                        "usage_hint": tags_meta.usage_hint,
                        "nsfw_score": tags_meta.nsfw_score,
                        "status": "manual",
                    },
                )
                final_status = "manual"
                refreshed = await AiMemeRecord.get_by_meme_id(meme_id)
                if refreshed is not None:
                    sync_records.append(refreshed)

        if (has_semantic or existing is not None) and final_status != "rejected":
            if await PackStore.add_member(pack_id, meme_id):
                linked_count += 1
        meme_ids.append(meme_id)

    for record in sync_records:
        try:
            await MemeLibrary.sync_to_qdrant(record)
        except Exception as e:
            logger.warning(t("log.meme.store_vector_sync_fail", meme_id=record.meme_id, e=e))

    if meme_ids:
        await write_member_payloads(meme_ids, strict=False)

    return meme_ids, imported_new, linked_count, reused_count, untagged_count, rejected_count


async def cleanup_market_orphans(candidate_ids: Sequence[str]) -> tuple[int, list[str]]:
    """清理候选中不再属于任何包的纯市场来源表情（文件 + 记录 + 向量 + 出站缓存）。

    Returns: (删除数, 幸存且需要刷新 payload 的 meme_id 列表)
    """
    unique_ids = sorted({mid for mid in candidate_ids if mid})
    if not unique_ids:
        return 0, []
    pack_map = await PackStore.pack_ids_by_memes(unique_ids)
    deleted = 0
    survivors: list[str] = []
    for meme_id in unique_ids:
        other_packs = pack_map[meme_id] if meme_id in pack_map else []
        if other_packs:
            survivors.append(meme_id)
            continue
        record = await AiMemeRecord.get_by_meme_id(meme_id)
        if record is None:
            continue
        if record.source_group != "meme_store":
            survivors.append(meme_id)
            continue
        # 待打标记录若仍挂靠其他存活包（同图多包复用），打标完成才会入包，不能提前清
        if record.pending_pack_id and await PackStore.get_pack(record.pending_pack_id) is not None:
            survivors.append(meme_id)
            continue
        if await MemeLibrary.delete_meme(meme_id):
            deleted += 1
    return deleted, survivors


async def _refresh_survivor_payloads(survivors: Sequence[str]) -> None:
    if survivors:
        await write_member_payloads(list(survivors), strict=False)


def _git_clone_url(repo_url: str) -> str:
    clean = repo_url.strip().rstrip("/")
    if clean.endswith(".git") or clean.startswith("git@") or clean.startswith("ssh://"):
        return clean
    return f"{clean}.git"


async def set_pack_personas(
    pack_id: str,
    persona_names: Sequence[str],
) -> tuple[PackPersonasBindResult | None, str | None]:
    """为指定表情包批量设置要挂载的人格列表（保留其他人格已有包，跳过 git 托管人格）。"""
    from gsuid_core.ai_core.persona.store import is_git_managed

    pack = await PackStore.get_pack(pack_id)
    if pack is None:
        return None, "表情包不存在"

    wanted: list[str] = []
    seen: set[str] = set()
    for raw_name in persona_names:
        name = raw_name.strip()
        if not name or name in seen:
            continue
        if not is_safe_filename(name) or not Persona(name).exists():
            return None, f"人格「{name}」不存在"
        if is_git_managed(name):
            return None, f"人格「{name}」由人格市场托管，不允许手动修改表情包挂载"
        seen.add(name)
        wanted.append(name)

    summaries = await PackStore.list_summaries()
    current_bound: list[str] = []
    for row in summaries:
        if row.pack_id == pack_id:
            current_bound = list(row.bound_personas)
            break

    # 检查新增挂载是否超出每人格上限
    to_add = [p for p in wanted if p not in current_bound]
    for p_name in to_add:
        existing_packs = await PackStore.pack_ids_for_persona(p_name)
        if pack_id not in existing_packs and len(existing_packs) >= MAX_PACKS_PER_PERSONA:
            return None, f"人格「{p_name}」已达最多 {MAX_PACKS_PER_PERSONA} 个表情包挂载上限"

    # 移除取消勾选的可编辑人格上的该包
    to_remove = [p for p in current_bound if p not in seen and not is_git_managed(p)]
    for p_name in to_remove:
        existing_packs = await PackStore.pack_ids_for_persona(p_name)
        next_packs = [pid for pid in existing_packs if pid != pack_id]
        if len(next_packs) != len(existing_packs):
            await PackStore.replace_binds(p_name, next_packs)

    for p_name in to_add:
        await PackStore.add_bind_if_missing(p_name, pack_id)

    final_bound: list[str] = []
    for row in await PackStore.list_summaries():
        if row.pack_id == pack_id:
            final_bound = list(row.bound_personas)
            break

    return (
        PackPersonasBindResult(
            pack_id=pack_id,
            pack_name=pack.name,
            bound_personas=final_bound,
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class _PackImportOutcome:
    pack_id: str
    pack_name: str
    meme_ids: list[str]
    imported: int
    linked: int
    reused: int
    untagged: int
    rejected: int


async def _persist_new_market_pack(
    pack_root: Path,
    *,
    store_id: str,
    local_name: str,
    repo_url: str,
    branch: str,
    commit: str,
    series: str,
) -> tuple[_PackImportOutcome | None, str | None]:
    """预检、建包、导入、写登记的一体化安装；任一步失败回滚本地包与本次纯市场入库。"""
    try:
        prepared_items = await _prepare_pack_images(pack_root)
    except MemePackImportError as e:
        return None, str(e)
    tags_map = await asyncio.to_thread(_load_tags_map, pack_root)

    created = await PackStore.create_pack(local_name)
    committed = False
    meme_ids: list[str] = []
    imported = linked = reused = untagged = rejected = 0
    try:
        meme_ids, imported, linked, reused, untagged, rejected = await _apply_pack_import(
            prepared_items,
            tags_map,
            created.pack_id,
        )
        await _save_installed_entry(
            InstalledMemePack(
                store_id=store_id,
                pack_id=created.pack_id,
                pack_name=created.name,
                repo_url=repo_url,
                branch=branch,
                commit=commit,
                series=series,
                meme_ids=list(dict.fromkeys(meme_ids)),
                installed_at=time.time(),
            )
        )
        committed = True
    finally:
        if not committed:
            delete_res = await PackStore.delete_pack(created.pack_id)
            rollback_ids = sorted(set(delete_res.meme_ids) | set(delete_res.pending_meme_ids))
            await cleanup_market_orphans(rollback_ids)

    return (
        _PackImportOutcome(
            pack_id=created.pack_id,
            pack_name=created.name,
            meme_ids=meme_ids,
            imported=imported,
            linked=linked,
            reused=reused,
            untagged=untagged,
            rejected=rejected,
        ),
        None,
    )


async def install_pack_from_store(
    store_id: str,
    *,
    persona_names: Sequence[str] = (),
) -> tuple[MemeStoreInstallResult | None, str | None]:
    """从 CNB 市场白名单按单分支浅克隆安装一个表情包到本地。"""
    from gsuid_core.utils.plugins_update.git_async import git_clone, git_get_current_commit

    entry = await _find_catalog_entry(store_id)
    if entry is None:
        return None, "表情包市场内不存在该条目"

    installed = await reconcile_installed()
    if store_id in installed:
        return None, f"表情包「{installed[store_id]['pack_name']}」已安装"

    MEME_STORE_TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = MEME_STORE_TMP_DIR / f"{store_id}_{uuid.uuid4().hex[:8]}"
    try:
        clone_url = _git_clone_url(entry["repo"])
        ok, message = await git_clone(
            clone_url,
            tmp_dir,
            branch=entry["branch"],
            depth=1,
            single_branch=True,
        )
        if not ok:
            return None, f"克隆失败: {message}"

        pack_root = await asyncio.to_thread(_find_pack_root, tmp_dir)
        if pack_root is None:
            return None, "仓库分支中未找到可导入的图片文件"

        commit = await git_get_current_commit(tmp_dir)
        local_name = await _allocate_pack_name(entry["name"], store_id)
        outcome, err = await _persist_new_market_pack(
            pack_root,
            store_id=store_id,
            local_name=local_name,
            repo_url=entry["repo"],
            branch=entry["branch"],
            commit=commit or entry["commit"],
            series=entry["series"],
        )
        if err is not None or outcome is None:
            return None, err or "导入失败"

        bound_personas: list[str] = []
        if persona_names:
            bind_res, bind_err = await set_pack_personas(outcome.pack_id, persona_names)
            if bind_res is not None:
                bound_personas = bind_res["bound_personas"]
            elif bind_err is not None:
                logger.warning(t("log.meme.store_bind_after_install_fail", store_id=store_id, e=bind_err))

        logger.info(
            t(
                "log.meme.store_installed",
                store_id=store_id,
                pack_name=outcome.pack_name,
                imported=outcome.imported,
                linked=outcome.linked,
            )
        )
        return (
            MemeStoreInstallResult(
                store_id=store_id,
                pack_id=outcome.pack_id,
                pack_name=outcome.pack_name,
                imported=outcome.imported,
                linked=outcome.linked,
                reused=outcome.reused,
                untagged=outcome.untagged,
                rejected=outcome.rejected,
                bound_personas=bound_personas,
            ),
            None,
        )
    finally:
        await asyncio.to_thread(_remove_dir_force, tmp_dir)


async def install_pack_from_url(
    url: str,
    *,
    branch: str | None = None,
    pack_name: str | None = None,
    persona_names: Sequence[str] = (),
) -> tuple[MemeStoreInstallResult | None, str | None]:
    """通过任意合法 Git 仓库 URL 导入一个表情包到本地。"""
    from gsuid_core.plugin_registry.source import parse_repo_identity
    from gsuid_core.utils.plugins_update.git_async import git_clone, git_get_current_branch, git_get_current_commit
    from gsuid_core.utils.plugins_update.git_mirror import _extract_repo_name

    clean_url = url.strip()
    err = validate_install_source_url(clean_url)
    if err is not None:
        return None, err

    repo_name = _extract_repo_name(clean_url)
    if repo_name is None or not is_safe_filename(repo_name):
        return None, "无法从 URL 解析出合法仓库名"

    clean_branch = branch.strip() if isinstance(branch, str) and branch.strip() else None

    MEME_STORE_TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = MEME_STORE_TMP_DIR / f"url_{uuid.uuid4().hex[:8]}"
    try:
        ok, message = await git_clone(
            clean_url,
            tmp_dir,
            branch=clean_branch,
            depth=1,
            single_branch=bool(clean_branch),
        )
        if not ok:
            return None, f"克隆失败: {message}"

        pack_root = await asyncio.to_thread(_find_pack_root, tmp_dir)
        if pack_root is None:
            return None, "仓库中未找到可导入的图片文件"

        # 未显式指定分支时克隆的是远端默认分支；登记必须记录真实分支，否则后续更新会去拉 main
        resolved_branch = clean_branch or await git_get_current_branch(tmp_dir)

        # store_id = 可读前缀 + 规范化 URL#分支 的哈希：不同 host/owner 的同名仓库不再互相碰撞
        ident = parse_repo_identity(clean_url)
        canonical = ident.canonical_url if ident is not None else clean_url.rstrip("/")
        digest = hashlib.sha256(f"{canonical.lower()}#{resolved_branch.lower()}".encode("utf-8")).hexdigest()[:8]
        branch_slug = resolved_branch.replace("/", "-")[:20] or "default"
        store_id = f"custom-{repo_name[:24]}-{branch_slug}-{digest}"

        installed = await reconcile_installed()
        if store_id in installed:
            return None, f"该仓库分支已安装为「{installed[store_id]['pack_name']}」"

        manifest_name, manifest_series = await asyncio.to_thread(_read_pack_manifest_name, pack_root)
        preferred_name = (
            pack_name.strip() if isinstance(pack_name, str) and pack_name.strip() else manifest_name or repo_name
        )
        commit = await git_get_current_commit(tmp_dir)
        local_name = await _allocate_pack_name(preferred_name, store_id)
        outcome, err = await _persist_new_market_pack(
            pack_root,
            store_id=store_id,
            local_name=local_name,
            repo_url=clean_url,
            branch=resolved_branch,
            commit=commit,
            series=manifest_series,
        )
        if err is not None or outcome is None:
            return None, err or "导入失败"

        bound_personas: list[str] = []
        if persona_names:
            bind_res, _ = await set_pack_personas(outcome.pack_id, persona_names)
            if bind_res is not None:
                bound_personas = bind_res["bound_personas"]

        logger.info(
            t(
                "log.meme.store_installed",
                store_id=store_id,
                pack_name=outcome.pack_name,
                imported=outcome.imported,
                linked=outcome.linked,
            )
        )
        return (
            MemeStoreInstallResult(
                store_id=store_id,
                pack_id=outcome.pack_id,
                pack_name=outcome.pack_name,
                imported=outcome.imported,
                linked=outcome.linked,
                reused=outcome.reused,
                untagged=outcome.untagged,
                rejected=outcome.rejected,
                bound_personas=bound_personas,
            ),
            None,
        )
    finally:
        await asyncio.to_thread(_remove_dir_force, tmp_dir)


async def update_store_pack(store_id: str) -> tuple[MemeStoreUpdateResult | None, str | None]:
    """同步远端表情包分支最新变更：增量导入新图、移除上游已删图，保留所有人格挂载。"""
    from gsuid_core.utils.plugins_update.git_async import git_clone, git_get_current_commit

    installed = await reconcile_installed()
    if store_id not in installed:
        return None, "该表情包未通过市场安装"
    item = installed[store_id]

    MEME_STORE_TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = MEME_STORE_TMP_DIR / f"upd_{uuid.uuid4().hex[:8]}"
    try:
        clone_url = _git_clone_url(item["repo_url"])
        ok, message = await git_clone(
            clone_url,
            tmp_dir,
            branch=item["branch"],
            depth=1,
            single_branch=True,
        )
        if not ok:
            return None, f"拉取更新失败: {message}"

        pack_root = await asyncio.to_thread(_find_pack_root, tmp_dir)
        if pack_root is None:
            return None, "远端分支中未找到可导入的图片文件"

        commit = await git_get_current_commit(tmp_dir)
        try:
            prepared_items = await _prepare_pack_images(pack_root)
        except MemePackImportError as e:
            return None, str(e)
        tags_map = await asyncio.to_thread(_load_tags_map, pack_root)
        new_ids, _imported, linked, _reused, _untagged, _rejected = await _apply_pack_import(
            prepared_items,
            tags_map,
            item["pack_id"],
        )
        expected_set = set(new_ids)
        old_managed = set(item["meme_ids"])
        stale_remove = sorted(old_managed - expected_set)

        removed_count = 0
        if stale_remove:
            # 先摘除成员（含 pending 指向），再删纯市场孤儿：无其他包引用的直接清文件/记录/向量
            await PackStore.remove_members(item["pack_id"], stale_remove)
            removed_count = len(stale_remove)
            _, survivors = await cleanup_market_orphans(stale_remove)
            await _refresh_survivor_payloads(survivors)

        updated_entry = InstalledMemePack(
            store_id=item["store_id"],
            pack_id=item["pack_id"],
            pack_name=item["pack_name"],
            repo_url=item["repo_url"],
            branch=item["branch"],
            commit=commit or item["commit"],
            series=item["series"],
            meme_ids=list(dict.fromkeys(new_ids)),
            installed_at=item["installed_at"],
        )
        await _save_installed_entry(updated_entry)
        logger.info(
            t(
                "log.meme.store_updated",
                store_id=store_id,
                pack_name=item["pack_name"],
                added=linked,
                removed=removed_count,
            )
        )
        return (
            MemeStoreUpdateResult(
                store_id=store_id,
                pack_id=item["pack_id"],
                pack_name=item["pack_name"],
                commit=updated_entry["commit"],
                added=linked,
                removed=removed_count,
                total=len(new_ids),
            ),
            None,
        )
    finally:
        await asyncio.to_thread(_remove_dir_force, tmp_dir)


async def uninstall_store_pack(store_id: str) -> tuple[MemeStoreUninstallResult | None, str | None]:
    """卸载市场安装的表情包：删包解绑，并清理不再属于任何包的纯市场来源图片。"""
    installed = await reconcile_installed()
    if store_id not in installed:
        return None, "该表情包未通过市场安装"
    item = installed[store_id]

    delete_res = await PackStore.delete_pack(item["pack_id"])
    # 清理范围 = 登记历史成员 ∪ 当前成员 ∪ 待打标挂靠：更新期已移除或未能入包的图也不再漏
    candidates = sorted(set(item["meme_ids"]) | set(delete_res.meme_ids) | set(delete_res.pending_meme_ids))
    deleted_memes, survivors = await cleanup_market_orphans(candidates)
    await _refresh_survivor_payloads(survivors)

    await _delete_installed_entry(store_id)
    logger.info(
        t(
            "log.meme.store_uninstalled",
            store_id=store_id,
            pack_name=item["pack_name"],
            deleted=deleted_memes,
        )
    )
    return (
        MemeStoreUninstallResult(
            store_id=store_id,
            pack_id=item["pack_id"],
            pack_name=item["pack_name"],
            deleted_memes=deleted_memes,
            lost_personas=delete_res.lost_personas,
        ),
        None,
    )
