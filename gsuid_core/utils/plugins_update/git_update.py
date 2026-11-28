"""
Git Update 工具模块

提供异步的 git 版本管理功能，支持：
- 获取远程 commit 列表
- 获取当前 commit 信息
- 回退到指定版本
- 强制更新（git reset --hard + git pull）

所有 git 操作均通过 git_async 模块异步执行，避免阻塞事件循环。
"""

import asyncio
from typing import List, Optional, TypedDict
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.plugin_registry import (
    CORE_PLUGIN_NAME,
    LocalPluginMeta,
    plugin_registry,
    probe_plugin_disk,
    resolve_plugin_path_sync,
)

from .api import CORE_PATH, PLUGINS_PATH
from .git_async import run_git, git_pull, git_fetch, git_reset_hard, git_get_current_branch


class CommitInfo(TypedDict):
    """Commit 信息"""

    hash: str
    short_hash: str
    author: str
    date: str
    message: str


class GitStatusInfo(TypedDict):
    """Git 仓库状态信息"""

    name: str
    path: str
    current_commit: CommitInfo
    is_git_repo: bool
    branch: str
    remote_url: str
    mirror: str
    source_kind: str
    upstream_host: str
    upstream_url: str


def _parse_commit_line(line: str) -> Optional[CommitInfo]:
    """
    解析 git log 格式的 commit 行。

    格式: hash|author|date|message

    Args:
        line: git log 输出行

    Returns:
        CommitInfo 字典，解析失败返回 None
    """
    parts = line.split("|", 3)
    if len(parts) < 4:
        return None

    hash_val, author, date, message = parts
    return CommitInfo(
        hash=hash_val.strip(),
        short_hash=hash_val.strip()[:7],
        author=author.strip(),
        date=date.strip(),
        message=message.strip(),
    )


async def get_current_commit(repo_path: Path) -> Optional[CommitInfo]:
    """
    获取仓库当前 HEAD 的 commit 信息。

    Args:
        repo_path: 仓库路径

    Returns:
        CommitInfo 字典，失败返回 None
    """
    if not (repo_path / ".git").exists():
        return None

    returncode, stdout, stderr = await run_git(
        repo_path,
        "log",
        "-1",
        "--format=%H|%an|%ai|%s",
    )

    if returncode != 0 or not stdout:
        logger.warning(t("log.plugin.git_update_get_commit_stderr", stderr=stderr))
        return None

    return _parse_commit_line(stdout)


async def get_current_branch(repo_path: Path) -> str:
    """
    获取仓库当前分支名称。

    在 detached HEAD 状态下，尝试获取默认分支名（main/master）。

    Args:
        repo_path: 仓库路径

    Returns:
        分支名称
    """
    return await git_get_current_branch(repo_path)


async def get_remote_commits(
    repo_path: Path,
    max_count: int = 50,
) -> List[CommitInfo]:
    """
    获取远程仓库的 commit 列表。

    先尝试 git fetch，如果失败（如认证问题）则使用本地缓存的 origin ref。
    然后获取 origin/{branch} 的 commit 历史。

    Args:
        repo_path: 仓库路径
        max_count: 最大返回数量

    Returns:
        CommitInfo 列表
    """
    if not (repo_path / ".git").exists():
        return []

    # 尝试 fetch 获取最新远程信息，失败则使用本地缓存
    success, message = await git_fetch(repo_path)
    if not success:
        logger.warning(t("log.plugin.git_update_fetch_use_locally", message=message))

    # 获取当前分支
    branch = await git_get_current_branch(repo_path)

    # 获取远程 commit 列表
    returncode, stdout, stderr = await run_git(
        repo_path,
        "log",
        f"origin/{branch}",
        f"-{max_count}",
        "--format=%H|%an|%ai|%s",
    )

    if returncode != 0 or not stdout:
        logger.warning(t("log.plugin.git_update_get_remote_commit_fail", stderr=stderr))
        return []

    commits: List[CommitInfo] = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        commit = _parse_commit_line(line)
        if commit:
            commits.append(commit)

    return commits


