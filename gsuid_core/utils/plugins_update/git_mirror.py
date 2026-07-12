"""
Git Mirror 工具模块

提供异步的 git remote URL 管理功能，支持三种模式：
1. 镜像模式 (gitcode/cnb): {mirror_prefix}/{repo_name}
2. 代理前缀模式 (ghproxy): {proxy_prefix}{full_github_url}
3. SSH 模式: ssh://git@ssh.github.com:443/{owner}/{repo}.git

支持：
- 查看所有插件的 git remote URL
- 将插件的 git remote URL 切换到指定镜像源/代理/SSH
- 批量替换所有插件的 git remote URL
"""

from typing import Dict, List, Optional, TypedDict
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

from .api import CORE_PATH, PLUGINS_PATH
from .git_async import git_get_remote_url, git_set_remote_url

# GitHub 原始地址前缀
GITHUB_PREFIX = "https://github.com/"

# SSH 模式特殊标记
SSH_PREFIX = "ssh://"

# SSH GitHub 地址模板
SSH_GITHUB_TEMPLATE = "ssh://git@ssh.github.com:443/{owner}/{repo}.git"

# 已知的镜像源前缀（替换模式）：{mirror_prefix}/{repo_name}
MIRROR_PREFIXES: Dict[str, str] = {
    "https://gitcode.com/gscore-mirror/": "gitcode",
    "https://cnb.cool/gscore-mirror/": "cnb",
}

# 已知的代理前缀（拼接模式）：{proxy_prefix}{full_github_url}
PROXY_PREFIXES: Dict[str, str] = {
    "https://ghproxy.mihomo.me/": "ghproxy",
}

# 所有已知前缀合并（不含 SSH，SSH 是特殊模式）
ALL_KNOWN_PREFIXES: Dict[str, str] = {**MIRROR_PREFIXES, **PROXY_PREFIXES}


class PluginGitInfo(TypedDict):
    name: str
    path: str
    remote_url: str
    is_git_repo: bool
    mirror: str
    commit: str


async def get_remote_url(repo_path: Path) -> Optional[str]:
    """
    获取指定仓库的 origin remote URL。

    Args:
        repo_path: 仓库路径

    Returns:
        remote URL 字符串，如果不是 git 仓库则返回 None
    """
    return await git_get_remote_url(repo_path)


def _is_proxy_prefix(prefix: str) -> bool:
    """
    判断给定前缀是否是代理前缀模式（拼接模式）。

    Args:
        prefix: 镜像/代理前缀

    Returns:
        是否是代理前缀
    """
    clean = prefix.rstrip("/")
    for known in PROXY_PREFIXES:
        if clean == known.rstrip("/"):
            return True
    return False


def _is_ssh_mode(prefix: str) -> bool:
    """
    判断给定前缀是否是 SSH 模式。

    Args:
        prefix: 镜像/代理前缀

    Returns:
        是否是 SSH 模式
    """
    return prefix == SSH_PREFIX


def _is_ssh_url(url: str) -> bool:
    """
    判断 URL 是否是 SSH 格式。

    Args:
        url: git remote URL

    Returns:
        是否是 SSH URL
    """
    return url.startswith("ssh://") or url.startswith("git@")


def detect_mirror(url: str) -> str:
    """
    检测 URL 对应的镜像源/代理/协议名称。

    Args:
        url: git remote URL

    Returns:
        镜像源名称，如 "gitcode"、"cnb"、"ghproxy"、"ssh"、"github"、"unknown"
    """
    if _is_ssh_url(url):
        return "ssh"
    for prefix, name in ALL_KNOWN_PREFIXES.items():
        if url.startswith(prefix):
            return name
    if url.startswith(GITHUB_PREFIX):
        return "github"
    return "unknown"


