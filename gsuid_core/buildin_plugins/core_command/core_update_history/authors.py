"""从 git / GitHub 解析 changelog 条目对应的 commit 作者头像。"""

from __future__ import annotations

import os
import re
import json
import base64
import asyncio
from io import BytesIO
from typing import Sequence
from pathlib import Path
from dataclasses import replace
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.image.utils import sget
from gsuid_core.utils.plugins_update.git_async import run_git

from .changelog import (
    CommitAuthor,
    ChangelogEntry,
    ChangelogVersion,
    changelog_dir,
    group_entries,
)

_DEFAULT_REPO = "Genshin-bots/gsuid_core"
_AVATAR_PX = 40
_API_CONCURRENCY = 6
_NOREPLY_RE = re.compile(
    r"^(?:(?P<id>\d+)\+)?(?P<login>.+)@users\.noreply\.github\.com$",
    re.IGNORECASE,
)
_OWNER_REPO_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)")
_CACHE_NAME = "authors.json"


def login_from_email(email: str) -> str:
    """从 GitHub noreply 邮箱取出登录名；其它邮箱返回空串。"""
    matched = _NOREPLY_RE.match(email.strip())
    if matched is None:
        return ""
    return matched.group("login")


def _user_id_from_email(email: str) -> str:
    matched = _NOREPLY_RE.match(email.strip())
    if matched is None:
        return ""
    user_id = matched.group("id")
    return user_id if user_id else ""


def _avatar_url_for(login: str, user_id: str, api_url: str) -> str:
    if api_url:
        return _sized_avatar_url(api_url)
    if user_id:
        return f"https://avatars.githubusercontent.com/u/{user_id}?s=80"
    if login:
        return f"https://github.com/{quote(login)}.png?size=80"
    return ""


def _sized_avatar_url(url: str) -> str:
    if "s=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}s=80"


def _cache_path() -> Path:
    return get_res_path("core_update_history") / _CACHE_NAME


def _repo_root() -> Path | None:
    root = changelog_dir()
    if root is None:
        return None
    return root.parent


def _unique_authors(authors: Sequence[CommitAuthor]) -> tuple[CommitAuthor, ...]:
    out: list[CommitAuthor] = []
    seen: set[str] = set()
    for author in authors:
        key = author.login.lower() if author.login else author.name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(author)
    return tuple(out)


def _entry_with_authors(entry: ChangelogEntry, by_sha: dict[str, CommitAuthor]) -> ChangelogEntry:
    found: list[CommitAuthor] = []
    for sha in entry.commits:
        author = by_sha[sha] if sha in by_sha else None
        if author is None and len(sha) >= 8 and sha[:8] in by_sha:
            author = by_sha[sha[:8]]
        if author is not None:
            found.append(author)
    authors = _unique_authors(found)
    if authors == entry.authors:
        return entry
    return replace(entry, authors=authors)


async def _load_cache() -> dict[str, dict[str, str]]:
    path = _cache_path()
    if not path.is_file():
        return {}
    raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for sha, item in payload.items():
        if not isinstance(sha, str) or not isinstance(item, dict):
            continue
        name = item["name"] if "name" in item and isinstance(item["name"], str) else ""
        login = item["login"] if "login" in item and isinstance(item["login"], str) else ""
        avatar_url = item["avatar_url"] if "avatar_url" in item and isinstance(item["avatar_url"], str) else ""
        out[sha[:8]] = {"name": name, "login": login, "avatar_url": avatar_url}
    return out


async def _save_cache(cache: dict[str, dict[str, str]]) -> None:
    path = _cache_path()
    text = json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True)
    await asyncio.to_thread(path.write_text, text, encoding="utf-8")


async def _origin_repo(root: Path) -> str:
    code, out, _err = await run_git(root, "remote", "get-url", "origin")
    if code != 0 or not out:
        return _DEFAULT_REPO
    matched = _OWNER_REPO_RE.search(out.strip())
    if matched is None:
        return _DEFAULT_REPO
    return f"{matched.group('owner')}/{matched.group('repo')}"


