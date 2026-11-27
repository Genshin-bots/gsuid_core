"""插件上游来源识别、严格 (host, owner, repo) 归属、SSRF 护栏与全通道探活。"""

from __future__ import annotations

import re
import time
import socket
import asyncio
import ipaddress
from typing import Literal
from weakref import WeakKeyDictionary
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse
from collections.abc import Mapping

import httpx2

from gsuid_core.utils.path_safety import reject_outbound_http_url
from gsuid_core.utils.plugins_update.git_async import git_ls_remote_head

from .models import (
    MirrorType,
    ProbeState,
    MirrorChannel,
    MirrorDisplay,
    LocalPluginMeta,
    StoreIndexEntry,
    PluginSourceKind,
    UpstreamProvider,
    MirrorProbeResult,
    ResolvedMirrorMode,
    MirrorTargetResolution,
)

GITHUB_HOST = "github.com"
GITHUB_PREFIX = "https://github.com/"
SSH_PREFIX = "ssh://"
SSH_GITHUB_TEMPLATE = "ssh://git@ssh.github.com:443/{owner}/{repo}.git"
CORE_PLUGIN_NAME = "gsuid_core"
CORE_UPSTREAM_URL = "https://github.com/Genshin-bots/gsuid_core"

MIRROR_PREFIXES: dict[str, Literal["gitcode", "cnb"]] = {
    "https://gitcode.com/gscore-mirror/": "gitcode",
    "https://cnb.cool/gscore-mirror/": "cnb",
}
PROXY_PREFIXES: dict[str, Literal["ghproxy"]] = {
    "https://ghproxy.mihomo.me/": "ghproxy",
}
ALL_KNOWN_PREFIXES: dict[str, str] = {**MIRROR_PREFIXES, **PROXY_PREFIXES}

_EMBEDDED_HTTP_RE = re.compile(r"^(https?://.+?)/(https?://.+)$", re.IGNORECASE)
_SCP_GIT_RE = re.compile(r"^(?:(?P<user>[^@\s/]+)@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>.+)$")
_METADATA_IPS = frozenset({"169.254.169.254", "100.100.100.200", "fd00:ec2::254"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_PROBE_REDIRECTS = 2

_shared_probe_client: httpx2.AsyncClient | None = None
_loop_probe_clients: WeakKeyDictionary[asyncio.AbstractEventLoop, httpx2.AsyncClient] = WeakKeyDictionary()


@dataclass(frozen=True)
class StoreCatalog:
    """官方插件商店的双索引视图：严格三元组 + 仓库名索引。"""

    by_identity: dict[tuple[str, str, str], StoreIndexEntry]
    by_repo: dict[str, tuple[StoreIndexEntry, ...]]


@dataclass(frozen=True)
class SourceClassification:
    """对单个仓库远端地址的完整归属与传输通道分类结果。"""

    source_kind: PluginSourceKind
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


@dataclass(frozen=True)
class ParsedIdentity:
    """规范化后的 Git 仓库身份。"""

    host: str
    owner: str
    repo: str
    canonical_url: str


def _strip_single_url_creds(text: str) -> str:
    parsed = urlparse(text)
    if parsed.username is None and parsed.password is None:
        return text
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, ""))


def strip_url_credentials(url: str) -> str:
    """剥离 http(s) URL（含代理链内嵌上游 URL）中的内嵌凭据，防止回显或持久化 token。"""
    text = url.strip()
    if not text.lower().startswith(("http://", "https://")):
        return text
    cleaned = _strip_single_url_creds(text)
    parsed = urlparse(cleaned)
    inner_match = re.search(r"https?://", parsed.path, re.IGNORECASE)
    if inner_match is None:
        return cleaned
    prefix_path = parsed.path[: inner_match.start()]
    inner_raw = parsed.path[inner_match.start() :]
    inner_clean = _strip_single_url_creds(inner_raw)
    if inner_clean == inner_raw:
        return cleaned
    return urlunparse((parsed.scheme, parsed.netloc, f"{prefix_path}{inner_clean}", parsed.params, parsed.query, ""))


