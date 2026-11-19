"""
GitHub 账号连接（OAuth Device Flow）与插件 star 查询 / 操作 API。

- 一个 Core 绑定一个 GitHub 账号（部署者自己的账号）；device_code 只留在进程内存
- access_token 用 Fernet 加密存 ``data/webconsole_github.json``（密钥 = b64(sha256(REGISTER_CODE))）
- /api/github/stars 批量返回仓库 star 数与已连接账号是否已 star：
  已连接走 GraphQL（50 个/批），未连接走匿名 REST（并发 5，限流或连续失败后余项返回 null）
"""

import re
import json
import time
import base64
import asyncio
import hashlib
import threading
from typing import Iterable, TypedDict, TypeGuard, NotRequired
from datetime import datetime
from dataclasses import field, dataclass

import httpx2
from fastapi import Depends
from pydantic import BaseModel
from boltons.fileutils import atomic_save
from cryptography.fernet import Fernet, InvalidToken

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.data_store import GITHUB_AUTH_PATH, GITHUB_STARS_CACHE_PATH
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import SessionRecord, require_auth, require_admin

from ._api_tags import PLUGINS

# OAuth App 的 Client ID（公开值，不是密钥）
GITHUB_CLIENT_ID = "Ov23lihQdEbX5md7O1Y5"
# 需要能 star 任意公共仓库的 scope；授权响应未含该 scope 时 can_star=false
STAR_SCOPE = "public_repo"
# 授权 scope 集合与之一有交集即可 star（repo 是 public_repo 的超集）
_STAR_CAPABLE_SCOPES = frozenset({"public_repo", "repo"})

_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GRAPHQL_URL = "https://api.github.com/graphql"
_API_BASE = "https://api.github.com"
_DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
# 域名必须完整匹配，否则 notgithub.com 这类拼合域名会被误判为 GitHub 仓库
_GITHUB_REPO_URL_RE = re.compile(r"(?<![A-Za-z0-9-])github\.com[/:]([^/]+)/([^/?#]+)", re.IGNORECASE)
MAX_REPOS_PER_REQUEST = 100
_GRAPHQL_BATCH_SIZE = 50
_ANON_CONCURRENCY = 5
# GitHub 不可达时连续失败即短路剩余匿名请求，避免上百个仓库逐个等超时
_ANON_MAX_FAILURES = 5
STAR_CACHE_TTL_SECONDS = 12 * 60 * 60
STAR_CACHE_MAX_ENTRIES = 1024
_HTTP_TIMEOUT = 15

_BASE_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "gsuid-core",
}


class GitHubAuthRecord(TypedDict):
    """data/webconsole_github.json 落盘结构；token_enc 为 Fernet 密文。"""

    token_enc: str
    login: str
    avatar_url: str
    connected_at: str
    can_star: bool


class _Viewer(TypedDict):
    """已解密、可直接发起 GitHub 请求的连接视图。"""

    token: str
    login: str
    avatar_url: str
    can_star: bool


class StarInfo(TypedDict):
    count: int | None
    starred: bool | None


class _StarCacheEntry(StarInfo):
    fetched_at: float


class _DiskStarEntry(TypedDict):
    count: int
    fetched_at: float


class _PendingDevice(TypedDict):
    device_code: str
    interval: float
    expires_at: float


class _StarsBody(BaseModel):
    repos: list[str]


class GitHubStatusData(TypedDict):
    connected: bool
    login: str
    avatar_url: str
    can_star: bool


class DeviceStartData(TypedDict):
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DevicePollData(TypedDict, total=False):
    state: str
    interval: float
    login: str
    avatar_url: str
    can_star: bool


class StarsData(TypedDict):
    connected: bool
    stars: dict[str, StarInfo]


class StarToggleData(TypedDict):
    starred: bool


class _Envelope(TypedDict):
    status: int
    msg: str
    data: NotRequired[GitHubStatusData | DeviceStartData | DevicePollData | StarsData | StarToggleData]