async def _git_authors(root: Path, shas: Sequence[str]) -> dict[str, tuple[str, str]]:
    """sha[:8] → (name, email)。仓库里没有的 SHA 不会出现在结果里。"""
    if not shas or not (root / ".git").exists():
        return {}
    # SHA 必须当 revision 传：写在 `--` 后面会被当成路径，结果只剩 HEAD。
    code, out, _err = await run_git(
        root,
        "log",
        "--no-walk",
        "--format=%H%x00%an%x00%ae",
        *shas,
    )
    if code != 0 or not out:
        return {}
    rows: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        parts = line.split("\x00")
        if len(parts) < 3:
            continue
        sha, name, email = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if sha:
            rows[sha[:8]] = (name, email)
    return rows


async def _github_token() -> str:
    """环境变量优先；没有则问本机 `gh auth token`。失败当无令牌。"""
    if "GITHUB_TOKEN" in os.environ:
        token = os.environ["GITHUB_TOKEN"].strip()
        if token:
            return token
    if "GH_TOKEN" in os.environ:
        token = os.environ["GH_TOKEN"].strip()
        if token:
            return token
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "auth",
            "token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _err = await asyncio.wait_for(proc.communicate(), timeout=3)
    except (OSError, TimeoutError):
        return ""
    if proc.returncode != 0 or not stdout:
        return ""
    return stdout.decode("utf-8", errors="replace").strip()