def _parse_noncanonical_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """解析十进制整数/八进制/十六进制及简写 IPv4（如 2130706433、0177.0.0.1、127.1、0）。"""
    parts = host.split(".")
    if not (1 <= len(parts) <= 4):
        return None
    nums: list[int] = []
    for part in parts:
        if not part:
            return None
        low = part.lower()
        if low.startswith("0x"):
            digits = low[2:]
            if not digits or not all(c in "0123456789abcdef" for c in digits):
                return None
            val = int(digits, 16)
        elif len(low) > 1 and low.startswith("0"):
            if not all(c in "01234567" for c in low):
                return None
            val = int(low, 8)
        else:
            if not low.isdigit():
                return None
            val = int(low, 10)
        nums.append(val)
    if any(v > 255 for v in nums[:-1]):
        return None
    last_max = (1 << (8 * (5 - len(nums)))) - 1
    if nums[-1] > last_max:
        return None
    addr_int = 0
    for idx, v in enumerate(nums[:-1]):
        addr_int |= v << (24 - idx * 8)
    addr_int |= nums[-1]
    return ipaddress.IPv4Address(addr_int)


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return _parse_noncanonical_ipv4(host)


def _is_forbidden_ip(raw_ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(raw_ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if str(addr) in _METADATA_IPS:
        return True
    return addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast


def validate_mirror_prefix(prefix: str, *, allow_query: bool = False) -> str | None:
    """同步 SSRF 与格式校验：仅允许无凭据的公网或局域网 http(s) 地址。"""
    text = prefix.strip()
    if not text:
        return "镜像地址不能为空"
    err = reject_outbound_http_url(text)
    if err is not None:
        return err
    parsed = urlparse(text)
    if parsed.username is not None or parsed.password is not None:
        return "镜像地址不允许包含内嵌用户名或密码"
    if (not allow_query and parsed.query) or parsed.fragment:
        return "镜像地址不允许包含查询参数或锚点"
    host = (parsed.hostname or "").lower()
    ip = _parse_ip_literal(host)
    if ip is not None and _is_forbidden_ip(str(ip)):
        return "不允许指向回环、链路本地或元数据地址"
    return None


async def validate_mirror_prefix_async(
    prefix: str,
    *,
    allow_query: bool = False,
    fail_closed_on_dns_error: bool = False,
) -> str | None:
    """异步 SSRF 复核：在同步规则基础上校验 DNS 解析出的实际 IP。"""
    sync_err = validate_mirror_prefix(prefix, allow_query=allow_query)
    if sync_err is not None:
        return sync_err
    parsed = urlparse(prefix.strip())
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if _parse_ip_literal(host) is not None:
        return None
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=3.0,
        )
    except (OSError, asyncio.TimeoutError):
        if fail_closed_on_dns_error:
            return f"域名解析失败: {host}"
        return None
    for _family, _type, _proto, _canon, sockaddr in infos:
        raw_ip = str(sockaddr[0])
        if _is_forbidden_ip(raw_ip):
            return f"目标域名解析到了受限地址: {host}"
    return None


def detect_provider(host: str) -> UpstreamProvider:
    hostname = host.lower().split(":", 1)[0].strip("[]")
    if hostname in {"github.com", "ssh.github.com"}:
        return "github"
    if hostname == "cnb.cool":
        return "cnb"
    if hostname == "gitcode.com":
        return "gitcode"
    if hostname == "gitlab.com" or "gitlab" in hostname:
        return "gitlab"
    if "gitea" in hostname or "forgejo" in hostname:
        return "gitea"
    return "other"


def _normalize_host(hostname: str, port: int | None, scheme: str) -> str:
    low = hostname.lower().strip("[]")
    if low == "ssh.github.com":
        return GITHUB_HOST
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else 22
    if port is not None and port != default_port and low != GITHUB_HOST:
        return f"{low}:{port}"
    return low


def parse_repo_identity(url: str) -> ParsedIdentity | None:
    """从任意 Git HTTP(S) / SSH / SCP URL 中解析 (host, owner, repo, canonical_url)。"""
    raw = strip_url_credentials(url.strip())
    if not raw:
        return None

    low = raw.lower()
    if low.startswith(("http://", "https://", "ssh://", "git://")):
        parsed = urlparse(raw)
        if not parsed.hostname:
            return None
        scheme = parsed.scheme.lower()
        host = _normalize_host(parsed.hostname, parsed.port, scheme)
        parts = [seg for seg in parsed.path.strip("/").split("/") if seg]
        if len(parts) < 2:
            return None
        repo = parts[-1][:-4] if parts[-1].lower().endswith(".git") else parts[-1]
        owner = "/".join(parts[:-1])
        if not owner or not repo:
            return None
        if host == GITHUB_HOST:
            canonical = f"https://{GITHUB_HOST}/{owner}/{repo}"
        elif scheme in ("http", "https"):
            canonical = f"{scheme}://{host}/{owner}/{repo}"
        else:
            canonical = raw[:-4] if raw.lower().endswith(".git") else raw
        return ParsedIdentity(host=host, owner=owner, repo=repo, canonical_url=canonical)

    scp = _SCP_GIT_RE.match(raw)
    if scp is not None:
        user = scp.group("user")
        host = scp.group("host").lower()
        # 排除 Windows 盘符路径（如 F:/repos/DemoUID）及无域名无用户的非 SCP 字串
        if user is None and (len(host) <= 1 or "." not in host):
            return None
        if host == "ssh.github.com":
            host = GITHUB_HOST
        path_part = scp.group("path").strip("/")
        parts = [seg for seg in path_part.split("/") if seg]
        if len(parts) < 2:
            return None
        repo = parts[-1][:-4] if parts[-1].lower().endswith(".git") else parts[-1]
        owner = "/".join(parts[:-1])
        if not owner or not repo:
            return None
        if host == GITHUB_HOST:
            canonical = f"https://{GITHUB_HOST}/{owner}/{repo}"
        else:
            canonical = raw[:-4] if raw.lower().endswith(".git") else raw
        return ParsedIdentity(host=host, owner=owner, repo=repo, canonical_url=canonical)

    return None


def build_store_catalog(raw_links: Mapping[str, str]) -> StoreCatalog:
    """构建官方商店严格三元组索引，并内置 gsuid_core 本体条目。"""
    by_identity: dict[tuple[str, str, str], StoreIndexEntry] = {}
    by_repo_lists: dict[str, list[StoreIndexEntry]] = {}

    def _add(plugin_id: str, link: str) -> None:
        ident = parse_repo_identity(link)
        if ident is None:
            return
        entry = StoreIndexEntry(
            plugin_id=plugin_id,
            link=ident.canonical_url,
            host=ident.host,
            owner=ident.owner,
            repo=ident.repo,
        )
        key = (ident.host.lower(), ident.owner.lower(), ident.repo.lower())
        by_identity[key] = entry
        repo_key = ident.repo.lower()
        bucket = by_repo_lists[repo_key] if repo_key in by_repo_lists else []
        if not any(existing.link.lower() == entry.link.lower() for existing in bucket):
            bucket.append(entry)
        by_repo_lists[repo_key] = bucket

    _add(CORE_PLUGIN_NAME, CORE_UPSTREAM_URL)
    for plugin_id, link in raw_links.items():
        if isinstance(plugin_id, str) and isinstance(link, str) and link.strip():
            _add(plugin_id, link.strip())

    by_repo: dict[str, tuple[StoreIndexEntry, ...]] = {k: tuple(v) for k, v in by_repo_lists.items()}
    return StoreCatalog(by_identity=by_identity, by_repo=by_repo)


def _match_builtin_mirror_prefix(url: str) -> tuple[str, Literal["gitcode", "cnb"]] | None:
    low = url.lower()
    for prefix, label in MIRROR_PREFIXES.items():
        if low.startswith(prefix.lower()):
            return prefix, label
    return None


def _match_builtin_proxy_prefix(url: str) -> tuple[str, Literal["ghproxy"]] | None:
    low = url.lower()
    for prefix, label in PROXY_PREFIXES.items():
        if low.startswith(prefix.lower()):
            return prefix, label
    return None


def is_builtin_mirror_or_proxy(prefix: str) -> bool:
    """判断前缀是否属于内置镜像源或内置反代前缀。"""
    clean = prefix.strip()
    if not clean:
        return False
    norm = clean.rstrip("/") + "/"
    return _match_builtin_mirror_prefix(norm) is not None or _match_builtin_proxy_prefix(norm) is not None


def _extract_embedded_proxy(url: str) -> tuple[str, str, Literal["ghproxy", "custom_proxy"]] | None:
    builtin = _match_builtin_proxy_prefix(url)
    if builtin is not None:
        prefix, label = builtin
        inner = url[len(prefix) :].lstrip("/")
        if inner.lower().startswith(("http://", "https://")):
            return prefix, inner, label
    matched = _EMBEDDED_HTTP_RE.match(url)
    if matched is not None:
        prefix = matched.group(1).rstrip("/") + "/"
        inner = matched.group(2)
        return prefix, inner, "custom_proxy"
    return None


def _extract_flat_repo_name(url: str, prefix: str) -> str:
    rest = url[len(prefix) :].strip("/")
    if rest.lower().endswith(".git"):
        rest = rest[:-4]
    parts = [p for p in rest.split("/") if p]
    return parts[-1] if parts else ""


def _supported_mirrors_for(source_kind: PluginSourceKind, upstream_host: str) -> tuple[MirrorChannel, ...]:
    if source_kind in ("local", "git_local_only"):
        return ()
    if upstream_host.lower() != GITHUB_HOST:
        return ()
    if source_kind == "official":
        return ("github", "gitcode", "cnb", "ghproxy", "ssh")
    return ("github", "ghproxy", "ssh")


def classify_git_source(
    remote_url: str,
    *,
    recorded_upstream: str = "",
    recorded_mirror_mode: str = "",
    recorded_mirror_prefix: str = "",
    plugin_name: str = "",
    catalog: StoreCatalog | None = None,
) -> SourceClassification:
    """按严格 (host, owner, repo) 规则识别来源、传输通道与支持镜像集合。"""
    clean_remote = strip_url_credentials(remote_url.strip())
    if not clean_remote:
        return SourceClassification(
            source_kind="git_local_only",
            remote_url="",
            active_mirror="unknown",
            active_mirror_prefix="",
            upstream_url="",
            upstream_host="",
            upstream_provider="other",
            upstream_owner="",
            upstream_repo="",
            store_plugin_id=None,
            store_upstream_url=None,
            supported_mirrors=(),
        )

    store = catalog if catalog is not None else build_store_catalog({})
    active_mirror: MirrorDisplay = "unknown"
    active_prefix = ""
    candidate_upstream = ""
    flat_repo_hint = ""

    # 1. 识别当前 origin 所属传输形态
    builtin_mirror = _match_builtin_mirror_prefix(clean_remote)
    embedded_proxy = _extract_embedded_proxy(clean_remote)
    norm_rec_prefix = recorded_mirror_prefix.strip().rstrip("/") + "/" if recorded_mirror_prefix.strip() else ""

    if builtin_mirror is not None:
        active_prefix, active_mirror = builtin_mirror
        flat_repo_hint = _extract_flat_repo_name(clean_remote, active_prefix)
    elif (
        recorded_mirror_mode.strip().lower() == "mirror"
        and norm_rec_prefix
        and clean_remote.lower().startswith(norm_rec_prefix.lower())
    ):
        active_mirror = "custom_mirror"
        active_prefix = norm_rec_prefix
        flat_repo_hint = _extract_flat_repo_name(clean_remote, norm_rec_prefix)
    elif embedded_proxy is not None:
        active_prefix, candidate_upstream, active_mirror = embedded_proxy
    else:
        candidate_upstream = clean_remote
        direct_ident = parse_repo_identity(clean_remote)
        if direct_ident is not None:
            if direct_ident.host == GITHUB_HOST:
                is_ssh = clean_remote.lower().startswith("ssh://") or clean_remote.startswith("git@")
                active_mirror = "ssh" if is_ssh else "github"
            else:
                active_mirror = "native"

    # 2. 按无损优先还原真实上游身份：直连/反代直接以 origin 为准，扁平镜像才回查记录或商店
    upstream_ident: ParsedIdentity | None = None
    if candidate_upstream:
        upstream_ident = parse_repo_identity(candidate_upstream)
    else:
        if recorded_upstream.strip():
            upstream_ident = parse_repo_identity(recorded_upstream.strip())
        if upstream_ident is None and flat_repo_hint:
            matches = store.by_repo[flat_repo_hint.lower()] if flat_repo_hint.lower() in store.by_repo else ()
            if len(matches) == 1:
                match = matches[0]
                upstream_ident = ParsedIdentity(
                    host=match.host,
                    owner=match.owner,
                    repo=match.repo,
                    canonical_url=match.link,
                )

    if upstream_ident is None:
        repo_fallback = flat_repo_hint or plugin_name
        # 存量内置扁平镜像在商店索引未就绪时保留 official 与官方镜像互切能力，避免锁死
        if builtin_mirror is not None and flat_repo_hint:
            return SourceClassification(
                source_kind="official",
                remote_url=clean_remote,
                active_mirror=active_mirror,
                active_mirror_prefix=active_prefix,
                upstream_url="",
                upstream_host="",
                upstream_provider="github",
                upstream_owner="",
                upstream_repo=flat_repo_hint,
                store_plugin_id=None,
                store_upstream_url=None,
                supported_mirrors=("gitcode", "cnb"),
            )
        return SourceClassification(
            source_kind="third_party",
            remote_url=clean_remote,
            active_mirror=active_mirror,
            active_mirror_prefix=active_prefix,
            upstream_url="",
            upstream_host="",
            upstream_provider="other",
            upstream_owner="",
            upstream_repo=repo_fallback,
            store_plugin_id=None,
            store_upstream_url=None,
            supported_mirrors=(),
        )

    # 3. 与官方商店严格三元组比对：仅 (host, owner, repo) 全一致才算 official
    id_key = (upstream_ident.host.lower(), upstream_ident.owner.lower(), upstream_ident.repo.lower())
    store_plugin_id: str | None = None
    store_upstream_url: str | None = None
    source_kind: PluginSourceKind = "third_party"

    if id_key in store.by_identity:
        matched_entry = store.by_identity[id_key]
        source_kind = "official"
        store_plugin_id = matched_entry.plugin_id
        store_upstream_url = matched_entry.link
    else:
        repo_keys = [upstream_ident.repo.lower()]
        if plugin_name and plugin_name.lower() not in repo_keys:
            repo_keys.append(plugin_name.lower())
        for rk in repo_keys:
            if rk in store.by_repo and store.by_repo[rk]:
                ref = store.by_repo[rk][0]
                source_kind = "fork"
                store_plugin_id = ref.plugin_id
                store_upstream_url = ref.link
                break

    provider = detect_provider(upstream_ident.host)
    supported = _supported_mirrors_for(source_kind, upstream_ident.host)
    return SourceClassification(
        source_kind=source_kind,
        remote_url=clean_remote,
        active_mirror=active_mirror,
        active_mirror_prefix=active_prefix,
        upstream_url=upstream_ident.canonical_url,
        upstream_host=upstream_ident.host,
        upstream_provider=provider,
        upstream_owner=upstream_ident.owner,
        upstream_repo=upstream_ident.repo,
        store_plugin_id=store_plugin_id,
        store_upstream_url=store_upstream_url,
        supported_mirrors=supported,
    )


def resolve_target_mirror_mode(target_prefix: str, mirror_type: MirrorType = "auto") -> ResolvedMirrorMode:
    """解析目标前缀对应的传输模式；内置前缀优先识别，自定义地址再看 mirror_type（默认 proxy）。"""
    clean = target_prefix.strip()
    if not clean or mirror_type == "default":
        return "default"
    if clean.lower().startswith(SSH_PREFIX) or mirror_type == "ssh":
        return "ssh"
    norm = clean.rstrip("/") + "/"
    if _match_builtin_mirror_prefix(norm) is not None:
        return "mirror"
    if _match_builtin_proxy_prefix(norm) is not None:
        return "proxy"
    if mirror_type == "mirror":
        return "mirror"
    if mirror_type == "proxy":
        return "proxy"
    return "proxy"


def normalize_mirror_config_pair(
    prefix: str,
    mirror_type: MirrorType = "auto",
) -> tuple[str, Literal["auto", "proxy", "mirror"]]:
    """规范化落盘的 (GitMirror, GitMirrorMode)，防止 default/ssh 夹带脏前缀或内置源残留旧模式。"""
    clean = prefix.strip()
    if not clean or mirror_type == "default":
        return "", "auto"
    if clean.lower().startswith(SSH_PREFIX) or mirror_type == "ssh":
        return SSH_PREFIX, "auto"
    norm = clean.rstrip("/") + "/"
    if _match_builtin_mirror_prefix(norm) is not None or _match_builtin_proxy_prefix(norm) is not None:
        return norm, "auto"
    if mirror_type == "mirror":
        return norm, "mirror"
    if mirror_type == "proxy":
        return norm, "proxy"
    return norm, "auto"


def resolve_safe_mirror_target(
    entry: LocalPluginMeta,
    target_prefix: str,
    *,
    mirror_type: MirrorType = "auto",
) -> MirrorTargetResolution:
    """按严格归属规则构造目标镜像 URL，明确区分不可切源（ineligible）与非法输入（invalid）。"""
    mode = resolve_target_mirror_mode(target_prefix, mirror_type)
    git = entry.git
    if not entry.is_git or not entry.has_remote or git is None:
        return MirrorTargetResolution(
            status="ineligible",
            target_url=None,
            mode=mode,
            channel="unknown",
            reason=f"{entry.name}: 非有效的 Git 仓库或无 origin 远端",
        )

    clean_prefix = target_prefix.strip()
    norm_prefix = clean_prefix.rstrip("/") + "/" if clean_prefix and mode in ("mirror", "proxy") else ""

    if mode in ("mirror", "proxy"):
        ssrf_err = validate_mirror_prefix(norm_prefix)
        if ssrf_err is not None:
            return MirrorTargetResolution(
                status="invalid",
                target_url=None,
                mode=mode,
                channel="custom",
                reason=f"{entry.name}: 镜像地址无效 - {ssrf_err}",
            )

    is_unresolved_builtin_mirror = (
        entry.source_kind == "official"
        and git.active_mirror in ("cnb", "gitcode")
        and not git.upstream_url
        and bool(git.upstream_repo)
    )
    if is_unresolved_builtin_mirror:
        if mode == "mirror":
            builtin_mirror = _match_builtin_mirror_prefix(norm_prefix)
            channel = builtin_mirror[1] if builtin_mirror is not None else "custom_mirror"
            return MirrorTargetResolution(
                status="ok",
                target_url=f"{norm_prefix}{git.upstream_repo}",
                mode="mirror",
                channel=channel,
            )
        return MirrorTargetResolution(
            status="invalid",
            target_url=None,
            mode=mode,
            channel="unknown",
            reason=f"{entry.name}: 尚未获取插件商店索引，暂无法还原 GitHub 上游地址，请联网后刷新重试",
        )

    if git.upstream_host.lower() != GITHUB_HOST:
        host_label = git.upstream_host or "非 GitHub"
        return MirrorTargetResolution(
            status="ineligible",
            target_url=None,
            mode=mode,
            channel="native",
            reason=f"{entry.name}: 托管在原生源 ({host_label})，不支持切换 GitHub 镜像或代理",
        )

    if not git.upstream_owner or not git.upstream_repo:
        return MirrorTargetResolution(
            status="invalid",
            target_url=None,
            mode=mode,
            channel="unknown",
            reason=f"{entry.name}: 无法识别上游仓库的作者与仓库名",
        )

    if mode == "default":
        return MirrorTargetResolution(status="ok", target_url=git.upstream_url, mode="default", channel="github")

    if mode == "ssh":
        ssh_url = SSH_GITHUB_TEMPLATE.format(owner=git.upstream_owner, repo=git.upstream_repo)
        return MirrorTargetResolution(status="ok", target_url=ssh_url, mode="ssh", channel="ssh")

    if mode == "proxy":
        builtin_proxy = _match_builtin_proxy_prefix(norm_prefix)
        channel = builtin_proxy[1] if builtin_proxy is not None else "custom_proxy"
        return MirrorTargetResolution(
            status="ok",
            target_url=f"{norm_prefix}{git.upstream_url}.git",
            mode="proxy",
            channel=channel,
        )

    # mode == "mirror"（官方扁平镜像或自建扁平镜像）：严格仅 official 允许
    if entry.source_kind == "fork":
        return MirrorTargetResolution(
            status="ineligible",
            target_url=None,
            mode="mirror",
            channel="mirror",
            reason=(
                f"{entry.name}: Fork 仓库 ({git.upstream_owner}/{git.upstream_repo}) "
                "不允许切换至扁平镜像源，请改用反代或 SSH"
            ),
        )
    if entry.source_kind != "official":
        return MirrorTargetResolution(
            status="ineligible",
            target_url=None,
            mode="mirror",
            channel="mirror",
            reason=f"{entry.name}: 非官方商店插件不允许切换至扁平镜像源，请改用反代或 SSH",
        )
    builtin_mirror = _match_builtin_mirror_prefix(norm_prefix)
    channel = builtin_mirror[1] if builtin_mirror is not None else "custom_mirror"
    return MirrorTargetResolution(
        status="ok",
        target_url=f"{norm_prefix}{git.upstream_repo}",
        mode="mirror",
        channel=channel,
    )


def build_safe_mirror_url(
    entry: LocalPluginMeta,
    target_prefix: str,
    *,
    mirror_type: MirrorType = "auto",
) -> tuple[str | None, ResolvedMirrorMode, str, str]:
    """兼容封装：返回 (target_url, mode, channel, error_msg)。"""
    res = resolve_safe_mirror_target(entry, target_prefix, mirror_type=mirror_type)
    return res.target_url, res.mode, res.channel, res.reason


def _get_probe_client() -> httpx2.AsyncClient:
    if _shared_probe_client is not None:
        return _shared_probe_client
    loop = asyncio.get_running_loop()
    if loop not in _loop_probe_clients:
        _loop_probe_clients[loop] = httpx2.AsyncClient(
            timeout=httpx2.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0),
            follow_redirects=False,
        )
    return _loop_probe_clients[loop]


