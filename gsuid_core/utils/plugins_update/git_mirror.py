"""Git Mirror 业务动作层：委托 plugin_registry 做严格归属判定、SSRF 护栏与切前强制探活。"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, TypedDict
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.plugin_registry import (
    SSH_PREFIX,
    GITHUB_HOST,
    GITHUB_PREFIX,
    PROXY_PREFIXES,
    MIRROR_PREFIXES,
    CORE_PLUGIN_NAME,
    ALL_KNOWN_PREFIXES,
    SSH_GITHUB_TEMPLATE,
    MirrorType,
    LocalPluginMeta,
    MirrorProbeDict,
    MirrorSwitchItem,
    PluginSourceKind,
    UpstreamProvider,
    probe_to_dict,
    plugin_registry,
    probe_plugin_disk,
    classify_git_source,
    parse_repo_identity,
    validate_mirror_prefix,
    resolve_safe_mirror_target,
    resolve_target_mirror_mode,
)
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

from .api import CORE_PATH
from .git_async import (
    git_get_remote_url,
    git_set_remote_url,
    git_set_local_config,
    git_unset_local_config,
)

__all__ = [
    "ALL_KNOWN_PREFIXES",
    "GITHUB_HOST",
    "GITHUB_PREFIX",
    "MIRROR_PREFIXES",
    "PROXY_PREFIXES",
    "SSH_GITHUB_TEMPLATE",
    "SSH_PREFIX",
    "PluginGitInfo",
    "build_mirror_url",
    "detect_mirror",
    "get_all_plugins_git_info",
    "get_available_mirrors",
    "get_current_mirror_config",
    "get_current_mirror_mode",
    "get_plugin_git_info",
    "get_remote_url",
    "set_all_plugins_mirror",
    "set_all_plugins_mirror_detailed",
    "set_plugin_mirror",
    "set_plugin_mirror_detail",
]


class PluginGitInfo(TypedDict):
    name: str
    path: str
    remote_url: str
    is_git_repo: bool
    mirror: str
    commit: str
    source_kind: PluginSourceKind
    upstream_url: str
    upstream_host: str
    upstream_provider: UpstreamProvider
    active_mirror_prefix: str
    store_upstream_url: str | None
    supported_mirrors: list[str]
    mirror_probes: list[MirrorProbeDict]


async def get_remote_url(repo_path: Path) -> Optional[str]:
    return await git_get_remote_url(repo_path)


def detect_mirror(url: str) -> str:
    classified = classify_git_source(url, catalog=plugin_registry.catalog)
    return classified.active_mirror


def _extract_owner_repo(url: str) -> Optional[tuple[str, str]]:
    classified = classify_git_source(url, catalog=plugin_registry.catalog)
    if classified.upstream_owner and classified.upstream_repo:
        return classified.upstream_owner, classified.upstream_repo
    return None


def _extract_repo_name(url: str) -> Optional[str]:
    ident = parse_repo_identity(url)
    if ident is not None:
        return ident.repo
    clean = url.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    parts = [p for p in clean.split("/") if p]
    return parts[-1] if parts else None


def build_mirror_url(original_url: str, prefix: str, *, mirror_type: MirrorType = "auto") -> Optional[str]:
    """兼容辅助函数：按源归属安全构造目标地址，不合法时返回 None。"""
    classified = classify_git_source(original_url, catalog=plugin_registry.catalog)
    if classified.upstream_host.lower() != GITHUB_HOST or not classified.upstream_owner or not classified.upstream_repo:
        return None
    mode = resolve_target_mirror_mode(prefix, mirror_type)
    if mode == "default":
        return classified.upstream_url
    if mode == "ssh":
        return SSH_GITHUB_TEMPLATE.format(owner=classified.upstream_owner, repo=classified.upstream_repo)
    clean_prefix = prefix.strip().rstrip("/") + "/"
    if validate_mirror_prefix(clean_prefix) is not None:
        return None
    if mode == "proxy":
        return f"{clean_prefix}{classified.upstream_url}.git"
    if classified.source_kind != "official":
        return None
    return f"{clean_prefix}{classified.upstream_repo}"


def _meta_to_git_info(meta: LocalPluginMeta) -> PluginGitInfo:
    git = meta.git
    return PluginGitInfo(
        name=meta.name,
        path=str(meta.path),
        remote_url=git.remote_url if git is not None else "",
        is_git_repo=meta.is_git and meta.has_remote,
        mirror=git.active_mirror if git is not None else "unknown",
        commit=git.short_commit if git is not None else "",
        source_kind=meta.source_kind,
        upstream_url=git.upstream_url if git is not None else "",
        upstream_host=git.upstream_host if git is not None else "",
        upstream_provider=git.upstream_provider if git is not None else "other",
        active_mirror_prefix=git.active_mirror_prefix if git is not None else "",
        store_upstream_url=git.store_upstream_url if git is not None else None,
        supported_mirrors=list(git.supported_mirrors) if git is not None else [],
        mirror_probes=[probe_to_dict(p) for p in git.mirror_probes] if git is not None else [],
    )


def _resolve_repo_pair(plugin_path: Path) -> tuple[Path, bool]:
    resolved = plugin_path.resolve()
    return resolved, resolved == CORE_PATH.resolve()


async def get_plugin_git_info(plugin_path: Path) -> PluginGitInfo:
    resolved, is_core = await asyncio.to_thread(_resolve_repo_pair, plugin_path)
    name = CORE_PLUGIN_NAME if is_core else plugin_path.name
    cached = plugin_registry.get(name, include_core=True, include_buildin=True)
    if cached is not None and cached.path == resolved:
        return _meta_to_git_info(cached)
    probed = await probe_plugin_disk(plugin_path, scope="core" if is_core else "plugin", with_commit=False)
    if probed is not None:
        return _meta_to_git_info(probed)
    return PluginGitInfo(
        name=name,
        path=str(plugin_path),
        remote_url="",
        is_git_repo=False,
        mirror="unknown",
        commit="",
        source_kind="local",
        upstream_url="",
        upstream_host="",
        upstream_provider="other",
        active_mirror_prefix="",
        store_upstream_url=None,
        supported_mirrors=[],
        mirror_probes=[],
    )


async def get_all_plugins_git_info(*, fresh: bool = False, probe: bool = False) -> List[PluginGitInfo]:
    """仅返回 core 本体及具备 origin 远端的 Git 插件；过滤无 .git 与本地无远端仓库。"""
    entries = await plugin_registry.list_plugins(
        scope="plugin",
        git_only=True,
        include_core=True,
        fresh=fresh,
        with_commit=False,
    )
    # 存量 CNB/GitCode 仓首次冷启动无缓存时按需补拉商店索引，避免误判或无法还原 GitHub 上游
    if not plugin_registry.has_store_catalog and plugin_registry.has_unresolved_builtin_mirror():
        if await plugin_registry.ensure_store_catalog_loaded(timeout=4.0):
            entries = plugin_registry.list_cached(scope="plugin", git_only=True, include_core=True)
    if probe:
        await plugin_registry.probe_mirrors(force=fresh)
        entries = plugin_registry.list_cached(scope="plugin", git_only=True, include_core=True)
    return [_meta_to_git_info(item) for item in entries]


def _normalize_url_compare(url: str) -> str:
    clean = url.strip().rstrip("/")
    return clean[:-4] if clean.lower().endswith(".git") else clean


async def _persist_git_provenance(
    repo_path: Path,
    *,
    upstream_url: str,
    mirror_mode: str,
    mirror_prefix: str,
) -> None:
    if upstream_url:
        await git_set_local_config(repo_path, "gscore.upstreamUrl", upstream_url)
    await git_set_local_config(repo_path, "gscore.mirrorMode", mirror_mode)
    if mirror_prefix:
        await git_set_local_config(repo_path, "gscore.mirrorPrefix", mirror_prefix)
    else:
        await git_unset_local_config(repo_path, "gscore.mirrorPrefix")


async def _resolve_entry_for_switch(plugin_path: Path) -> tuple[str, LocalPluginMeta | None]:
    resolved, is_core = await asyncio.to_thread(_resolve_repo_pair, plugin_path)
    name = CORE_PLUGIN_NAME if is_core else plugin_path.name
    refreshed = await plugin_registry.refresh_one(name, with_commit=False)
    if refreshed is not None and refreshed.path == resolved:
        return name, refreshed
    probed = await probe_plugin_disk(plugin_path, scope="core" if is_core else "plugin", with_commit=False)
    return name, probed


async def set_plugin_mirror_detail(
    plugin_path: Path,
    prefix: str,
    *,
    mirror_type: MirrorType = "auto",
    allow_skip: bool = False,
    force_probe: bool = True,
    preloaded_entry: LocalPluginMeta | None = None,
) -> MirrorSwitchItem:
    """安全切换单个仓库的镜像源：校验归属 + 强制可拉取预检 + 持久化真实上游。"""
    if preloaded_entry is not None:
        name, entry = preloaded_entry.name, preloaded_entry
    else:
        if not plugin_registry.has_store_catalog:
            await plugin_registry.ensure_store_catalog_loaded(timeout=4.0)
        name, entry = await _resolve_entry_for_switch(plugin_path)
    if entry is None or not entry.is_git or not entry.has_remote or entry.git is None:
        return MirrorSwitchItem(
            name=name,
            success=allow_skip,
            status="skipped" if allow_skip else "failed",
            message=f"{name}: 非 git 仓库或无 origin remote",
        )

    resolution = resolve_safe_mirror_target(
        entry,
        prefix,
        mirror_type=mirror_type,
    )
    if resolution.status != "ok" or resolution.target_url is None:
        if allow_skip and resolution.status == "ineligible":
            return MirrorSwitchItem(name=entry.name, success=True, status="skipped", message=resolution.reason)
        return MirrorSwitchItem(name=entry.name, success=False, status="failed", message=resolution.reason)

    target_url = resolution.target_url
    resolved_mode = resolution.mode
    channel = resolution.channel
    norm_prefix = prefix.strip().rstrip("/") + "/" if prefix.strip() and resolved_mode in ("mirror", "proxy") else ""
    current_url = entry.git.remote_url
    if _normalize_url_compare(current_url) == _normalize_url_compare(target_url):
        await _persist_git_provenance(
            entry.path,
            upstream_url=entry.git.upstream_url,
            mirror_mode=resolved_mode,
            mirror_prefix=norm_prefix,
        )
        await plugin_registry.refresh_one(entry.name, with_commit=False)
        return MirrorSwitchItem(
            name=entry.name, success=True, status="unchanged", message=f"{entry.name}: 已是目标地址，无需修改"
        )

    # 全通道切前强制探活：必须确认可 pull 才改写 origin（网络层失败允许 git ls-remote 补测）
    probe_res = await plugin_registry.probe_target(
        entry,
        target_url,
        channel=channel,
        mirror_type=resolved_mode,
        force=force_probe,
        retry_unreachable=True,
        git_fallback_on_net_error=True,
    )
    if probe_res.state != "available":
        reason = probe_res.detail or (
            "目标镜像尚未同步该仓库" if probe_res.state == "not_synced" else "目标地址当前不可访问"
        )
        logger.warning(
            t(
                "log.plugin.git_mirror_probe_blocked",
                plugin_name=entry.name,
                target_url=target_url,
                reason=reason,
            )
        )
        return MirrorSwitchItem(
            name=entry.name,
            success=False,
            status="failed",
            message=f"{entry.name}: 预检未通过（{reason}），已保留原源",
        )

    ok, msg = await git_set_remote_url(entry.path, target_url)
    if not ok:
        logger.error(t("log.plugin.git_mirror_set_remote_url", plugin_name=entry.name, msg=msg))
        return MirrorSwitchItem(
            name=entry.name, success=False, status="failed", message=f"{entry.name}: 设置失败 - {msg}"
        )
    await _persist_git_provenance(
        entry.path,
        upstream_url=entry.git.upstream_url,
        mirror_mode=resolved_mode,
        mirror_prefix=norm_prefix,
    )

    logger.info(
        t(
            "log.plugin.git_mirror_name_url",
            plugin_name=entry.name,
            current_url=current_url,
            new_url=target_url,
        )
    )
    await plugin_registry.refresh_one(entry.name, with_commit=False)
    return MirrorSwitchItem(
        name=entry.name,
        success=True,
        status="switched",
        message=f"{entry.name}: {current_url} -> {target_url}",
    )


async def set_plugin_mirror(
    plugin_path: Path,
    prefix: str,
    *,
    mirror_type: MirrorType = "auto",
) -> tuple[bool, str]:
    item = await set_plugin_mirror_detail(plugin_path, prefix, mirror_type=mirror_type, allow_skip=False)
    return item.success, item.message


async def set_all_plugins_mirror_detailed(
    prefix: str,
    *,
    mirror_type: MirrorType = "auto",
) -> List[MirrorSwitchItem]:
    """批量切换所有含 origin 的 Git 插件镜像：自动跳过不适用源，待切项并发探活后再改源。"""
    if not plugin_registry.has_store_catalog:
        await plugin_registry.ensure_store_catalog_loaded(timeout=4.0)
    entries = await plugin_registry.list_plugins(
        scope="plugin",
        git_only=True,
        include_core=True,
        fresh=True,
        with_commit=False,
    )
    write_sem = asyncio.Semaphore(4)

    async def _switch_one(entry: LocalPluginMeta) -> MirrorSwitchItem:
        async with write_sem:
            return await set_plugin_mirror_detail(
                entry.path,
                prefix,
                mirror_type=mirror_type,
                allow_skip=True,
                force_probe=False,
                preloaded_entry=entry,
            )

    return list(await asyncio.gather(*(_switch_one(entry) for entry in entries)))


async def set_all_plugins_mirror(
    prefix: str,
    *,
    mirror_type: MirrorType = "auto",
) -> List[tuple[str, bool, str]]:
    items = await set_all_plugins_mirror_detailed(prefix, mirror_type=mirror_type)
    return [(item.name, item.success, item.message) for item in items]


def get_current_mirror_config() -> str:
    return core_plugins_config.get_config("GitMirror").data


def get_current_mirror_mode() -> MirrorType:
    raw = core_plugins_config.get_config("GitMirrorMode").data
    if raw in ("auto", "default", "mirror", "proxy", "ssh"):
        return raw
    return "auto"


def get_available_mirrors() -> List[Dict[str, str]]:
    return [
        {"label": "GitHub (默认)", "value": "", "type": "default", "channel": "github"},
        {
            "label": "GitCode 镜像",
            "value": "https://gitcode.com/gscore-mirror/",
            "type": "mirror",
            "channel": "gitcode",
        },
        {"label": "CNB 镜像", "value": "https://cnb.cool/gscore-mirror/", "type": "mirror", "channel": "cnb"},
        {"label": "ghproxy 代理", "value": "https://ghproxy.mihomo.me/", "type": "proxy", "channel": "ghproxy"},
        {"label": "GitHub SSH", "value": "ssh://", "type": "ssh", "channel": "ssh"},
    ]