async def _github_headers() -> dict[str, str]:
    headers = {
        "User-Agent": "gsuid-core",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = await _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_commit_payload(payload: object) -> tuple[str, str, str]:
    """GitHub commit JSON → (name, login, avatar_url)。"""
    if not isinstance(payload, dict):
        return "", "", ""
    name = ""
    if "commit" in payload and isinstance(payload["commit"], dict):
        commit = payload["commit"]
        if "author" in commit and isinstance(commit["author"], dict):
            raw_name = commit["author"]["name"] if "name" in commit["author"] else ""
            if isinstance(raw_name, str):
                name = raw_name
    login = ""
    avatar_url = ""
    if "author" in payload and isinstance(payload["author"], dict):
        gh_author = payload["author"]
        raw_login = gh_author["login"] if "login" in gh_author else ""
        raw_avatar = gh_author["avatar_url"] if "avatar_url" in gh_author else ""
        if isinstance(raw_login, str):
            login = raw_login
        if isinstance(raw_avatar, str):
            avatar_url = raw_avatar
    return name, login, avatar_url


async def _github_commit(
    client: httpx.AsyncClient,
    repo: str,
    sha: str,
    gate: asyncio.Semaphore,
) -> tuple[str, str, str]:
    async with gate:
        try:
            resp = await client.get(f"https://api.github.com/repos/{repo}/commits/{sha}")
            resp.raise_for_status()
        except httpx.HTTPError:
            return "", "", ""
    return _parse_commit_payload(resp.json())


def _circle_png(raw: bytes, size: int) -> bytes:
    img = Image.open(BytesIO(raw)).convert("RGBA")
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((1, 1, size - 2, size - 2), fill=255)
    img.putalpha(mask)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def _avatar_uri(url: str) -> str:
    if not url:
        return ""
    try:
        resp = await sget(url, use_cache=True)
    except (httpx.HTTPError, OSError, TimeoutError):
        return ""
    if resp.status_code != 200 or not resp.content:
        return ""
    try:
        png = await asyncio.to_thread(_circle_png, resp.content, _AVATAR_PX)
    except OSError:
        return ""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def resolve_authors(shas: Sequence[str]) -> dict[str, CommitAuthor]:
    """短 SHA → 作者。git 优先，缺登录名再问 GitHub；头像失败则只留名字。"""
    unique: list[str] = []
    seen: set[str] = set()
    for sha in shas:
        key = sha[:8]
        if len(key) < 7 or key in seen:
            continue
        seen.add(key)
        unique.append(key)
    if not unique:
        return {}

    try:
        cache = await _load_cache()
    except (OSError, json.JSONDecodeError, UnicodeError):
        cache = {}

    pending = [sha for sha in unique if sha not in cache or not (cache[sha]["login"] if "login" in cache[sha] else "")]
    root = _repo_root()
    git_rows: dict[str, tuple[str, str]] = {}
    if pending and root is not None:
        git_rows = await _git_authors(root, pending)

    for sha, (name, email) in git_rows.items():
        login = login_from_email(email)
        user_id = _user_id_from_email(email)
        if login:
            cache[sha] = {
                "name": name,
                "login": login,
                "avatar_url": _avatar_url_for(login, user_id, ""),
            }

    still = [sha for sha in pending if not (cache[sha]["login"] if sha in cache and "login" in cache[sha] else "")]
    if still:
        reps: list[str] = []
        seen_email: set[str] = set()
        for sha in still:
            email_key = git_rows[sha][1].lower() if sha in git_rows else sha
            if email_key in seen_email:
                continue
            seen_email.add(email_key)
            reps.append(sha)
        repo = await _origin_repo(root) if root is not None else _DEFAULT_REPO
        gate = asyncio.Semaphore(_API_CONCURRENCY)
        timeout = httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=await _github_headers(),
            follow_redirects=True,
        ) as client:
            results = await asyncio.gather(*[_github_commit(client, repo, sha, gate) for sha in reps])
        by_rep: dict[str, dict[str, str]] = {}
        by_email: dict[str, dict[str, str]] = {}
        for sha, (name, login, avatar_url) in zip(reps, results, strict=True):
            git_name = git_rows[sha][0] if sha in git_rows else ""
            if not name:
                name = git_name
            if not name and not login:
                continue
            item = {
                "name": name,
                "login": login,
                "avatar_url": _avatar_url_for(login, "", avatar_url),
            }
            by_rep[sha] = item
            if sha in git_rows:
                by_email[git_rows[sha][1].lower()] = item
        for sha in still:
            item: dict[str, str] | None = None
            if sha in by_rep:
                item = by_rep[sha]
            elif sha in git_rows and git_rows[sha][1].lower() in by_email:
                found = by_email[git_rows[sha][1].lower()]
                item = {
                    "name": git_rows[sha][0] or found["name"],
                    "login": found["login"],
                    "avatar_url": found["avatar_url"],
                }
            if item is not None and (item["login"] or item["avatar_url"]):
                cache[sha] = item
            elif sha in git_rows:
                cache[sha] = {"name": git_rows[sha][0], "login": "", "avatar_url": ""}

    durable = {sha: item for sha, item in cache.items() if "login" in item and item["login"]}
    try:
        await _save_cache(durable)
    except OSError:
        pass

    url_by_login: dict[str, str] = {}
    for sha in unique:
        if sha not in cache:
            continue
        item = cache[sha]
        login = item["login"] if "login" in item else ""
        url = item["avatar_url"] if "avatar_url" in item else ""
        key = login or sha
        if key not in url_by_login:
            url_by_login[key] = url

    uris: dict[str, str] = {}
    fetched = await asyncio.gather(*[_avatar_uri(url) for url in url_by_login.values()])
    for key, uri in zip(url_by_login, fetched, strict=True):
        uris[key] = uri

    out: dict[str, CommitAuthor] = {}
    for sha in unique:
        if sha not in cache:
            continue
        item = cache[sha]
        name = item["name"] if "name" in item else ""
        login = item["login"] if "login" in item else ""
        key = login or sha
        uri = uris[key] if key in uris else ""
        out[sha] = CommitAuthor(name=name, login=login, avatar_uri=uri)
    return out


async def attach_authors(versions: Sequence[ChangelogVersion]) -> tuple[ChangelogVersion, ...]:
    """给版本条目填上作者头像。网络或 git 失败时原样返回。"""
    shas: list[str] = []
    seen: set[str] = set()
    for version in versions:
        for entry in version.entries:
            for sha in entry.commits:
                if sha not in seen:
                    seen.add(sha)
                    shas.append(sha)
    if not shas:
        return tuple(versions)
    try:
        by_sha = await resolve_authors(shas)
    except (httpx.HTTPError, OSError, json.JSONDecodeError, TimeoutError):
        return tuple(versions)
    painted: list[ChangelogVersion] = []
    for version in versions:
        entries = tuple(_entry_with_authors(entry, by_sha) for entry in version.entries)
        authors = _unique_authors(tuple(author for entry in entries for author in entry.authors))
        painted.append(replace(version, entries=entries, groups=group_entries(entries), authors=authors))
    return tuple(painted)
