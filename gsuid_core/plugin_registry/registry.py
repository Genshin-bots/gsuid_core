"""本地插件元信息中心：纯 Python 快速扫描、按需现扫 API 与无锁内存快照。"""

from __future__ import annotations

import os
import re
import json
import time
import uuid
import asyncio
from pathlib import Path
from weakref import WeakKeyDictionary
from dataclasses import field, replace, dataclass
from collections.abc import Mapping, Sequence

import toml

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import PLUGIN_STORE_INDEX_CACHE_PATH
from gsuid_core.utils.path_safety import PathEscapeError, safe_join, is_safe_filename
from gsuid_core.utils.plugins_update.api import CORE_PATH, PLUGINS_PATH
from gsuid_core.utils.plugins_update.git_async import (
    git_get_current_branch,
    git_get_head_commit_info,
)

from .models import (
    PluginScope,
    PluginLayout,
    MirrorChannel,
    PluginGitMeta,
    PluginKindMeta,
    LocalPluginMeta,
    PluginLoadStatus,
    MirrorProbeResult,
    ResolvedMirrorMode,
    probe_ttl,
)
from .source import (
    MIRROR_PREFIXES,
    CORE_PLUGIN_NAME,
    StoreCatalog,
    probe_target_url,
    build_store_catalog,
    classify_git_source,
)

BUILDIN_PLUGINS_PATH = Path(__file__).parents[1] / "buildin_plugins"

_MAX_PROBE_CACHE_SIZE = 512
_UI_PROBE_CHANNELS: frozenset[str] = frozenset({"cnb", "gitcode"})
_STORE_FETCH_COOLDOWN_SECONDS = 60.0
_GIT_CONCURRENCY = 4
_HTTP_CONCURRENCY = 6
_GIT_SECTION_RE = re.compile(r'^\[([A-Za-z0-9-]+)(?:\s+"((?:\\.|[^"\\])*)")?\]$')


def is_probe_fresh(probe: MirrorProbeResult, now: float | None = None) -> bool:
    """判断单条镜像探活记录是否在 TTL 有效期内。"""
    ts = time.time() if now is None else now
    return ts - probe.checked_at < probe_ttl(probe.state)


def _normalize_plugin_key(name: str) -> str:
    """统一归一化插件注册表 key（兼容 gscore 别名与单文件 .py 后缀）。"""
    clean = name.strip().lower()
    if not clean:
        return ""
    if clean == "gscore":
        return CORE_PLUGIN_NAME
    if clean.endswith(".py"):
        clean = clean[:-3]
    return clean


def _parse_git_config_value(raw: str) -> str:
    """解析单行 .git/config 值：去除未加引号的行尾注释并解包双引号转义。"""
    out: list[str] = []
    in_quotes = False
    i = 0
    length = len(raw)
    while i < length:
        ch = raw[i]
        if ch == "\\" and in_quotes and i + 1 < length:
            out.append(raw[i + 1])
            i += 2
            continue
        if ch == '"':
            in_quotes = not in_quotes
            i += 1
            continue
        if not in_quotes and ch in ("#", ";"):
            break
        out.append(ch)
        i += 1
    return "".join(out).strip()


@dataclass(frozen=True)
class _RawGitLocalState:
    is_git: bool
    remote_url: str = ""
    branch: str = ""
    recorded_upstream: str = ""
    recorded_mirror_mode: str = ""
    recorded_mirror_prefix: str = ""


@dataclass
class _RegistryLoopState:
    git_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(_GIT_CONCURRENCY))
    http_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(_HTTP_CONCURRENCY))
    refresh_all_task: asyncio.Task[list[LocalPluginMeta]] | None = None
    commit_enrich_task: asyncio.Task[None] | None = None
    store_save_task: asyncio.Task[None] | None = None
    store_fetch_task: asyncio.Task[bool] | None = None
    store_bootstrap_task: asyncio.Task[bool] | None = None
    probe_tasks: dict[tuple[PluginScope, str, str, str], asyncio.Task[MirrorProbeResult]] = field(default_factory=dict)


def _read_pyproject_table(pyproject: Path) -> dict[str, object] | None:
    try:
        raw: object = toml.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, toml.TomlDecodeError) as exc:
        logger.error(t("log.server.pyproject_parse_fail", pyproject=pyproject, error=exc))
        return None
    if not isinstance(raw, dict):
        logger.error(t("log.server.pyproject_parse_fail", pyproject=pyproject, error="root is not a table"))
        return None
    return raw


def read_meta_plugin_info(plugin: Path) -> PluginKindMeta | None:
    """目录是 meta plugin 则返回元数据，否则 None。"""
    if not plugin.is_dir():
        return None
    is_meta = (plugin / "__meta_plugin__.py").is_file()
    provides = plugin.stem
    pyproject = plugin / "pyproject.toml"
    if pyproject.is_file():
        toml_data = _read_pyproject_table(pyproject)
        if toml_data is None:
            if not is_meta:
                return None
        else:
            tool = toml_data["tool"] if "tool" in toml_data and isinstance(toml_data["tool"], dict) else {}
            gsuid = tool["gsuid"] if "gsuid" in tool and isinstance(tool["gsuid"], dict) else {}
            kind = gsuid["kind"] if "kind" in gsuid else None
            if kind == "meta":
                is_meta = True
            raw_provides = gsuid["provides"] if "provides" in gsuid else None
            if isinstance(raw_provides, str) and raw_provides.strip():
                provides = raw_provides.strip()
    if not is_meta:
        return None
    return PluginKindMeta(kind="meta", provides=provides, plugin_name=plugin.stem)


def is_valid_plugin_entry(entry: Path, *, scope: PluginScope) -> bool:
    """统一目录过滤：排除隐藏与双下划线缓存目录，保留单下划线禁用插件供管理与卸载。"""
    name = entry.name
    if not name or name.startswith(".") or name.startswith("__"):
        return False
    if scope == "plugin" and name.lower() == "core_command":
        return False
    if entry.is_dir():
        return True
    return entry.is_file() and entry.suffix == ".py"


def validate_installable_plugin_name(name: str) -> str | None:
    """校验拟安装的插件目录名是否合法且可加载；合法返回 None，否则返回错误文案。"""
    if not name or not is_safe_filename(name):
        return f"插件注册名 {name} 非法"
    if name.startswith((".", "_")):
        return f"插件注册名 {name} 不能以 . 或 _ 开头"
    if name.lower() in {"core_command", CORE_PLUGIN_NAME, "gscore"}:
        return f"插件注册名 {name} 不能占用内置或核心保留名"
    return None


