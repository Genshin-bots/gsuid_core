"""本地插件元信息中心的数据模型（全不可变、零 Any）。"""

from __future__ import annotations

from typing import Literal, TypedDict
from pathlib import Path
from dataclasses import dataclass

PluginScope = Literal["plugin", "buildin", "core"]
PluginLayout = Literal["nested", "full", "package", "single", "unknown"]
PluginLoadStatus = Literal["loaded", "unloaded", "failed", "disabled"]
PluginSourceKind = Literal["local", "git_local_only", "official", "fork", "third_party"]
UpstreamProvider = Literal["github", "cnb", "gitcode", "gitlab", "gitea", "other"]
MirrorChannel = Literal["github", "gitcode", "cnb", "ghproxy", "ssh"]
MirrorType = Literal["auto", "default", "mirror", "proxy", "ssh"]
ResolvedMirrorMode = Literal["default", "mirror", "proxy", "ssh"]
MirrorDisplay = Literal[
    "github",
    "gitcode",
    "cnb",
    "ghproxy",
    "custom_proxy",
    "custom_mirror",
    "ssh",
    "native",
    "unknown",
]
ProbeState = Literal["unknown", "available", "not_synced", "unreachable"]
MirrorSwitchStatus = Literal["switched", "unchanged", "skipped", "failed"]
MirrorTargetStatus = Literal["ok", "ineligible", "invalid"]


@dataclass(frozen=True)
class PluginKindMeta:
    """发现期读到的基础设施插件元数据（早于 import，不能靠 Plugins()）。"""

    kind: Literal["meta"]
    provides: str
    plugin_name: str


@dataclass(frozen=True)
class StoreIndexEntry:
    """官方插件商店单条规范化上游索引。"""

    plugin_id: str
    link: str
    host: str
    owner: str
    repo: str


@dataclass(frozen=True)
class MirrorProbeResult:
    """单个镜像或目标 URL 的可用性探测结果。"""

    channel: str
    target_url: str
    state: ProbeState
    checked_at: float
    detail: str = ""


@dataclass(frozen=True)
class PluginGitMeta:
    """仅当 is_git=True 时填充的 Git 运行时与上游归属元信息。"""

    commit_hash: str
    short_commit: str
    commit_author: str
    commit_date: str
    commit_message: str
    branch: str
    remote_url: str
    active_mirror: MirrorDisplay
    active_mirror_prefix: str
    upstream_url: str
    upstream_host: str
    upstream_provider: UpstreamProvider
    upstream_owner: str
    upstream_repo: str
    store_plugin_id: str | None
    store_upstream_url: str | None
    supported_mirrors: tuple[MirrorChannel, ...]
    mirror_probes: tuple[MirrorProbeResult, ...] = ()
    recorded_upstream: str = ""


@dataclass(frozen=True)
class LocalPluginMeta:
    """单个本地插件（或 core 本体）的统一只读元信息快照。"""

    name: str
    key: str
    path: Path
    scope: PluginScope
    layout: PluginLayout
    source_kind: PluginSourceKind
    is_git: bool
    has_remote: bool
    git: PluginGitMeta | None
    is_meta: bool
    meta_provides: str | None
    has_pyproject: bool
    icon_path: Path | None
    load_status: PluginLoadStatus
    last_error: str | None
    updated_at: float
    disabled: bool = False


@dataclass(frozen=True)
class MirrorSwitchItem:
    """批量切换镜像时的单插件执行结果。"""

    name: str
    success: bool
    status: MirrorSwitchStatus
    message: str


@dataclass(frozen=True)
class MirrorTargetResolution:
    """构造目标镜像 URL 的结构化结果，明确区分不可切源与非法输入。"""

    status: MirrorTargetStatus
    target_url: str | None
    mode: ResolvedMirrorMode
    channel: str
    reason: str = ""


PROBE_OK_TTL_SECONDS = 15 * 60.0
PROBE_NOT_SYNCED_TTL_SECONDS = 90.0
PROBE_FAIL_TTL_SECONDS = 30.0


def probe_ttl(state: ProbeState) -> float:
    if state == "unreachable":
        return PROBE_FAIL_TTL_SECONDS
    if state == "not_synced":
        return PROBE_NOT_SYNCED_TTL_SECONDS
    return PROBE_OK_TTL_SECONDS


class MirrorProbeDict(TypedDict):
    channel: str
    target_url: str
    state: ProbeState
    checked_at: float
    expires_at: float
    detail: str


class LocalPluginDiskViewDict(TypedDict):
    name: str
    id: str
    scope: PluginScope
    layout: PluginLayout
    is_git: bool
    has_remote: bool
    source_kind: PluginSourceKind
    upstream_host: str
    upstream_url: str
    commit: str
    is_meta: bool
    meta_provides: str | None
    has_icon: bool
    disabled: bool
    load_status: PluginLoadStatus
    last_error: str | None


def probe_to_dict(probe: MirrorProbeResult) -> MirrorProbeDict:
    return {
        "channel": probe.channel,
        "target_url": probe.target_url,
        "state": probe.state,
        "checked_at": probe.checked_at,
        "expires_at": probe.checked_at + probe_ttl(probe.state),
        "detail": probe.detail,
    }


def meta_to_disk_view(meta: LocalPluginMeta) -> LocalPluginDiskViewDict:
    git = meta.git
    return {
        "name": meta.name,
        "id": meta.key,
        "scope": meta.scope,
        "layout": meta.layout,
        "is_git": meta.is_git,
        "has_remote": meta.has_remote,
        "source_kind": meta.source_kind,
        "upstream_host": git.upstream_host if git is not None else "",
        "upstream_url": git.upstream_url if git is not None else "",
        "commit": git.short_commit if git is not None else "",
        "is_meta": meta.is_meta,
        "meta_provides": meta.meta_provides,
        "has_icon": meta.icon_path is not None,
        "disabled": meta.disabled,
        "load_status": meta.load_status,
        "last_error": meta.last_error,
    }