async def get_local_commits(
    repo_path: Path,
    max_count: int = 50,
) -> List[CommitInfo]:
    """
    获取本地仓库的 commit 历史。

    Args:
        repo_path: 仓库路径
        max_count: 最大返回数量

    Returns:
        CommitInfo 列表
    """
    if not (repo_path / ".git").exists():
        return []

    returncode, stdout, stderr = await run_git(
        repo_path,
        "log",
        f"-{max_count}",
        "--format=%H|%an|%ai|%s",
    )

    if returncode != 0 or not stdout:
        logger.warning(t("log.plugin.git_update_get_local_commit_list", stderr=stderr))
        return []

    commits: List[CommitInfo] = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        commit = _parse_commit_line(line)
        if commit:
            commits.append(commit)

    return commits


def _repo_plugin_name(repo_path: Path) -> str:
    return CORE_PLUGIN_NAME if repo_path.resolve() == CORE_PATH.resolve() else repo_path.name


async def get_git_status(repo_path: Path) -> Optional[GitStatusInfo]:
    """
    获取仓库的完整状态信息。

    Args:
        repo_path: 仓库路径

    Returns:
        GitStatusInfo 字典，失败返回 None
    """
    if not (repo_path / ".git").exists():
        return None

    current_commit = await get_current_commit(repo_path)
    if not current_commit:
        return None

    branch = await git_get_current_branch(repo_path)
    name = await asyncio.to_thread(_repo_plugin_name, repo_path)
    cached = plugin_registry.get(name, include_core=True, include_buildin=True)
    if cached is None:
        scope = "core" if name == CORE_PLUGIN_NAME else "plugin"
        cached = await probe_plugin_disk(repo_path, scope=scope, with_commit=False)
    git = cached.git if cached is not None else None

    return GitStatusInfo(
        name=name,
        path=str(repo_path),
        current_commit=current_commit,
        is_git_repo=True,
        branch=branch,
        remote_url=git.remote_url if git is not None else "",
        mirror=git.active_mirror if git is not None else "unknown",
        source_kind=cached.source_kind if cached is not None else "third_party",
        upstream_host=git.upstream_host if git is not None else "",
        upstream_url=git.upstream_url if git is not None else "",
    )


async def checkout_commit(repo_path: Path, commit_hash: str) -> tuple[bool, str]:
    """
    回退到指定 commit。

    执行 git reset --hard {commit_hash}，将仓库切换到指定版本。
    使用 reset --hard 而非 checkout，避免进入 detached HEAD 状态，
    也不会触发 git 凭证请求。

    Args:
        repo_path: 仓库路径
        commit_hash: 目标 commit hash（支持短 hash）

    Returns:
        (success, message)
    """
    if not (repo_path / ".git").exists():
        return False, "不是有效的 git 仓库"

    # 验证 commit hash 是否存在
    returncode, _, stderr = await run_git(
        repo_path,
        "cat-file",
        "-t",
        commit_hash,
    )

    if returncode != 0:
        return False, f"无效的 commit hash: {commit_hash}"

    # 执行 reset --hard
    success, msg = await git_reset_hard(repo_path, commit_hash)

    if not success:
        logger.warning(t("log.plugin.git_update_reset_hard_msg", msg=msg))
        return False, f"reset --hard 失败: {msg}"

    logger.info(t("log.plugin.git_update_rolled_commit_hash", commit_hash=commit_hash))
    refresh_name = await asyncio.to_thread(_repo_plugin_name, repo_path)
    await plugin_registry.refresh_one(refresh_name, with_commit=True)
    return True, f"已回退到 commit: {commit_hash[:7]}"