def detect_plugin_layout(entry: Path, *, plugin_name: str | None = None) -> PluginLayout:
    """识别插件布局，与 GsServer.load_plugin 保持同一判定口径。"""
    if entry.is_file() and entry.suffix == ".py":
        return "single"
    if not entry.is_dir():
        return "unknown"
    if (entry / "__full__.py").is_file():
        return "full"
    inner_name = (plugin_name if plugin_name is not None else entry.name).removesuffix("-dev")
    if (entry / "__nest__.py").is_file() or (entry / inner_name).is_dir():
        return "nested"
    if (entry / "__init__.py").is_file():
        return "package"
    return "unknown"


def is_loadable_plugin_dir(entry: Path, *, plugin_name: str | None = None) -> bool:
    """判断目录落地为 plugin_name 后能否被 GsServer.load_plugin 发现可导入模块。"""
    if not entry.is_dir():
        return False
    layout = detect_plugin_layout(entry, plugin_name=plugin_name)
    if layout == "full":
        return (entry / "__init__.py").is_file() or any(
            child.is_dir() and (child / "__init__.py").is_file() for child in entry.iterdir()
        )
    if layout == "nested":
        inner_name = (plugin_name if plugin_name is not None else entry.name).removesuffix("-dev")
        inner = entry / inner_name
        if not inner.is_dir():
            return False
        return (inner / "__init__.py").is_file() or any(
            child.is_dir() and (child / "__init__.py").is_file() for child in inner.iterdir()
        )
    return layout == "package"


def _resolve_git_dirs(repo_dir: Path) -> tuple[Path, Path] | None:
    """解析 (git_dir, common_dir)，兼容普通 .git 目录与 worktree/submodule .git 文件。"""
    dot_git = repo_dir / ".git"
    if not dot_git.exists():
        return None
    if dot_git.is_dir():
        return dot_git, dot_git
    if not dot_git.is_file():
        return None
    try:
        first_line = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not first_line.lower().startswith("gitdir:"):
        return None
    raw_gitdir = first_line.split(":", 1)[1].strip()
    git_dir = Path(raw_gitdir)
    if not git_dir.is_absolute():
        git_dir = (repo_dir / git_dir).resolve()
    if not git_dir.is_dir():
        return None
    common_dir = git_dir
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        try:
            rel_common = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
            if rel_common:
                cand = Path(rel_common)
                common_dir = cand if cand.is_absolute() else (git_dir / cand).resolve()
        except OSError:
            pass
    return git_dir, common_dir


def _read_git_local_state(repo_dir: Path) -> _RawGitLocalState:
    """纯 Python 读取 .git/config 与 .git/HEAD，零子进程、毫秒级完成。"""
    if not repo_dir.is_dir():
        return _RawGitLocalState(is_git=False)
    dirs = _resolve_git_dirs(repo_dir)
    if dirs is None:
        return _RawGitLocalState(is_git=False)
    git_dir, common_dir = dirs

    remote_url = ""
    recorded_upstream = ""
    recorded_mode = ""
    recorded_prefix = ""
    config_file = common_dir / "config"
    if config_file.is_file():
        try:
            lines = config_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        section_name = ""
        subsection_name = ""
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            sec_match = _GIT_SECTION_RE.match(line)
            if sec_match is not None:
                section_name = sec_match.group(1).lower()
                raw_sub = sec_match.group(2)
                subsection_name = raw_sub.replace('\\"', '"').replace("\\\\", "\\") if raw_sub is not None else ""
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = k.strip().lower()
            val = _parse_git_config_value(v.strip())
            # Git 语义：同名键重复出现时最后一条生效
            if section_name == "remote" and subsection_name == "origin" and key == "url":
                remote_url = val
            elif section_name == "gscore" and not subsection_name:
                if key == "upstreamurl":
                    recorded_upstream = val
                elif key == "mirrormode":
                    recorded_mode = val
                elif key == "mirrorprefix":
                    recorded_prefix = val

    branch = ""
    head_file = git_dir / "HEAD"
    if head_file.is_file():
        try:
            head_text = head_file.read_text(encoding="utf-8", errors="replace").strip()
            if head_text.startswith("ref: refs/heads/"):
                branch = head_text.removeprefix("ref: refs/heads/").strip()
        except OSError:
            pass

    return _RawGitLocalState(
        is_git=True,
        remote_url=remote_url,
        branch=branch,
        recorded_upstream=recorded_upstream,
        recorded_mirror_mode=recorded_mode,
        recorded_mirror_prefix=recorded_prefix,
    )


def _build_meta_from_disk(
    entry_path: Path,
    *,
    scope: PluginScope,
    catalog: StoreCatalog,
    existing: LocalPluginMeta | None = None,
) -> LocalPluginMeta:
    resolved = entry_path.resolve()
    name = CORE_PLUGIN_NAME if scope == "core" else (entry_path.stem if entry_path.is_file() else entry_path.name)
    layout: PluginLayout = "package" if scope == "core" else detect_plugin_layout(entry_path)
    meta_info = None if scope == "core" else read_meta_plugin_info(entry_path)
    has_pyproject = (entry_path / "pyproject.toml").is_file() if entry_path.is_dir() else False
    icon_path: Path | None = None
    if entry_path.is_dir():
        try:
            icon_candidate = safe_join(resolved, "ICON.png")
        except PathEscapeError:
            icon_candidate = None
        if icon_candidate is not None and icon_candidate.is_file():
            icon_path = icon_candidate

    raw_git = _read_git_local_state(entry_path)
    prev_git = existing.git if existing is not None else None
    now = time.time()
    disabled = scope == "plugin" and entry_path.name.startswith("_")
    load_status: PluginLoadStatus = (
        "disabled" if disabled else (existing.load_status if existing is not None else "unloaded")
    )

    if not raw_git.is_git:
        return LocalPluginMeta(
            name=name,
            key=name.lower(),
            path=resolved,
            scope=scope,
            layout=layout,
            source_kind="local",
            is_git=False,
            has_remote=False,
            git=None,
            is_meta=meta_info is not None,
            meta_provides=meta_info.provides if meta_info is not None else None,
            has_pyproject=has_pyproject,
            icon_path=icon_path,
            load_status=load_status,
            last_error=existing.last_error if existing is not None else None,
            updated_at=now,
            disabled=disabled,
        )

    classified = classify_git_source(
        raw_git.remote_url,
        recorded_upstream=raw_git.recorded_upstream,
        recorded_mirror_mode=raw_git.recorded_mirror_mode,
        recorded_mirror_prefix=raw_git.recorded_mirror_prefix,
        plugin_name=name,
        catalog=catalog,
    )
    same_remote = prev_git is not None and prev_git.remote_url == classified.remote_url
    git_meta = PluginGitMeta(
        commit_hash=prev_git.commit_hash if prev_git is not None else "",
        short_commit=prev_git.short_commit if prev_git is not None else "",
        commit_author=prev_git.commit_author if prev_git is not None else "",
        commit_date=prev_git.commit_date if prev_git is not None else "",
        commit_message=prev_git.commit_message if prev_git is not None else "",
        branch=raw_git.branch or (prev_git.branch if prev_git is not None else ""),
        remote_url=classified.remote_url,
        active_mirror=classified.active_mirror,
        active_mirror_prefix=classified.active_mirror_prefix,
        upstream_url=classified.upstream_url,
        upstream_host=classified.upstream_host,
        upstream_provider=classified.upstream_provider,
        upstream_owner=classified.upstream_owner,
        upstream_repo=classified.upstream_repo,
        store_plugin_id=classified.store_plugin_id,
        store_upstream_url=classified.store_upstream_url,
        supported_mirrors=classified.supported_mirrors,
        mirror_probes=prev_git.mirror_probes if same_remote and prev_git is not None else (),
        recorded_upstream=raw_git.recorded_upstream,
    )
    return LocalPluginMeta(
        name=name,
        key=name.lower(),
        path=resolved,
        scope=scope,
        layout=layout,
        source_kind=classified.source_kind,
        is_git=True,
        has_remote=bool(classified.remote_url),
        git=git_meta,
        is_meta=meta_info is not None,
        meta_provides=meta_info.provides if meta_info is not None else None,
        has_pyproject=has_pyproject,
        icon_path=icon_path,
        load_status=load_status,
        last_error=existing.last_error if existing is not None else None,
        updated_at=now,
        disabled=disabled,
    )