@dataclass
class _GraphqlResult:
    stars: dict[str, StarInfo] = field(default_factory=dict)
    error: str = ""
    revoked: bool = False


_file_lock = threading.Lock()
_stars_file_lock = threading.Lock()
_pending_device: _PendingDevice | None = None
_star_cache: dict[str, _StarCacheEntry] = {}
_star_cache_loaded = False
_star_cache_gen = 0
_persisted_star_cache_gen = 0
_viewer: _Viewer | None = None
_viewer_loaded = False


def _is_plain_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_plain_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read_str(mapping: object, key: str) -> str:
    if not isinstance(mapping, dict) or key not in mapping:
        return ""
    value = mapping[key]
    return value if isinstance(value, str) else ""


def _get_fernet() -> Fernet | None:
    """Fernet 实例：密钥 = base64(sha256(REGISTER_CODE))；未配置时返回 None。"""
    code = core_config.get_config("REGISTER_CODE")
    if not isinstance(code, str) or not code:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(code.encode()).digest())
    return Fernet(key)


def _load_viewer_from_disk_sync() -> _Viewer | None:
    with _file_lock:
        if not GITHUB_AUTH_PATH.exists():
            return None
        # 连接文件属外部输入（可人工编辑/损坏）：读不出来一律视为未连接
        try:
            raw = json.loads(GITHUB_AUTH_PATH.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning(t("log.webconsole.github_file_corrupted_ignore", e=e))
            return None
    if not isinstance(raw, dict):
        return None
    if any(k not in raw or not isinstance(raw[k], str) for k in ("token_enc", "login", "avatar_url", "connected_at")):
        return None
    can_star = "can_star" in raw and isinstance(raw["can_star"], bool) and raw["can_star"]
    fernet = _get_fernet()
    if fernet is None:
        return None
    try:
        token = fernet.decrypt(raw["token_enc"].encode()).decode()
    except (InvalidToken, ValueError):
        # REGISTER_CODE 被改导致解不开，等同未连接
        return None
    return _Viewer(
        token=token,
        login=raw["login"],
        avatar_url=raw["avatar_url"],
        can_star=can_star,
    )


def _save_record_sync(record: GitHubAuthRecord) -> bool:
    with _file_lock:
        try:
            GITHUB_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
            with atomic_save(
                str(GITHUB_AUTH_PATH),
                text_mode=False,
                overwrite=True,
                overwrite_part=True,
                file_perms=0o600,
            ) as f:
                if f is None:
                    return False
                f.write(json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8"))
        except OSError as e:
            logger.warning(t("log.webconsole.github_write_fail", e=e))
            return False
        return True


def _unlink_auth_file_sync() -> None:
    with _file_lock:
        try:
            GITHUB_AUTH_PATH.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(t("log.webconsole.github_write_fail", e=e))


@to_thread
def _load_viewer_from_disk() -> _Viewer | None:
    return _load_viewer_from_disk_sync()


@to_thread
def _save_record(record: GitHubAuthRecord) -> bool:
    return _save_record_sync(record)


@to_thread
def _unlink_auth_file() -> None:
    _unlink_auth_file_sync()


async def _get_viewer() -> _Viewer | None:
    global _viewer, _viewer_loaded
    if not _viewer_loaded:
        loaded = await _load_viewer_from_disk()
        # 仅在异步读盘期间未被 connect/disconnect 写穿时落入内存态
        if not _viewer_loaded:
            _viewer = loaded
            _viewer_loaded = True
    return _viewer


def _clear_cached_starred() -> None:
    """剥除内存缓存中旧账号的 starred 标记（保留已获取的 count）。"""
    for entry in _star_cache.values():
        entry["starred"] = None


def _clear_pending_if(pending: _PendingDevice) -> None:
    """仅当未被并发的 device/start 替换时才清空待授权设备码。"""
    global _pending_device
    if _pending_device is pending:
        _pending_device = None


async def _clear_connection() -> None:
    """断开：清空内存态、剥除缓存里的旧账号 starred 状态并删本地文件。"""
    global _pending_device, _viewer, _viewer_loaded
    _pending_device = None
    _viewer = None
    _viewer_loaded = True
    _clear_cached_starred()
    await _unlink_auth_file()


def _headers(token: str | None = None) -> dict[str, str]:
    headers = dict(_BASE_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def is_valid_repo_name(full_name: str) -> bool:
    """校验 ``owner/repo`` 格式，并拦截 ``.`` / ``..`` 与首尾连字符。"""
    if not _REPO_RE.fullmatch(full_name):
        return False
    owner, repo = full_name.split("/", 1)
    return not owner.startswith("-") and not owner.endswith("-") and repo not in (".", "..")


def parse_github_repo(link: str) -> str | None:
    """从仓库 URL 提取规范 ``owner/repo``；非 GitHub 或非法名称返回 None。"""
    if not link:
        return None
    match = _GITHUB_REPO_URL_RE.search(link)
    if match is None:
        return None
    owner = match.group(1).strip()
    repo = match.group(2).strip()
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    full_name = f"{owner}/{repo}"
    return full_name if is_valid_repo_name(full_name) else None


def normalize_repo_names(repos: Iterable[str], limit: int | None = MAX_REPOS_PER_REQUEST) -> list[str]:
    """清洗 ``owner/repo``：去 .git 后缀、校验格式、大小写不敏感去重。"""
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in repos:
        name = raw.strip()
        if name.lower().endswith(".git"):
            name = name[:-4]
        if not is_valid_repo_name(name):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
        if limit is not None and len(cleaned) >= limit:
            break
    return cleaned


def build_star_graphql_query(repos: list[str]) -> str:
    """拼 ``r0: repository(...) { stargazerCount viewerHasStarred }`` 别名批量查询。"""
    parts = []
    for index, full_name in enumerate(repos):
        owner, name = full_name.split("/", 1)
        parts.append(
            f"r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
            "{ stargazerCount viewerHasStarred }"
        )
    return "query { " + " ".join(parts) + " }"


def parse_star_graphql_data(data: object, repos: list[str]) -> dict[str, StarInfo]:
    """解析 GraphQL data 字段；仓库不存在时对应别名为 null → count/starred 均 null。"""
    result: dict[str, StarInfo] = {}
    for index, repo in enumerate(repos):
        node = data[f"r{index}"] if isinstance(data, dict) and f"r{index}" in data else None
        count: int | None = None
        starred: bool | None = None
        if isinstance(node, dict):
            raw_count = node["stargazerCount"] if "stargazerCount" in node else None
            raw_starred = node["viewerHasStarred"] if "viewerHasStarred" in node else None
            count = raw_count if _is_plain_int(raw_count) else None
            starred = raw_starred if isinstance(raw_starred, bool) else None
        result[repo] = StarInfo(count=count, starred=starred)
    return result


def _cache_get(repo_key: str) -> StarInfo | None:
    if repo_key not in _star_cache:
        return None
    entry = _star_cache[repo_key]
    if time.time() - entry["fetched_at"] > STAR_CACHE_TTL_SECONDS:
        return None
    return StarInfo(count=entry["count"], starred=entry["starred"])


def _cache_get_stale_count(repo_key: str) -> int | None:
    if repo_key not in _star_cache:
        return None
    return _star_cache[repo_key]["count"]


def _cache_put(repo: str, info: StarInfo) -> None:
    global _star_cache_gen
    key = repo.lower()
    _star_cache.pop(key, None)
    _star_cache[key] = _StarCacheEntry(count=info["count"], starred=info["starred"], fetched_at=time.time())
    while len(_star_cache) > STAR_CACHE_MAX_ENTRIES:
        oldest = next(iter(_star_cache))
        del _star_cache[oldest]
    _star_cache_gen += 1


def _load_star_cache_sync() -> dict[str, _StarCacheEntry]:
    with _stars_file_lock:
        if not GITHUB_STARS_CACHE_PATH.exists():
            return {}
        try:
            raw = json.loads(GITHUB_STARS_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    loaded: dict[str, _StarCacheEntry] = {}
    for raw_key, raw_entry in raw.items():
        if not isinstance(raw_key, str) or not isinstance(raw_entry, dict):
            continue
        key = raw_key.lower()
        if not is_valid_repo_name(key) or "count" not in raw_entry or "fetched_at" not in raw_entry:
            continue
        count = raw_entry["count"]
        fetched_at = raw_entry["fetched_at"]
        if not _is_plain_int(count) or count < 0 or not _is_plain_number(fetched_at) or fetched_at < 0:
            continue
        loaded[key] = _StarCacheEntry(count=count, starred=None, fetched_at=float(fetched_at))
        if len(loaded) >= STAR_CACHE_MAX_ENTRIES:
            break
    return loaded


def _save_star_cache_sync(snapshot: dict[str, _DiskStarEntry], gen: int) -> None:
    global _persisted_star_cache_gen
    with _stars_file_lock:
        if gen <= _persisted_star_cache_gen:
            return
        try:
            GITHUB_STARS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with atomic_save(
                str(GITHUB_STARS_CACHE_PATH),
                text_mode=False,
                overwrite=True,
                overwrite_part=True,
            ) as f:
                if f is not None:
                    f.write(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
                    _persisted_star_cache_gen = gen
        except OSError as e:
            logger.warning(t("log.webconsole.github_write_fail", e=e))


@to_thread
def _load_star_cache() -> dict[str, _StarCacheEntry]:
    return _load_star_cache_sync()


@to_thread
def _save_star_cache(snapshot: dict[str, _DiskStarEntry], gen: int) -> None:
    _save_star_cache_sync(snapshot, gen)


async def _ensure_star_cache_loaded() -> None:
    global _star_cache_loaded
    if _star_cache_loaded:
        return
    loaded = await _load_star_cache()
    if not _star_cache_loaded:
        for key, entry in loaded.items():
            if key not in _star_cache and len(_star_cache) < STAR_CACHE_MAX_ENTRIES:
                _star_cache[key] = entry
        _star_cache_loaded = True


async def _persist_star_cache() -> None:
    gen = _star_cache_gen
    snapshot: dict[str, _DiskStarEntry] = {}
    for key, entry in _star_cache.items():
        if entry["count"] is not None:
            snapshot[key] = _DiskStarEntry(count=entry["count"], fetched_at=entry["fetched_at"])
    await _save_star_cache(snapshot, gen)


def _github_error_message(raw: object, status_code: int) -> str:
    """取 GitHub 错误描述：OAuth 用 error_description/error，REST 用 message。"""
    for key in ("error_description", "error", "message"):
        value = _read_str(raw, key)
        if value:
            return value
    return f"GitHub 请求失败（HTTP {status_code}）"


def _graphql_global_error(payload: dict[object, object]) -> str:
    """当 GraphQL data 整段缺失时提取顶层 errors 提示。"""
    if "errors" not in payload or not isinstance(payload["errors"], list):
        return ""
    for item in payload["errors"]:
        msg = _read_str(item, "message")
        if msg:
            return msg
    return ""


def _scope_can_star(scope: str) -> bool:
    """授权 scope 串（空格/逗号分隔）是否具备 star 能力。"""
    return bool({part.strip() for part in scope.replace(",", " ").split()} & _STAR_CAPABLE_SCOPES)


def _star_toggle_cache_update(cached: StarInfo | None, star: bool) -> StarInfo | None:
    """star 幂等缓存更新：starred 真变化且 count 已知才 ±1，否则只写状态。"""
    if cached is None:
        return None
    if isinstance(cached["starred"], bool) and cached["starred"] != star and cached["count"] is not None:
        return StarInfo(count=cached["count"] + (1 if star else -1), starred=star)
    return StarInfo(count=cached["count"], starred=star)


def _resp_json(resp: httpx2.Response) -> object:
    """GitHub 返回体属不可信外部输入：非 JSON（如 502 HTML 页）按 None 处理。"""
    try:
        return resp.json()
    except json.JSONDecodeError:
        return None


async def _fetch_graphql_stars(token: str, repos: list[str]) -> _GraphqlResult:
    """已连接路径：50 个一批走 GraphQL，顺带查 viewerHasStarred。"""
    result = _GraphqlResult()
    if not repos:
        return result
    try:
        async with httpx2.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            for start in range(0, len(repos), _GRAPHQL_BATCH_SIZE):
                batch = repos[start : start + _GRAPHQL_BATCH_SIZE]
                resp = await client.post(
                    _GRAPHQL_URL,
                    headers=_headers(token),
                    json={"query": build_star_graphql_query(batch)},
                )
                if resp.status_code == 401:
                    result.revoked = True
                    return result
                payload = _resp_json(resp)
                if resp.status_code != 200:
                    result.error = _github_error_message(payload, resp.status_code)
                    return result
                if not isinstance(payload, dict):
                    result.error = "GitHub 返回了无法解析的响应"
                    return result
                gql_data = payload["data"] if "data" in payload else None
                if not isinstance(gql_data, dict):
                    global_err = _graphql_global_error(payload)
                    if global_err:
                        result.error = global_err
                        return result
                result.stars.update(parse_star_graphql_data(gql_data, batch))
    except httpx2.HTTPError as e:
        result.error = f"GitHub 请求失败: {e}"
    return result


async def _fetch_anon_stars(repos: list[str]) -> dict[str, StarInfo]:
    """未连接路径：磁盘/内存缓存优先，缺项走匿名 REST；限流或连续网络失败时短路，回退过期缓存。"""
    await _ensure_star_cache_loaded()
    stars: dict[str, StarInfo] = {}
    missing: list[str] = []
    for repo in repos:
        cached = _cache_get(repo.lower())
        if cached is not None:
            # 未连接路径只复用 star 数；starred 属于旧连接态，断开后不能回显
            stars[repo] = StarInfo(count=cached["count"], starred=None)
        else:
            missing.append(repo)
    if not missing:
        return stars

    rate_limited = False
    consecutive_failures = 0
    dirty = False
    semaphore = asyncio.Semaphore(_ANON_CONCURRENCY)

    async def _one(client: httpx2.AsyncClient, repo: str) -> None:
        nonlocal rate_limited, dirty, consecutive_failures
        stale_count = _cache_get_stale_count(repo.lower())
        if rate_limited or consecutive_failures >= _ANON_MAX_FAILURES:
            stars[repo] = StarInfo(count=stale_count, starred=None)
            return
        async with semaphore:
            if rate_limited or consecutive_failures >= _ANON_MAX_FAILURES:
                stars[repo] = StarInfo(count=stale_count, starred=None)
                return
            try:
                resp = await client.get(f"{_API_BASE}/repos/{repo}", headers=_headers())
            except httpx2.HTTPError:
                consecutive_failures += 1
                stars[repo] = StarInfo(count=stale_count, starred=None)
                return
            consecutive_failures = 0
            if resp.status_code in (403, 429):
                rate_limited = True
                stars[repo] = StarInfo(count=stale_count, starred=None)
                return
            count: int | None = None
            if resp.status_code == 200:
                payload = _resp_json(resp)
                if isinstance(payload, dict):
                    raw = payload["stargazers_count"] if "stargazers_count" in payload else None
                    if _is_plain_int(raw):
                        count = raw
            if count is not None:
                info = StarInfo(count=count, starred=None)
                _cache_put(repo, info)
                dirty = True
                stars[repo] = info
            else:
                stars[repo] = StarInfo(count=stale_count, starred=None)

    async with httpx2.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        await asyncio.gather(*(_one(client, repo) for repo in missing))
    for repo in missing:
        if repo not in stars:
            stars[repo] = StarInfo(count=_cache_get_stale_count(repo.lower()), starred=None)
    if dirty:
        await _persist_star_cache()
    return stars


async def resolve_repo_star_counts(repos: Iterable[str]) -> dict[str, int | None]:
    """批量获取仓库 star 数（key 为小写 ``owner/repo``）：磁盘/内存缓存优先，缺项走已连接/匿名。"""
    await _ensure_star_cache_loaded()
    cleaned = normalize_repo_names(repos, limit=None)
    if not cleaned:
        return {}
    counts: dict[str, int | None] = {repo.lower(): _cache_get_stale_count(repo.lower()) for repo in cleaned}
    missing: list[str] = []
    for repo in cleaned:
        cached = _cache_get(repo.lower())
        if cached is not None and cached["count"] is not None:
            counts[repo.lower()] = cached["count"]
        else:
            missing.append(repo)
    if not missing:
        return counts

    viewer = await _get_viewer()
    if viewer is not None:
        result = await _fetch_graphql_stars(viewer["token"], missing)
        if result.revoked:
            logger.warning(t("log.webconsole.github_token_revoked"))
            await _clear_connection()
            viewer = None
        elif not result.error:
            dirty = False
            for repo, info in result.stars.items():
                if info["count"] is not None:
                    _cache_put(repo, info)
                    dirty = True
                    counts[repo.lower()] = info["count"]
            if dirty:
                await _persist_star_cache()
            return counts
        else:
            return counts

    anon_stars = await _fetch_anon_stars(missing)
    for repo, info in anon_stars.items():
        if info["count"] is not None:
            counts[repo.lower()] = info["count"]
    return counts


@app.get("/api/github/status", summary="获取 GitHub 连接状态", tags=PLUGINS)
async def github_status(_user: SessionRecord = Depends(require_auth)) -> _Envelope:
    viewer = await _get_viewer()
    data = GitHubStatusData(
        connected=viewer is not None,
        login=viewer["login"] if viewer else "",
        avatar_url=viewer["avatar_url"] if viewer else "",
        can_star=viewer["can_star"] if viewer else False,
    )
    return {"status": 0, "msg": "ok", "data": data}


@app.post("/api/github/device/start", summary="发起 GitHub Device Flow 授权", tags=PLUGINS)
async def github_device_start(_user: SessionRecord = Depends(require_admin)) -> _Envelope:
    global _pending_device
    try:
        async with httpx2.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                _DEVICE_CODE_URL,
                params={"client_id": GITHUB_CLIENT_ID, "scope": STAR_SCOPE},
                headers={"Accept": "application/json"},
            )
    except httpx2.HTTPError as e:
        return {"status": 1, "msg": f"GitHub 设备码申请失败: {e}"}
    data = _resp_json(resp)
    if not isinstance(data, dict) or "error" in data or resp.status_code != 200:
        return {"status": 1, "msg": _github_error_message(data, resp.status_code)}

    device_code = _read_str(data, "device_code")
    user_code = _read_str(data, "user_code")
    verification_uri = _read_str(data, "verification_uri")
    raw_expires = data["expires_in"] if "expires_in" in data else None
    raw_interval = data["interval"] if "interval" in data else None
    if (
        not device_code
        or not user_code
        or not verification_uri
        or not _is_plain_int(raw_expires)
        or raw_expires <= 0
        or not _is_plain_int(raw_interval)
        or raw_interval <= 0
    ):
        return {"status": 1, "msg": "GitHub 返回了无法解析的设备码响应"}

    _pending_device = _PendingDevice(
        device_code=device_code,
        interval=float(raw_interval),
        expires_at=time.time() + raw_expires,
    )
    return {
        "status": 0,
        "msg": "ok",
        "data": DeviceStartData(
            user_code=user_code,
            verification_uri=verification_uri,
            expires_in=raw_expires,
            interval=raw_interval,
        ),
    }


@app.post("/api/github/device/poll", summary="轮询 GitHub Device Flow 授权结果", tags=PLUGINS)
async def github_device_poll(_user: SessionRecord = Depends(require_admin)) -> _Envelope:
    global _viewer, _viewer_loaded
    pending = _pending_device
    if pending is None or time.time() >= pending["expires_at"]:
        if pending is not None:
            _clear_pending_if(pending)
        return {"status": 0, "msg": "ok", "data": DevicePollData(state="expired")}

    try:
        async with httpx2.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                _ACCESS_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": pending["device_code"],
                    "grant_type": _DEVICE_GRANT_TYPE,
                },
            )
            data = _resp_json(resp)
            if not isinstance(data, dict):
                return {"status": 1, "msg": _github_error_message(data, resp.status_code)}

            error = _read_str(data, "error")
            if error == "authorization_pending":
                return {"status": 0, "msg": "ok", "data": DevicePollData(state="pending", interval=pending["interval"])}
            if error == "slow_down":
                raw_interval = data["interval"] if "interval" in data else None
                pending["interval"] = (
                    float(raw_interval)
                    if _is_plain_int(raw_interval) and raw_interval > pending["interval"]
                    else pending["interval"] + 5
                )
                return {
                    "status": 0,
                    "msg": "ok",
                    "data": DevicePollData(state="slow_down", interval=pending["interval"]),
                }
            if error in ("expired_token", "bad_verification_code"):
                _clear_pending_if(pending)
                return {"status": 0, "msg": "ok", "data": DevicePollData(state="expired")}
            if error == "access_denied":
                _clear_pending_if(pending)
                return {"status": 0, "msg": "ok", "data": DevicePollData(state="denied")}
            if error:
                # 终止性错误：清掉待授权态，避免前端继续轮询一个已死的 device_code
                _clear_pending_if(pending)
                description = _read_str(data, "error_description") or error
                return {"status": 1, "msg": f"GitHub 授权失败: {description}"}

            if resp.status_code != 200:
                _clear_pending_if(pending)
                return {"status": 1, "msg": _github_error_message(data, resp.status_code)}
            token = _read_str(data, "access_token")
            if not token:
                _clear_pending_if(pending)
                return {"status": 1, "msg": "GitHub 返回了无法解析的授权响应"}
            can_star = _scope_can_star(_read_str(data, "scope"))

            user_resp = await client.get(f"{_API_BASE}/user", headers=_headers(token))
    except httpx2.HTTPError as e:
        return {"status": 1, "msg": f"GitHub 授权轮询失败: {e}"}

    user_data = _resp_json(user_resp)
    if user_resp.status_code != 200 or not isinstance(user_data, dict):
        _clear_pending_if(pending)
        return {"status": 1, "msg": _github_error_message(user_data, user_resp.status_code)}
    login = _read_str(user_data, "login")
    if not login:
        _clear_pending_if(pending)
        return {"status": 1, "msg": "GitHub 返回了无法解析的用户信息"}
    avatar_url = _read_str(user_data, "avatar_url")

    fernet = _get_fernet()
    if fernet is None:
        _clear_pending_if(pending)
        return {"status": 1, "msg": "REGISTER_CODE 未配置，无法加密保存 GitHub 凭据"}
    token_enc = fernet.encrypt(token.encode()).decode()

    if not await _save_record(
        GitHubAuthRecord(
            token_enc=token_enc,
            login=login,
            avatar_url=avatar_url,
            connected_at=datetime.now().isoformat(),
            can_star=can_star,
        )
    ):
        _clear_pending_if(pending)
        return {"status": 1, "msg": "GitHub 凭据写入失败"}
    _clear_cached_starred()
    _viewer = _Viewer(token=token, login=login, avatar_url=avatar_url, can_star=can_star)
    _viewer_loaded = True
    _clear_pending_if(pending)
    return {
        "status": 0,
        "msg": "ok",
        "data": DevicePollData(state="connected", login=login, avatar_url=avatar_url, can_star=can_star),
    }


@app.post("/api/github/disconnect", summary="断开 GitHub 连接", tags=PLUGINS)
async def github_disconnect(_user: SessionRecord = Depends(require_admin)) -> _Envelope:
    await _clear_connection()
    return {"status": 0, "msg": "已断开连接；如需撤销授权请到 GitHub 设置的 Applications 页操作"}


@app.post("/api/github/stars", summary="批量查询仓库 star 数与已 star 状态", tags=PLUGINS)
async def github_stars(body: _StarsBody, _user: SessionRecord = Depends(require_auth)) -> _Envelope:
    await _ensure_star_cache_loaded()
    viewer = await _get_viewer()
    repos = normalize_repo_names(body.repos)
    connected = viewer is not None
    if viewer is not None:
        result = await _fetch_graphql_stars(viewer["token"], repos)
        if result.revoked:
            # token 被用户撤销：清掉凭据，按未连接重新走匿名路径
            logger.warning(t("log.webconsole.github_token_revoked"))
            await _clear_connection()
            viewer = None
            connected = False
        elif result.error:
            return {"status": 1, "msg": result.error}
        else:
            # 与匿名路径一致：只缓存成功取到 star 数的条目并落盘
            dirty = False
            for repo, info in result.stars.items():
                if info["count"] is not None:
                    _cache_put(repo, info)
                    dirty = True
            if dirty:
                await _persist_star_cache()
            return {
                "status": 0,
                "msg": "ok",
                "data": StarsData(connected=True, stars=result.stars),
            }
    stars = await _fetch_anon_stars(repos)
    return {
        "status": 0,
        "msg": "ok",
        "data": StarsData(connected=connected, stars=stars),
    }


async def _set_star(owner: str, repo: str, star: bool) -> _Envelope:
    viewer = await _get_viewer()
    if viewer is None:
        return {"status": 1, "msg": "尚未连接 GitHub 账号"}
    if not viewer["can_star"]:
        return {"status": 1, "msg": "当前授权缺少 public_repo 权限，请重新连接 GitHub"}
    if not is_valid_repo_name(f"{owner}/{repo}"):
        return {"status": 1, "msg": "仓库名不合法"}
    try:
        async with httpx2.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            if star:
                resp = await client.put(
                    f"{_API_BASE}/user/starred/{owner}/{repo}",
                    headers=_headers(viewer["token"]),
                    content=b"",
                )
            else:
                resp = await client.delete(
                    f"{_API_BASE}/user/starred/{owner}/{repo}",
                    headers=_headers(viewer["token"]),
                )
    except httpx2.HTTPError as e:
        return {"status": 1, "msg": f"GitHub 请求失败: {e}"}
    if resp.status_code == 401:
        logger.warning(t("log.webconsole.github_token_revoked"))
        await _clear_connection()
        return {"status": 1, "msg": "GitHub 授权已失效，请重新连接"}
    if resp.status_code == 204:
        await _ensure_star_cache_loaded()
        key = f"{owner}/{repo}".lower()
        updated = _star_toggle_cache_update(_cache_get(key), star)
        if updated is not None:
            _cache_put(key, updated)
            if updated["count"] is not None:
                await _persist_star_cache()
        return {"status": 0, "msg": "ok", "data": StarToggleData(starred=star)}
    return {"status": 1, "msg": _github_error_message(_resp_json(resp), resp.status_code)}


@app.put("/api/github/star/{owner}/{repo}", summary="Star 一个 GitHub 仓库", tags=PLUGINS)
async def github_star(owner: str, repo: str, _user: SessionRecord = Depends(require_admin)) -> _Envelope:
    return await _set_star(owner, repo, True)


@app.delete("/api/github/star/{owner}/{repo}", summary="取消 star 一个 GitHub 仓库", tags=PLUGINS)
async def github_unstar(owner: str, repo: str, _user: SessionRecord = Depends(require_admin)) -> _Envelope:
    return await _set_star(owner, repo, False)