async def close_probe_client() -> None:
    """进程或当前事件循环关闭时释放共享探活客户端。"""
    global _shared_probe_client
    client = _shared_probe_client
    _shared_probe_client = None
    if client is not None:
        await client.aclose()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop_client = _loop_probe_clients.pop(loop, None)
    if loop_client is not None:
        await loop_client.aclose()


def _build_smart_http_probe_url(target_url: str) -> str:
    base = target_url.strip().rstrip("/")
    if not base.lower().endswith(".git"):
        base = f"{base}.git"
    return f"{base}/info/refs?service=git-upload-pack"


def _looks_like_git_upload_pack(content_type: str, body_prefix: bytes) -> bool:
    if "x-git-upload-pack-advertisement" in content_type.lower():
        return True
    return b"service=git-upload-pack" in body_prefix


async def probe_target_url(
    target_url: str,
    *,
    channel: str,
    mirror_type: ResolvedMirrorMode,
    http_sem: asyncio.Semaphore | None = None,
    git_sem: asyncio.Semaphore | None = None,
    git_fallback_on_net_error: bool = False,
) -> MirrorProbeResult:
    """全通道可拉取性预检：HTTP 走 Smart HTTP（401/纯网络异常可补 ls-remote），SSH 走 ls-remote。"""
    now = time.time()
    if mirror_type == "ssh":
        if git_sem is not None:
            async with git_sem:
                ok, err = await git_ls_remote_head(target_url, timeout=6)
        else:
            ok, err = await git_ls_remote_head(target_url, timeout=6)
        state: ProbeState = "available" if ok else "unreachable"
        return MirrorProbeResult(channel=channel, target_url=target_url, state=state, checked_at=now, detail=err)

    sync_ssrf_err = validate_mirror_prefix(target_url)
    if sync_ssrf_err is not None:
        return MirrorProbeResult(
            channel=channel,
            target_url=target_url,
            state="unreachable",
            checked_at=now,
            detail=sync_ssrf_err,
        )

    probe_url = _build_smart_http_probe_url(target_url)
    status_code = 0
    content_type = ""
    body_prefix = b""
    net_error = ""
    http_transport_error = False

    async def _do_http() -> None:
        nonlocal status_code, content_type, body_prefix, net_error, http_transport_error, probe_url
        client = _get_probe_client()
        current_url = probe_url
        for hop in range(_MAX_PROBE_REDIRECTS + 1):
            hop_err = await validate_mirror_prefix_async(
                current_url,
                allow_query=True,
                fail_closed_on_dns_error=True,
            )
            if hop_err is not None:
                net_error = hop_err
                # 仅首跳纯 DNS 不可达时标记为传输异常，重定向或命中受限 IP 绝不降级 git 补测
                if hop == 0 and hop_err.startswith("域名解析失败:"):
                    http_transport_error = True
                return
            try:
                async with client.stream(
                    "GET",
                    current_url,
                    headers={"User-Agent": "git/gsuid_core"},
                ) as resp:
                    status_code = resp.status_code
                    if status_code in _REDIRECT_STATUSES:
                        location = resp.headers["location"] if "location" in resp.headers else ""
                        if not location:
                            net_error = f"HTTP {status_code} 缺少跳转地址"
                            return
                        current_url = urljoin(current_url, location)
                        continue
                    content_type = resp.headers["content-type"] if "content-type" in resp.headers else ""
                    chunks: list[bytes] = []
                    read_len = 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        read_len += len(chunk)
                        if read_len >= 512:
                            break
                    body_prefix = b"".join(chunks)[:512]
                    return
            except httpx2.HTTPError as exc:
                net_error = str(exc)[:180]
                http_transport_error = True
                return
        net_error = "重定向次数过多"

    if http_sem is not None:
        async with http_sem:
            await _do_http()
    else:
        await _do_http()

    if net_error:
        # 仅纯 HTTP 传输失败且调用方允许时用 git ls-remote 补测，兼容仅配置 git http.proxy 的环境
        if http_transport_error and git_fallback_on_net_error:
            if git_sem is not None:
                async with git_sem:
                    ok, _git_err = await git_ls_remote_head(target_url, timeout=5)
            else:
                ok, _git_err = await git_ls_remote_head(target_url, timeout=5)
            if ok:
                return MirrorProbeResult(channel=channel, target_url=target_url, state="available", checked_at=now)
        return MirrorProbeResult(
            channel=channel,
            target_url=target_url,
            state="unreachable",
            checked_at=now,
            detail=net_error,
        )

    if status_code == 200:
        if _looks_like_git_upload_pack(content_type, body_prefix):
            return MirrorProbeResult(channel=channel, target_url=target_url, state="available", checked_at=now)
        return MirrorProbeResult(
            channel=channel,
            target_url=target_url,
            state="unreachable",
            checked_at=now,
            detail="目标地址返回的不是有效的 Git Smart HTTP 响应",
        )

    if status_code in (403, 404):
        return MirrorProbeResult(
            channel=channel,
            target_url=target_url,
            state="not_synced",
            checked_at=now,
            detail=f"目标仓库未同步或不存在 (HTTP {status_code})",
        )

    if status_code == 401:
        # 内置官方公开镜像返回 401 表示未同步；其余通道补测 git ls-remote 兼容本机凭据私有仓
        if channel in ("cnb", "gitcode"):
            return MirrorProbeResult(
                channel=channel,
                target_url=target_url,
                state="not_synced",
                checked_at=now,
                detail="镜像源尚未同步该仓库 (HTTP 401)",
            )
        if git_sem is not None:
            async with git_sem:
                ok, err = await git_ls_remote_head(target_url, timeout=5)
        else:
            ok, err = await git_ls_remote_head(target_url, timeout=5)
        if ok:
            return MirrorProbeResult(channel=channel, target_url=target_url, state="available", checked_at=now)
        return MirrorProbeResult(
            channel=channel,
            target_url=target_url,
            state="not_synced",
            checked_at=now,
            detail=err or "目标仓库需要认证且当前环境无法拉取 (HTTP 401)",
        )

    return MirrorProbeResult(
        channel=channel,
        target_url=target_url,
        state="unreachable",
        checked_at=now,
        detail=f"探测返回异常状态码 HTTP {status_code}",
    )