def _extract_owner_repo(url: str) -> Optional[tuple[str, str]]:
    """
    从 git remote URL 中提取 owner 和 repo 名称。

    支持 HTTPS 和 SSH 格式：
    - HTTPS: https://github.com/{owner}/{repo}(.git)
    - SSH: ssh://git@ssh.github.com:443/{owner}/{repo}(.git)
    - SCP: git@github.com:{owner}/{repo}(.git)

    Args:
        url: git remote URL

    Returns:
        (owner, repo) 元组，如 ("Genshin-bots", "GenshinUID")，无法提取则返回 None
    """
    clean = url.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]

    # SSH URL: ssh://git@ssh.github.com:443/{owner}/{repo}
    if clean.startswith("ssh://"):
        # ssh://git@ssh.github.com:443/owner/repo
        parts = clean.split("/")
        # 找到 host 后的部分
        # ssh:, "", git@ssh.github.com:443, owner, repo
        if len(parts) >= 5:
            return parts[-2], parts[-1]
        return None

    # SCP 格式: git@github.com:owner/repo
    if clean.startswith("git@"):
        colon_parts = clean.split(":")
        if len(colon_parts) == 2:
            path_parts = colon_parts[1].split("/")
            if len(path_parts) >= 2:
                return path_parts[-2], path_parts[-1]
        return None

    # HTTPS 格式: https://github.com/owner/repo
    # 也可能包含代理前缀，需要先提取 GitHub URL
    github_url = _extract_github_url(url)
    if github_url:
        clean_gh = github_url.rstrip("/")
        if clean_gh.endswith(".git"):
            clean_gh = clean_gh[:-4]
        parts = clean_gh.split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]

    return None


def _extract_repo_name(url: str) -> Optional[str]:
    """
    从 git remote URL 中提取仓库名（最后一段路径，去掉 .git 后缀）。

    Args:
        url: git remote URL

    Returns:
        仓库名，如 "GenshinUID"
    """
    owner_repo = _extract_owner_repo(url)
    if owner_repo:
        return owner_repo[1]

    # fallback: 取最后一段
    clean = url.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    parts = clean.split("/")
    if parts:
        return parts[-1]
    return None


def _extract_github_url(url: str) -> Optional[str]:
    """
    从可能包含代理前缀或 SSH 格式的 URL 中提取原始 GitHub HTTPS URL。

    Args:
        url: 可能包含代理前缀或 SSH 格式的 URL

    Returns:
        原始 GitHub HTTPS URL，如果无法提取则返回 None
    """
    # 如果已经是 GitHub HTTPS URL，直接返回
    if url.startswith(GITHUB_PREFIX):
        return url

    # 尝试从代理前缀中提取
    for prefix in PROXY_PREFIXES:
        if url.startswith(prefix):
            original = url[len(prefix) :]
            if original.startswith(GITHUB_PREFIX):
                return original

    # 尝试从 SSH URL 中提取
    if _is_ssh_url(url):
        owner_repo = _extract_owner_repo(url)
        if owner_repo:
            owner, repo = owner_repo
            return f"{GITHUB_PREFIX}{owner}/{repo}"

    return None


def build_mirror_url(original_url: str, prefix: str) -> Optional[str]:
    """
    根据原始 URL 和前缀构建目标 URL。

    支持三种模式：
    - 镜像模式 (gitcode/cnb): {prefix}/{repo_name}
    - 代理前缀模式 (ghproxy): {prefix}{full_github_url}
    - SSH 模式: ssh://git@ssh.github.com:443/{owner}/{repo}.git

    注意：对于代理模式，如果原始 URL 是镜像 URL（如 cnb/gitcode），
    则需要通过 _extract_owner_repo 从 URL 中解析出正确的 owner/repo。

    Args:
        original_url: 原始 git remote URL
        prefix: 镜像源/代理前缀，或 "ssh://" 表示 SSH 模式

    Returns:
        目标 URL，如果无法构建则返回 None
    """
    if _is_ssh_mode(prefix):
        # SSH 模式：需要 owner 和 repo
        owner_repo = _extract_owner_repo(original_url)
        if not owner_repo:
            return None
        owner, repo = owner_repo
        return SSH_GITHUB_TEMPLATE.format(owner=owner, repo=repo)

    clean_prefix = prefix.rstrip("/") + "/"

    if _is_proxy_prefix(prefix):
        # 代理前缀模式：需要完整的 GitHub URL
        github_url = _extract_github_url(original_url)
        if not github_url:
            # 如果无法提取 GitHub URL（如当前 URL 是镜像 URL），
            # 则从 URL 中提取 owner/repo 来构造 GitHub URL
            owner_repo = _extract_owner_repo(original_url)
            if not owner_repo:
                repo_name = _extract_repo_name(original_url)
                if not repo_name:
                    return None
                # 无法确定 owner，使用 gscore-mirror 作为默认值
                github_url = f"{GITHUB_PREFIX}gscore-mirror/{repo_name}"
            else:
                owner, repo = owner_repo
                github_url = f"{GITHUB_PREFIX}{owner}/{repo}"
        return f"{clean_prefix}{github_url}"
    else:
        # 镜像模式：只需要仓库名
        repo_name = _extract_repo_name(original_url)
        if not repo_name:
            return None
        return f"{clean_prefix}{repo_name}"