async def force_update(repo_path: Path) -> tuple[bool, str]:
    """
    强制更新仓库。

    执行 git reset --hard origin/{branch}，然后 git pull。

    Args:
        repo_path: 仓库路径

    Returns:
        (success, message)
    """
    if not (repo_path / ".git").exists():
        return False, "不是有效的 git 仓库"

    # 获取当前分支
    branch = await git_get_current_branch(repo_path)
    if branch == "unknown":
        return False, "无法获取当前分支信息"

    # 先 fetch
    success, message = await git_fetch(repo_path)
    if not success:
        return False, f"git fetch 失败: {message}"

    # git reset --hard origin/{branch}
    success, message = await git_reset_hard(repo_path, f"origin/{branch}")
    if not success:
        logger.warning(t("log.plugin.git_update_reset_hard", message=message))
        return False, f"git reset --hard 失败: {message}"

    # git pull
    success, message = await git_pull(repo_path)
    if not success:
        logger.warning(t("log.plugin.git_update_pull", message=message))
        return False, f"git pull 失败: {message}"

    # 获取更新后的 commit 信息
    current_commit = await get_current_commit(repo_path)
    if current_commit:
        message = f"强制更新成功，当前版本: {current_commit['short_hash']}"
    else:
        message = "强制更新成功"

    logger.info(t("log.git_update.update_result", message=message))
    refresh_name = await asyncio.to_thread(_repo_plugin_name, repo_path)
    await plugin_registry.refresh_one(refresh_name, with_commit=True)
    return True, message


async def update(repo_path: Path) -> tuple[bool, str]:
    """
    普通更新仓库。

    仅执行 git pull，适用于本地无冲突的正常更新场景。
    如果 git pull 失败（如网络问题、凭证问题），会返回错误信息。

    Args:
        repo_path: 仓库路径

    Returns:
        (success, message)
    """
    if not (repo_path / ".git").exists():
        return False, "不是有效的 git 仓库"

    # 先尝试 fetch 获取最新远程信息
    success, message = await git_fetch(repo_path)
    if not success:
        return False, f"git fetch 失败: {message}"

    # 执行 git pull
    success, message = await git_pull(repo_path)
    if not success:
        logger.warning(t("log.plugin.git_update_pull", message=message))
        return False, f"git pull 失败: {message}"

    # 获取更新后的 commit 信息
    current_commit = await get_current_commit(repo_path)
    if current_commit:
        message = f"更新成功，当前版本: {current_commit['short_hash']}"
    else:
        message = "更新成功"

    logger.info(t("log.git_update.update_result", message=message))
    refresh_name = await asyncio.to_thread(_repo_plugin_name, repo_path)
    await plugin_registry.refresh_one(refresh_name, with_commit=True)
    return True, message


def _meta_to_status(meta: LocalPluginMeta) -> GitStatusInfo | None:
    git = meta.git
    if not meta.is_git or not meta.has_remote or git is None or not git.commit_hash:
        return None
    return GitStatusInfo(
        name=meta.name,
        path=str(meta.path),
        current_commit=CommitInfo(
            hash=git.commit_hash,
            short_hash=git.short_commit,
            author=git.commit_author,
            date=git.commit_date,
            message=git.commit_message,
        ),
        is_git_repo=True,
        branch=git.branch or "main",
        remote_url=git.remote_url,
        mirror=git.active_mirror,
        source_kind=meta.source_kind,
        upstream_host=git.upstream_host,
        upstream_url=git.upstream_url,
    )


async def get_all_plugins_status(*, fresh: bool = False) -> List[GitStatusInfo]:
    """获取所有含 origin 的 Git 插件（含 core 本体）状态，默认复用元信息中心缓存。"""
    entries = await plugin_registry.list_plugins(
        scope="plugin",
        git_only=True,
        include_core=True,
        fresh=fresh,
        with_commit=True,
    )
    result: List[GitStatusInfo] = []
    for entry in entries:
        status = _meta_to_status(entry)
        if status is not None:
            result.append(status)
    return result


def _resolve_plugin_path(plugin_name: str) -> Optional[Path]:
    """解析插件名称到实际路径（统一走安全路径围栏）。"""
    found = resolve_plugin_path_sync(
        plugin_name,
        include_core=True,
        include_buildin=False,
        plugins_path=PLUGINS_PATH,
        buildin_path=plugin_registry.buildin_path,
        core_path=CORE_PATH,
    )
    return found[0] if found is not None else None