def load_cached_store_links(cache_path: Path = PLUGIN_STORE_INDEX_CACHE_PATH) -> dict[str, str]:
    """读取本地离线商店索引快照（{plugin_id: link}），损坏或缺失返回空字典。"""
    if not cache_path.is_file():
        return {}
    try:
        loaded: object = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(t("log.plugin.registry_store_cache_fail", error=exc))
        return {}
    if not isinstance(loaded, dict) or "plugins" not in loaded:
        return {}
    raw_plugins = loaded["plugins"]
    if not isinstance(raw_plugins, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw_plugins.items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def save_cached_store_links(
    links: Mapping[str, str],
    cache_path: Path = PLUGIN_STORE_INDEX_CACHE_PATH,
) -> None:
    """原子落盘轻量商店索引快照（唯一临时文件 + os.replace，避免 Windows 并发冲突）。"""
    payload = {"updated_at": time.time(), "plugins": dict(links)}
    tmp = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning(t("log.plugin.registry_store_cache_fail", error=exc))


def scan_disk_plugins(
    *,
    include_buildin: bool = False,
    include_core: bool = False,
    git_only: bool = False,
    plugins_path: Path = PLUGINS_PATH,
    buildin_path: Path = BUILDIN_PLUGINS_PATH,
    core_path: Path = CORE_PATH,
    catalog: StoreCatalog | None = None,
    existing_map: Mapping[tuple[PluginScope, str], LocalPluginMeta] | None = None,
) -> list[LocalPluginMeta]:
    """同步纯磁盘按需现扫（零子进程，供启动与线程池调用）。"""
    active_catalog = catalog if catalog is not None else build_store_catalog(load_cached_store_links())
    prev = existing_map if existing_map is not None else {}
    results: list[LocalPluginMeta] = []

    if include_core and core_path.exists():
        core_ek: tuple[PluginScope, str] = ("core", CORE_PLUGIN_NAME)
        core_prev = prev[core_ek] if core_ek in prev else None
        core_meta = _build_meta_from_disk(core_path, scope="core", catalog=active_catalog, existing=core_prev)
        if not git_only or (core_meta.is_git and core_meta.has_remote):
            results.append(core_meta)

    if plugins_path.is_dir():
        seen_plugin_keys: set[str] = set()
        # 同名目录优先于单文件 .py，防止同 scope 内静默覆盖
        for entry in sorted(plugins_path.iterdir(), key=lambda p: (0 if p.is_dir() else 1, p.name.lower())):
            if not is_valid_plugin_entry(entry, scope="plugin"):
                continue
            key = (entry.stem if entry.is_file() else entry.name).lower()
            if key in seen_plugin_keys:
                continue
            seen_plugin_keys.add(key)
            ek: tuple[PluginScope, str] = ("plugin", key)
            meta = _build_meta_from_disk(
                entry,
                scope="plugin",
                catalog=active_catalog,
                existing=prev[ek] if ek in prev else None,
            )
            if git_only and not (meta.is_git and meta.has_remote):
                continue
            results.append(meta)

    if include_buildin and buildin_path.is_dir():
        seen_buildin_keys: set[str] = set()
        for entry in sorted(buildin_path.iterdir(), key=lambda p: (0 if p.is_dir() else 1, p.name.lower())):
            if not is_valid_plugin_entry(entry, scope="buildin"):
                continue
            key = (entry.stem if entry.is_file() else entry.name).lower()
            if key in seen_buildin_keys:
                continue
            seen_buildin_keys.add(key)
            ek = ("buildin", key)
            meta = _build_meta_from_disk(
                entry,
                scope="buildin",
                catalog=active_catalog,
                existing=prev[ek] if ek in prev else None,
            )
            if git_only and not (meta.is_git and meta.has_remote):
                continue
            results.append(meta)

    return results


async def scan_disk_plugins_async(
    *,
    include_buildin: bool = False,
    include_core: bool = False,
    git_only: bool = False,
    plugins_path: Path = PLUGINS_PATH,
    buildin_path: Path = BUILDIN_PLUGINS_PATH,
    core_path: Path = CORE_PATH,
    catalog: StoreCatalog | None = None,
) -> list[LocalPluginMeta]:
    """异步磁盘按需现扫（放入线程池，绝不阻塞事件循环）。"""
    return await asyncio.to_thread(
        scan_disk_plugins,
        include_buildin=include_buildin,
        include_core=include_core,
        git_only=git_only,
        plugins_path=plugins_path,
        buildin_path=buildin_path,
        core_path=core_path,
        catalog=catalog,
    )


def resolve_plugin_path_sync(
    name: str,
    *,
    include_core: bool,
    include_buildin: bool,
    plugins_path: Path,
    buildin_path: Path,
    core_path: Path,
) -> tuple[Path, PluginScope] | None:
    clean = name.strip()
    if not clean:
        return None
    if include_core and clean.lower() in (CORE_PLUGIN_NAME, "gscore"):
        return (core_path, "core") if core_path.exists() else None
    if not is_safe_filename(clean):
        return None

    roots: list[tuple[Path, PluginScope]] = [(plugins_path, "plugin")]
    if include_buildin:
        roots.append((buildin_path, "buildin"))

    for base, scope in roots:
        if not base.is_dir():
            continue
        try:
            direct = safe_join(base, clean)
        except PathEscapeError:
            direct = None
        if direct is not None and direct.exists() and is_valid_plugin_entry(direct, scope=scope):
            return direct, scope
        try:
            single = safe_join(base, f"{clean}.py")
        except PathEscapeError:
            single = None
        if single is not None and single.is_file() and is_valid_plugin_entry(single, scope=scope):
            return single, scope

    low = clean.lower()
    dev_low = f"{low}-dev"
    dev_fallback: tuple[Path, PluginScope] | None = None
    for base, scope in roots:
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if not is_valid_plugin_entry(entry, scope=scope):
                continue
            cand_name = entry.stem if entry.is_file() else entry.name
            cand_low = cand_name.lower()
            if cand_low == low:
                try:
                    return safe_join(base, entry.name), scope
                except PathEscapeError:
                    continue
            if dev_fallback is None and scope == "plugin" and cand_low == dev_low:
                try:
                    dev_fallback = (safe_join(base, entry.name), scope)
                except PathEscapeError:
                    continue
    return dev_fallback


async def resolve_plugin_on_disk(
    name: str,
    *,
    include_core: bool = False,
    include_buildin: bool = False,
    plugins_path: Path = PLUGINS_PATH,
    buildin_path: Path = BUILDIN_PLUGINS_PATH,
    core_path: Path = CORE_PATH,
    catalog: StoreCatalog | None = None,
) -> LocalPluginMeta | None:
    """统一安全路径解析：防路径穿越、支持精确与大小写不敏感匹配。"""
    found = await asyncio.to_thread(
        resolve_plugin_path_sync,
        name,
        include_core=include_core,
        include_buildin=include_buildin,
        plugins_path=plugins_path,
        buildin_path=buildin_path,
        core_path=core_path,
    )
    if found is None:
        return None
    target_path, scope = found
    active_catalog = catalog if catalog is not None else plugin_registry.catalog
    return await asyncio.to_thread(
        _build_meta_from_disk,
        target_path,
        scope=scope,
        catalog=active_catalog,
    )


async def probe_plugin_disk(
    path: Path,
    *,
    scope: PluginScope = "plugin",
    with_commit: bool = False,
    catalog: StoreCatalog | None = None,
    git_sem: asyncio.Semaphore | None = None,
) -> LocalPluginMeta | None:
    """按需探测单个插件目录（可选拉取单次 git log -1）。"""
    if not await asyncio.to_thread(path.exists):
        return None
    active_catalog = catalog if catalog is not None else plugin_registry.catalog
    meta = await asyncio.to_thread(_build_meta_from_disk, path, scope=scope, catalog=active_catalog)
    if with_commit and meta.is_git:
        meta = await _enrich_single_commit(meta, git_sem=git_sem)
    return meta


async def _enrich_single_commit(
    meta: LocalPluginMeta,
    *,
    git_sem: asyncio.Semaphore | None = None,
) -> LocalPluginMeta:
    if not meta.is_git or meta.git is None:
        return meta

    async def _fetch() -> tuple[tuple[str, str, str, str, str] | None, str]:
        commit_info = await git_get_head_commit_info(meta.path, timeout=8)
        branch = meta.git.branch if meta.git is not None else ""
        if not branch:
            branch = await git_get_current_branch(meta.path)
        return commit_info, branch

    if git_sem is not None:
        async with git_sem:
            commit_info, branch = await _fetch()
    else:
        commit_info, branch = await _fetch()

    if commit_info is None and branch == meta.git.branch:
        return meta

    new_git = replace(
        meta.git,
        commit_hash=commit_info[0] if commit_info is not None else meta.git.commit_hash,
        short_commit=commit_info[1] if commit_info is not None else meta.git.short_commit,
        commit_author=commit_info[2] if commit_info is not None else meta.git.commit_author,
        commit_date=commit_info[3] if commit_info is not None else meta.git.commit_date,
        commit_message=commit_info[4] if commit_info is not None else meta.git.commit_message,
        branch=branch,
    )
    return replace(meta, git=new_git, updated_at=time.time())


class LocalPluginRegistry:
    """本地插件元信息中心：无锁内存快照 + single-flight 刷新 + 离线商店快照。"""

    def __init__(
        self,
        *,
        plugins_path: Path | None = None,
        buildin_path: Path | None = None,
        core_path: Path | None = None,
        store_cache_path: Path | None = None,
        allow_remote_store_fetch: bool = True,
    ) -> None:
        self._plugins_path_override = plugins_path
        self._buildin_path_override = buildin_path
        self._core_path_override = core_path
        self._store_cache_path_override = store_cache_path
        self._allow_remote_store_fetch = allow_remote_store_fetch
        self._entries: dict[tuple[PluginScope, str], LocalPluginMeta] = {}
        self._initialized = False
        self._commits_enriched = False
        self._store_links: dict[str, str] = {}
        self._store_fetch_failed_at: float = 0.0
        self._catalog: StoreCatalog = build_store_catalog({})
        self._probe_cache: dict[tuple[PluginScope, str, str, str], MirrorProbeResult] = {}
        self._loop_states: WeakKeyDictionary[asyncio.AbstractEventLoop, _RegistryLoopState] = WeakKeyDictionary()

    @property
    def plugins_path(self) -> Path:
        return self._plugins_path_override if self._plugins_path_override is not None else PLUGINS_PATH

    @property
    def buildin_path(self) -> Path:
        return self._buildin_path_override if self._buildin_path_override is not None else BUILDIN_PLUGINS_PATH

    @property
    def core_path(self) -> Path:
        return self._core_path_override if self._core_path_override is not None else CORE_PATH

    @property
    def store_cache_path(self) -> Path:
        return (
            self._store_cache_path_override
            if self._store_cache_path_override is not None
            else PLUGIN_STORE_INDEX_CACHE_PATH
        )

    @property
    def catalog(self) -> StoreCatalog:
        return self._catalog

    @property
    def has_store_catalog(self) -> bool:
        return bool(self._store_links)

    def has_unresolved_builtin_mirror(self) -> bool:
        """是否存在命中内置扁平镜像（CNB/GitCode）但尚无 GitHub 上游地址的存量插件。"""
        for item in self._entries.values():
            if item.scope != "plugin" or not item.is_git or item.git is None:
                continue
            if item.git.active_mirror in ("cnb", "gitcode") and not item.git.upstream_url:
                return True
        return False

    def _get_loop_state(self) -> _RegistryLoopState:
        loop = asyncio.get_running_loop()
        if loop not in self._loop_states:
            self._loop_states[loop] = _RegistryLoopState()
        return self._loop_states[loop]

    def _entry_probes(
        self,
        scope: PluginScope,
        key: str,
        *,
        now: float | None = None,
    ) -> tuple[MirrorProbeResult, ...]:
        ts = time.time() if now is None else now
        by_channel: dict[str, MirrorProbeResult] = {}
        for (sc, pk, _ch, _url), res in self._probe_cache.items():
            if sc != scope or pk != key or res.channel not in _UI_PROBE_CHANNELS:
                continue
            if not is_probe_fresh(res, ts):
                continue
            prev = by_channel[res.channel] if res.channel in by_channel else None
            if prev is None or res.checked_at >= prev.checked_at:
                by_channel[res.channel] = res
        return tuple(by_channel[ch] for ch in sorted(by_channel))

    def _attach_cached_probes(self, meta: LocalPluginMeta, *, now: float | None = None) -> LocalPluginMeta:
        if not meta.is_git or meta.git is None:
            return meta
        probes = self._entry_probes(meta.scope, meta.key, now=now)
        if probes == meta.git.mirror_probes:
            return meta
        return replace(meta, git=replace(meta.git, mirror_probes=probes))

    def _filter_entry_probes(self, meta: LocalPluginMeta, *, now: float) -> LocalPluginMeta:
        """无锁读路径实时过滤已过期的 UI 探活胶囊，防止过期 not_synced 长期锁死选项。"""
        if not meta.is_git or meta.git is None or not meta.git.mirror_probes:
            return meta
        fresh = tuple(p for p in meta.git.mirror_probes if p.channel in _UI_PROBE_CHANNELS and is_probe_fresh(p, now))
        if len(fresh) == len(meta.git.mirror_probes):
            return meta
        return replace(meta, git=replace(meta.git, mirror_probes=fresh))

    async def ensure_store_catalog_loaded(self, *, timeout: float = 4.0) -> bool:
        """按需加载插件商店上游索引（离线缓存优先，缺失时 single-flight 轻量拉取带失败冷却）。"""
        if self._store_links:
            return True
        loaded_links = await asyncio.to_thread(load_cached_store_links, self.store_cache_path)
        if loaded_links:
            self.reclassify_with_store(loaded_links, persist=False)
            return True
        if not self._allow_remote_store_fetch:
            return False
        now = time.time()
        if self._store_fetch_failed_at > 0 and now - self._store_fetch_failed_at < _STORE_FETCH_COOLDOWN_SECONDS:
            return False

        state = self._get_loop_state()
        if state.store_fetch_task is not None and not state.store_fetch_task.done():
            return await asyncio.shield(state.store_fetch_task)

        async def _fetch_store() -> bool:
            import httpx2

            from gsuid_core.utils.plugins_update.api import plugins_lib

            try:
                async with httpx2.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(plugins_lib)
                    resp.raise_for_status()
                    payload: object = resp.json()
            except (httpx2.HTTPError, ValueError) as exc:
                self._store_fetch_failed_at = time.time()
                logger.warning(t("log.plugin.registry_store_cache_fail", error=exc))
                return False

            if not isinstance(payload, dict) or "plugins" not in payload or not isinstance(payload["plugins"], dict):
                self._store_fetch_failed_at = time.time()
                return False
            self.reclassify_with_store(payload["plugins"], persist=True)
            if self._store_links:
                self._store_fetch_failed_at = 0.0
                return True
            self._store_fetch_failed_at = time.time()
            return False

        task = asyncio.create_task(_fetch_store())
        state.store_fetch_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if state.store_fetch_task is task:
                state.store_fetch_task = None

    def schedule_store_catalog_bootstrap(self) -> None:
        """仅当本地存在无上游记录的存量 CNB/GitCode 镜像仓且无缓存时，后台预热商店索引。"""
        if self.has_store_catalog or not self._allow_remote_store_fetch or not self.has_unresolved_builtin_mirror():
            return
        state = self._get_loop_state()
        if state.store_bootstrap_task is not None and not state.store_bootstrap_task.done():
            return

        async def _bg() -> bool:
            return await self.ensure_store_catalog_loaded(timeout=5.0)

        task = asyncio.create_task(_bg())
        state.store_bootstrap_task = task

        def _clear(done_task: asyncio.Task[bool]) -> None:
            if state.store_bootstrap_task is done_task:
                state.store_bootstrap_task = None

        task.add_done_callback(_clear)

    async def refresh_all(self, *, with_commit: bool = False) -> list[LocalPluginMeta]:
        """全量扫描本地插件并原子更新快照（同一事件循环内 single-flight 合并）。"""
        state = self._get_loop_state()
        if state.refresh_all_task is not None and not state.refresh_all_task.done():
            result = await asyncio.shield(state.refresh_all_task)
            if with_commit and (not self._commits_enriched or self._has_missing_commit_info()):
                await self.ensure_commits_loaded()
                return list(self._entries.values())
            return result

        async def _runner() -> list[LocalPluginMeta]:
            if not self._store_links:
                loaded_links = await asyncio.to_thread(load_cached_store_links, self.store_cache_path)
                if loaded_links:
                    self._store_links = loaded_links
                    self._catalog = build_store_catalog(loaded_links)

            catalog_snapshot = self._catalog
            scanned = await asyncio.to_thread(
                scan_disk_plugins,
                include_buildin=True,
                include_core=True,
                git_only=False,
                plugins_path=self.plugins_path,
                buildin_path=self.buildin_path,
                core_path=self.core_path,
                catalog=catalog_snapshot,
                existing_map=self._entries,
            )
            # 合并扫描期间并发写入的加载状态、commit 与最新商店目录，避免整表覆盖丢态
            latest_entries = self._entries
            latest_catalog = self._catalog
            new_entries: dict[tuple[PluginScope, str], LocalPluginMeta] = {}
            for item in scanned:
                merged = item
                ek: tuple[PluginScope, str] = (merged.scope, merged.key)
                if ek in latest_entries:
                    live = latest_entries[ek]
                    if not merged.disabled and live.load_status != "unloaded" and merged.load_status == "unloaded":
                        merged = replace(merged, load_status=live.load_status, last_error=live.last_error)
                    elif not merged.disabled and live.updated_at > merged.updated_at:
                        merged = replace(merged, load_status=live.load_status, last_error=live.last_error)
                    if (
                        merged.git is not None
                        and live.git is not None
                        and not merged.git.commit_hash
                        and live.git.commit_hash
                    ):
                        merged = replace(
                            merged,
                            git=replace(
                                merged.git,
                                commit_hash=live.git.commit_hash,
                                short_commit=live.git.short_commit,
                                commit_author=live.git.commit_author,
                                commit_date=live.git.commit_date,
                                commit_message=live.git.commit_message,
                                branch=merged.git.branch or live.git.branch,
                            ),
                        )
                if latest_catalog is not catalog_snapshot and merged.is_git and merged.git is not None:
                    merged = self._reclassify_entry(merged, latest_catalog)
                new_entries[ek] = self._attach_cached_probes(merged)
            self._entries = new_entries
            self._initialized = True

            user_plugins = [m for m in new_entries.values() if m.scope == "plugin"]
            git_count = sum(1 for m in user_plugins if m.is_git and m.has_remote)
            logger.debug(
                t(
                    "log.plugin.registry_refresh_done",
                    total=len(user_plugins),
                    git_count=git_count,
                    local_count=len(user_plugins) - git_count,
                )
            )
            if with_commit:
                if state.commit_enrich_task is not None and not state.commit_enrich_task.done():
                    await asyncio.shield(state.commit_enrich_task)
                else:
                    await self._run_commit_enrichment(state)
            return list(self._entries.values())

        task = asyncio.create_task(_runner())
        state.refresh_all_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if state.refresh_all_task is task:
                state.refresh_all_task = None

    def _has_missing_commit_info(self) -> bool:
        return any(item.is_git and item.git is not None and not item.git.commit_hash for item in self._entries.values())

    async def _run_commit_enrichment(self, state: _RegistryLoopState) -> None:
        targets = [m for m in self._entries.values() if m.is_git and m.git is not None and not m.git.commit_hash]
        if not targets:
            self._commits_enriched = True
            return

        async def _one(item: LocalPluginMeta) -> None:
            enriched = await _enrich_single_commit(item, git_sem=state.git_sem)
            ek: tuple[PluginScope, str] = (enriched.scope, enriched.key)
            current = self._entries
            if ek not in current:
                return
            latest = current[ek]
            if latest.git is None or enriched.git is None:
                return
            merged_git = replace(
                latest.git,
                commit_hash=enriched.git.commit_hash or latest.git.commit_hash,
                short_commit=enriched.git.short_commit or latest.git.short_commit,
                commit_author=enriched.git.commit_author or latest.git.commit_author,
                commit_date=enriched.git.commit_date or latest.git.commit_date,
                commit_message=enriched.git.commit_message or latest.git.commit_message,
                branch=latest.git.branch or enriched.git.branch,
            )
            updated = dict(self._entries)
            updated[ek] = replace(latest, git=merged_git)
            self._entries = updated

        results = await asyncio.gather(*(_one(item) for item in targets), return_exceptions=True)
        had_error = False
        for res in results:
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                had_error = True
                logger.warning(t("log.plugin.registry_commit_enrich_fail", error=res))
        if not had_error:
            self._commits_enriched = not self._has_missing_commit_info()

    def schedule_commit_enrichment(self) -> None:
        """在后台异步补全全部 Git 插件 commit 信息，不阻塞启动钩子与 WS 服务。"""
        state = self._get_loop_state()
        if state.commit_enrich_task is not None and not state.commit_enrich_task.done():
            return

        async def _bg() -> None:
            try:
                await self._run_commit_enrichment(state)
            except Exception as exc:
                logger.warning(t("log.plugin.registry_commit_enrich_fail", error=exc))

        task = asyncio.create_task(_bg())
        state.commit_enrich_task = task

    async def ensure_commits_loaded(self) -> None:
        """确保 Git commit 信息至少已补全一次（复用在途后台任务）。"""
        if not self._initialized:
            await self.refresh_all(with_commit=False)
        if self._commits_enriched and not self._has_missing_commit_info():
            return
        state = self._get_loop_state()
        if state.commit_enrich_task is None or state.commit_enrich_task.done():
            self.schedule_commit_enrichment()
        if state.commit_enrich_task is not None:
            await asyncio.shield(state.commit_enrich_task)

    def refresh_entry_sync(self, name: str) -> LocalPluginMeta | None:
        """同步纯盘刷新单个插件快照（零子进程，供同步重载路径建档与更新元信息）。"""
        found = resolve_plugin_path_sync(
            name,
            include_core=True,
            include_buildin=True,
            plugins_path=self.plugins_path,
            buildin_path=self.buildin_path,
            core_path=self.core_path,
        )
        if found is None:
            return None
        target_path, scope = found
        key = _normalize_plugin_key(name)
        prev = self._entries[(scope, key)] if (scope, key) in self._entries else None
        if prev is None and (scope, f"{key}-dev") in self._entries:
            prev = self._entries[(scope, f"{key}-dev")]
        meta = _build_meta_from_disk(target_path, scope=scope, catalog=self._catalog, existing=prev)
        ek: tuple[PluginScope, str] = (scope, meta.key)
        updated = dict(self._entries)
        updated[ek] = self._attach_cached_probes(meta)
        self._entries = updated
        return updated[ek]

    def ensure_entry_sync(self, name: str) -> LocalPluginMeta | None:
        """同步探测并补齐单个插件快照（已存在时直接返回）。"""
        existing = self.get(name, include_core=True, include_buildin=True)
        if existing is not None:
            return existing
        return self.refresh_entry_sync(name)

    async def refresh_one(self, name: str, *, with_commit: bool = True) -> LocalPluginMeta | None:
        """精准重扫单个插件目录；已删除则从内存快照摘除。"""
        key = _normalize_plugin_key(name)
        if not key:
            return None
        found = await asyncio.to_thread(
            resolve_plugin_path_sync,
            name.strip(),
            include_core=True,
            include_buildin=True,
            plugins_path=self.plugins_path,
            buildin_path=self.buildin_path,
            core_path=self.core_path,
        )
        if found is None:
            candidates: list[tuple[PluginScope, str]] = [
                ("plugin", key),
                ("plugin", f"{key}-dev"),
                ("buildin", key),
                ("buildin", f"{key}-dev"),
            ]
            if key == CORE_PLUGIN_NAME:
                candidates.append(("core", CORE_PLUGIN_NAME))
            removed_keys = [ek for ek in candidates if ek in self._entries]
            if removed_keys:
                updated = dict(self._entries)
                for ek in removed_keys:
                    del updated[ek]
                self._entries = updated
            return None

        target_path, scope = found
        prev = self._entries[(scope, key)] if (scope, key) in self._entries else None
        if prev is None and (scope, f"{key}-dev") in self._entries:
            prev = self._entries[(scope, f"{key}-dev")]
        meta = await asyncio.to_thread(
            _build_meta_from_disk,
            target_path,
            scope=scope,
            catalog=self._catalog,
            existing=prev,
        )
        if with_commit and meta.is_git:
            state = self._get_loop_state()
            meta = await _enrich_single_commit(meta, git_sem=state.git_sem)

        meta = self._attach_cached_probes(meta)
        ek: tuple[PluginScope, str] = (scope, meta.key)
        updated = dict(self._entries)
        updated[ek] = meta
        self._entries = updated
        return meta

    @staticmethod
    def _reclassify_entry(item: LocalPluginMeta, catalog: StoreCatalog) -> LocalPluginMeta:
        if not item.is_git or item.git is None:
            return item
        git = item.git
        rec_mode = "mirror" if git.active_mirror == "custom_mirror" else ""
        classified = classify_git_source(
            git.remote_url,
            recorded_upstream=git.recorded_upstream,
            recorded_mirror_mode=rec_mode,
            recorded_mirror_prefix=git.active_mirror_prefix,
            plugin_name=item.name,
            catalog=catalog,
        )
        new_git = replace(
            git,
            active_mirror=classified.active_mirror,
            active_mirror_prefix=classified.active_mirror_prefix,
            upstream_url=classified.upstream_url,
            upstream_host=classified.upstream_host,
            upstream_provider=classified.upstream_provider,
            upstream_owner=classified.upstream_owner,
            upstream_repo=classified.upstream_repo,
            store_plugin_id=classified.store_plugin_id,
            store_upstream_url=classified.store_upstream_url,
            supported_mirrors=classified.supported_mirrors,
            recorded_upstream=git.recorded_upstream,
        )
        return replace(item, source_kind=classified.source_kind, git=new_git)

    def reclassify_with_store(self, raw_store: Mapping[str, object], *, persist: bool = True) -> None:
        """拿到远程插件商店列表后纯内存重算所有条目的 official/fork 归属，0 阻塞 IO。"""
        links: dict[str, str] = {}
        for plugin_id, info in raw_store.items():
            if not isinstance(plugin_id, str):
                continue
            if isinstance(info, str) and info.strip():
                links[plugin_id] = info.strip()
            elif isinstance(info, dict) and "link" in info and isinstance(info["link"], str):
                link_val = info["link"].strip()
                if link_val:
                    links[plugin_id] = link_val
        if not links:
            return

        self._store_links = links
        self._catalog = build_store_catalog(links)

        if self._entries:
            updated: dict[tuple[PluginScope, str], LocalPluginMeta] = {
                ek: self._reclassify_entry(item, self._catalog) for ek, item in self._entries.items()
            }
            self._entries = updated

        if persist:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                save_cached_store_links(links, self.store_cache_path)
            else:
                state = self._get_loop_state()
                save_task = loop.create_task(asyncio.to_thread(save_cached_store_links, links, self.store_cache_path))
                state.store_save_task = save_task

                def _clear_save_task(done_task: asyncio.Task[None]) -> None:
                    if state.store_save_task is done_task:
                        state.store_save_task = None

                save_task.add_done_callback(_clear_save_task)

    def mark_load_result(
        self,
        name: str,
        status: PluginLoadStatus,
        error: str | None = None,
        *,
        scope: PluginScope | None = None,
    ) -> None:
        """标注插件加载/重载状态；支持同步调用（供 server.py 与 reload_plugin.py 使用）。"""
        key = _normalize_plugin_key(name)
        if not key:
            return
        candidates: list[tuple[PluginScope, str]] = (
            [(scope, key), (scope, f"{key}-dev")]
            if scope is not None
            else [
                ("plugin", key),
                ("plugin", f"{key}-dev"),
                ("buildin", key),
                ("buildin", f"{key}-dev"),
            ]
        )
        target_ek: tuple[PluginScope, str] | None = None
        for cand in candidates:
            if cand in self._entries:
                target_ek = cand
                break
        if target_ek is None:
            return
        prev = self._entries[target_ek]
        updated = dict(self._entries)
        updated[target_ek] = replace(prev, load_status=status, last_error=error, updated_at=time.time())
        self._entries = updated

    def seed_sync(self, entries: Sequence[LocalPluginMeta]) -> None:
        """由同步启动阶段写入初始快照，保留既有条目中更完整的运行期状态。"""
        merged: dict[tuple[PluginScope, str], LocalPluginMeta] = dict(self._entries)
        for item in entries:
            merged[(item.scope, item.key)] = item
        self._entries = merged
        self._initialized = True

    def list_cached(
        self,
        *,
        scope: PluginScope | None = "plugin",
        git_only: bool = False,
        include_core: bool = False,
        include_disabled: bool = False,
    ) -> list[LocalPluginMeta]:
        """无锁读取当前内存快照，实时过滤已过期的 UI 探活记录。"""
        now = time.time()
        out: list[LocalPluginMeta] = []
        for item in self._entries.values():
            if include_core and item.scope == "core":
                pass
            elif scope is not None and item.scope != scope:
                continue
            if not include_disabled and item.disabled:
                continue
            if git_only and not (item.is_git and item.has_remote):
                continue
            out.append(self._filter_entry_probes(item, now=now))
        out.sort(key=lambda m: (0 if m.scope == "core" else 1, m.key))
        return out

    async def list_plugins(
        self,
        *,
        scope: PluginScope | None = "plugin",
        git_only: bool = False,
        include_core: bool = False,
        include_disabled: bool = False,
        fresh: bool = False,
        with_commit: bool = False,
    ) -> list[LocalPluginMeta]:
        """异步查询接口：fresh=True 走 <10ms 纯盘快扫回填缓存；with_commit 按需确保 commit 已加载。"""
        if fresh or not self._initialized:
            await self.refresh_all(with_commit=with_commit)
        elif with_commit and (not self._commits_enriched or self._has_missing_commit_info()):
            await self.ensure_commits_loaded()
        return self.list_cached(
            scope=scope,
            git_only=git_only,
            include_core=include_core,
            include_disabled=include_disabled,
        )

    def get(
        self,
        name: str,
        *,
        include_core: bool = False,
        include_buildin: bool = False,
    ) -> LocalPluginMeta | None:
        """大小写不敏感查表，按 scope 隔离查找，支持 gscore 别名、单文件 .py 后缀与 --dev 回退。"""
        clean = _normalize_plugin_key(name)
        if not clean:
            return None
        item: LocalPluginMeta | None = None
        if include_core and clean == CORE_PLUGIN_NAME and ("core", CORE_PLUGIN_NAME) in self._entries:
            item = self._entries[("core", CORE_PLUGIN_NAME)]
        elif ("plugin", clean) in self._entries:
            item = self._entries[("plugin", clean)]
        elif ("plugin", f"{clean}-dev") in self._entries:
            item = self._entries[("plugin", f"{clean}-dev")]
        elif include_buildin and ("buildin", clean) in self._entries:
            item = self._entries[("buildin", clean)]
        elif include_buildin and ("buildin", f"{clean}-dev") in self._entries:
            item = self._entries[("buildin", f"{clean}-dev")]
        if item is None:
            return None
        return self._filter_entry_probes(item, now=time.time())

    def is_git_plugin(self, name: str) -> bool:
        item = self.get(name, include_core=True, include_buildin=True)
        return item is not None and item.is_git and item.has_remote

    def get_commit(self, name: str) -> str:
        clean = name.strip().lower()
        if clean == "core":
            clean = CORE_PLUGIN_NAME
        item = self.get(clean, include_core=True, include_buildin=True)
        if item is None or item.git is None:
            return ""
        return item.git.short_commit

    def record_probe_result(
        self,
        plugin_key: str,
        result: MirrorProbeResult,
        *,
        scope: PluginScope | None = None,
    ) -> None:
        pk = _normalize_plugin_key(plugin_key)
        if not pk:
            return
        resolved_scope: PluginScope
        if scope is not None:
            resolved_scope = scope
        elif ("plugin", pk) in self._entries:
            resolved_scope = "plugin"
        elif pk == CORE_PLUGIN_NAME and ("core", pk) in self._entries:
            resolved_scope = "core"
        elif ("buildin", pk) in self._entries:
            resolved_scope = "buildin"
        else:
            resolved_scope = "core" if pk == CORE_PLUGIN_NAME else "plugin"

        cache_key: tuple[PluginScope, str, str, str] = (resolved_scope, pk, result.channel, result.target_url)
        self._probe_cache.pop(cache_key, None)
        self._probe_cache[cache_key] = result
        while len(self._probe_cache) > _MAX_PROBE_CACHE_SIZE:
            self._probe_cache.pop(next(iter(self._probe_cache)))
        # 仅核心官方镜像通道挂入 UI 展示快照，避免切源临时探活污染表格胶囊
        if result.channel not in _UI_PROBE_CHANNELS:
            return
        ek: tuple[PluginScope, str] = (resolved_scope, pk)
        if ek in self._entries:
            prev = self._entries[ek]
            if prev.is_git and prev.git is not None:
                updated = dict(self._entries)
                updated[ek] = replace(
                    prev,
                    git=replace(prev.git, mirror_probes=self._entry_probes(resolved_scope, pk)),
                )
                self._entries = updated

    async def probe_target(
        self,
        entry: LocalPluginMeta,
        target_url: str,
        *,
        channel: str,
        mirror_type: ResolvedMirrorMode,
        force: bool = False,
        retry_unreachable: bool = False,
        git_fallback_on_net_error: bool = False,
    ) -> MirrorProbeResult:
        """单目标探活（TTL 缓存 + single-flight 合并，网络 await 期间不持全局锁）。"""
        cache_key: tuple[PluginScope, str, str, str] = (entry.scope, entry.key, channel, target_url)
        now = time.time()
        if not force and cache_key in self._probe_cache:
            cached = self._probe_cache[cache_key]
            if is_probe_fresh(cached, now) and not (retry_unreachable and cached.state == "unreachable"):
                return cached

        state = self._get_loop_state()
        if cache_key in state.probe_tasks and not state.probe_tasks[cache_key].done():
            return await asyncio.shield(state.probe_tasks[cache_key])

        async def _do_probe() -> MirrorProbeResult:
            res = await probe_target_url(
                target_url,
                channel=channel,
                mirror_type=mirror_type,
                http_sem=state.http_sem,
                git_sem=state.git_sem,
                git_fallback_on_net_error=git_fallback_on_net_error,
            )
            self.record_probe_result(entry.key, res, scope=entry.scope)
            return res

        task = asyncio.create_task(_do_probe())
        state.probe_tasks[cache_key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if cache_key in state.probe_tasks and state.probe_tasks[cache_key] is task:
                del state.probe_tasks[cache_key]

    async def probe_mirrors(
        self,
        name: str | None = None,
        *,
        channels: Sequence[MirrorChannel] = ("cnb", "gitcode"),
        force: bool = False,
    ) -> dict[str, tuple[MirrorProbeResult, ...]]:
        """并发探活单个或全部官方插件在指定内置镜像源上的可用性。"""
        if not self._initialized:
            await self.refresh_all(with_commit=False)

        if name is not None:
            single = self.get(name, include_core=True)
            targets = [single] if single is not None else []
        else:
            targets = self.list_cached(scope="plugin", git_only=True, include_core=True)

        prefix_by_channel = {label: prefix for prefix, label in MIRROR_PREFIXES.items()}
        jobs: list[tuple[LocalPluginMeta, str, str]] = []
        for entry in targets:
            if entry.source_kind != "official" or entry.git is None:
                continue
            for ch in channels:
                if ch not in entry.git.supported_mirrors or ch not in prefix_by_channel:
                    continue
                target_url = f"{prefix_by_channel[ch]}{entry.git.upstream_repo}"
                jobs.append((entry, ch, target_url))

        async def _run_job(entry: LocalPluginMeta, ch: str, target_url: str) -> tuple[str, MirrorProbeResult]:
            res = await self.probe_target(entry, target_url, channel=ch, mirror_type="mirror", force=force)
            return entry.name, res

        completed = await asyncio.gather(*(_run_job(e, c, u) for e, c, u in jobs))
        grouped: dict[str, list[MirrorProbeResult]] = {}
        for plugin_name, res in completed:
            bucket = grouped[plugin_name] if plugin_name in grouped else []
            bucket.append(res)
            grouped[plugin_name] = bucket
        return {k: tuple(v) for k, v in grouped.items()}


plugin_registry = LocalPluginRegistry()