async def get_plugin_git_info(plugin_path: Path) -> PluginGitInfo:
    """
    获取单个插件的 git 信息。

    Args:
        plugin_path: 插件目录路径

    Returns:
        PluginGitInfo 字典
    """
    from gsuid_core.utils.plugins_update._plugins import plugin_commit_versions

    name = plugin_path.name
    remote_url = await get_remote_url(plugin_path)
    is_git = remote_url is not None

    # 使用启动时获取的 commit 版本（运行版本）
    commit = plugin_commit_versions.get(name.lower(), "")

    return PluginGitInfo(
        name=name,
        path=str(plugin_path),
        remote_url=remote_url or "",
        is_git_repo=is_git,
        mirror=detect_mirror(remote_url) if remote_url else "unknown",
        commit=commit,
    )


async def get_all_plugins_git_info() -> List[PluginGitInfo]:
    """
    获取所有插件（包括 core 本体）的 git remote URL 信息。

    Returns:
        PluginGitInfo 列表
    """
    result: List[PluginGitInfo] = []

    # core 本体
    result.append(await get_plugin_git_info(CORE_PATH))

    # 所有插件
    if PLUGINS_PATH.exists():
        for plugin_dir in sorted(PLUGINS_PATH.iterdir()):
            if plugin_dir.is_dir() and plugin_dir.name != "__pycache__":
                result.append(await get_plugin_git_info(plugin_dir))

    return result


# 已知的原始 GitHub URL 映射（用于不在插件商店中的仓库）
_KNOWN_GITHUB_URLS: Dict[str, str] = {
    "gsuid_core": "https://github.com/Genshin-bots/gsuid_core",
}


async def _get_original_github_url(plugin_name: str) -> Optional[str]:
    """
    获取插件的原始 GitHub URL。

    优先从已知映射中查找，其次从插件商店数据中获取。

    Args:
        plugin_name: 插件名称

    Returns:
        原始 GitHub URL，如 "https://github.com/Genshin-bots/GenshinUID"，找不到则返回 None
    """
    # 优先从已知映射中查找
    if plugin_name in _KNOWN_GITHUB_URLS:
        return _KNOWN_GITHUB_URLS[plugin_name]

    # 从插件商店数据中获取
    from ._plugins import get_plugins_url

    url_info = await get_plugins_url(plugin_name)
    if url_info and "link" in url_info:
        link = url_info["link"]
        if link.startswith(GITHUB_PREFIX):
            return link
    return None


async def set_plugin_mirror(
    plugin_path: Path,
    prefix: str,
) -> tuple[bool, str]:
    """
    将单个插件的 git remote URL 切换到指定镜像源/代理/SSH。

    核心原则：始终使用插件商城中的原始 GitHub URL（作者/repo）来构建目标 URL，
    而不是依赖当前存储的 URL。这样可以确保：
    1. 无论当前 URL 是什么格式（镜像/代理），都能正确切换
    2. 镜像模式使用正确的 {prefix}/{repo_name} 格式
    3. 代理模式使用正确的 {prefix}{full_github_url} 格式

    Args:
        plugin_path: 插件目录路径
        prefix: 镜像源/代理前缀，"ssh://" 表示 SSH 模式，空字符串表示恢复为 GitHub 原始地址

    Returns:
        (success, message)
    """
    plugin_name = plugin_path.name

    # 检查是否是 git 仓库
    current_url = await get_remote_url(plugin_path)
    if current_url is None:
        return False, f"{plugin_name}: 非 git 仓库或无 origin remote"

    # 始终从插件商城获取原始 GitHub URL（这是正确的作者/repo 来源）
    original_github = await _get_original_github_url(plugin_name)

    if not prefix:
        # 恢复为 GitHub 原始地址
        if original_github:
            new_url = original_github
        else:
            # 回退：从当前 URL 提取仓库名，使用 gscore-mirror 组织
            repo_name = _extract_repo_name(current_url)
            if not repo_name:
                return False, f"{plugin_name}: 无法从当前 URL 提取仓库名"

            if plugin_path == CORE_PATH:
                repo_name = "gsuid_core"

            new_url = f"{GITHUB_PREFIX}gscore-mirror/{repo_name}"
    else:
        # 关键修复：始终使用插件商城的原始 GitHub URL 作为基础
        # 而不是当前 URL（可能是镜像 URL 或错误的 URL）
        # 这样确保无论用户之前如何切换，都能正确构建目标 URL
        base_url = original_github if original_github else current_url
        new_url = build_mirror_url(base_url, prefix)
        if not new_url:
            return False, f"{plugin_name}: 无法构建目标 URL"

    # 检查是否已经是目标地址
    if current_url == new_url:
        return True, f"{plugin_name}: 已是目标地址，无需修改"

    # 执行 git remote set-url
    success, msg = await git_set_remote_url(plugin_path, new_url)
    if not success:
        logger.error(t("[Git镜像] 设置 {plugin_name} remote URL 失败: {msg}", plugin_name=plugin_name, msg=msg))
        return False, f"{plugin_name}: 设置失败 - {msg}"

    logger.info(
        t(
            "[Git镜像] {plugin_name}: {current_url} -> {new_url}",
            plugin_name=plugin_name,
            current_url=current_url,
            new_url=new_url,
        )
    )
    return True, f"{plugin_name}: {current_url} -> {new_url}"


async def set_all_plugins_mirror(
    prefix: str,
) -> List[tuple[str, bool, str]]:
    """
    批量将所有插件（包括 core 本体）的 git remote URL 切换到指定镜像源/代理/SSH。

    Args:
        prefix: 镜像源/代理前缀，"ssh://" 表示 SSH 模式，空字符串表示恢复为 GitHub 原始地址

    Returns:
        [(plugin_name, success, message), ...]
    """
    results: List[tuple[str, bool, str]] = []

    # core 本体
    success, msg = await set_plugin_mirror(CORE_PATH, prefix)
    results.append(("gsuid_core", success, msg))

    # 所有插件
    if PLUGINS_PATH.exists():
        for plugin_dir in sorted(PLUGINS_PATH.iterdir()):
            if plugin_dir.is_dir() and plugin_dir.name != "__pycache__":
                success, msg = await set_plugin_mirror(plugin_dir, prefix)
                results.append((plugin_dir.name, success, msg))

    return results


def get_current_mirror_config() -> str:
    """
    获取当前配置的镜像源前缀。

    Returns:
        镜像源前缀字符串，空字符串表示未配置
    """
    return core_plugins_config.get_config("GitMirror").data


def get_available_mirrors() -> List[Dict[str, str]]:
    """
    获取所有可用的镜像源/代理/SSH 选项。

    Returns:
        [{"label": "显示名", "value": "前缀", "type": "mirror|proxy|ssh|default"}, ...]
    """
    return [
        {"label": "GitHub (默认)", "value": "", "type": "default"},
        {"label": "GitCode 镜像", "value": "https://gitcode.com/gscore-mirror/", "type": "mirror"},
        {"label": "CNB 镜像", "value": "https://cnb.cool/gscore-mirror/", "type": "mirror"},
        {"label": "ghproxy 代理", "value": "https://ghproxy.mihomo.me/", "type": "proxy"},
        {"label": "GitHub SSH", "value": "ssh://", "type": "ssh"},
    ]
