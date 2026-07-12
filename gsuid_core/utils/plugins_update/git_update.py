"""
Git Update 工具模块

提供异步的 git 版本管理功能，支持：
- 获取远程 commit 列表
- 获取当前 commit 信息
- 回退到指定版本
- 强制更新（git reset --hard + git pull）

所有 git 操作均通过 git_async 模块异步执行，避免阻塞事件循环。
"""

from typing import List, Optional, TypedDict
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger

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
        logger.warning(t("[Git Update] 获取当前 commit 失败: {stderr}", stderr=stderr))
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
        logger.warning(t("[Git Update] git fetch 失败（将使用本地缓存的远程 ref）: {message}", message=message))

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
        logger.warning(t("[Git Update] 获取远程 commit 列表失败: {stderr}", stderr=stderr))
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
        logger.warning(t("[Git Update] 获取本地 commit 列表失败: {stderr}", stderr=stderr))
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

    return GitStatusInfo(
        name=repo_path.name,
        path=str(repo_path),
        current_commit=current_commit,
        is_git_repo=True,
        branch=branch,
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
        logger.warning(t("[Git Update] reset --hard 失败: {msg}", msg=msg))
        return False, f"reset --hard 失败: {msg}"

    logger.info(t("[Git Update] 已回退到 commit: {commit_hash}", commit_hash=commit_hash))
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
        logger.warning(t("[Git Update] git reset --hard 失败: {message}", message=message))
        return False, f"git reset --hard 失败: {message}"

    # git pull
    success, message = await git_pull(repo_path)
    if not success:
        logger.warning(t("[Git Update] git pull 失败: {message}", message=message))
        return False, f"git pull 失败: {message}"

    # 获取更新后的 commit 信息
    current_commit = await get_current_commit(repo_path)
    if current_commit:
        message = f"强制更新成功，当前版本: {current_commit['short_hash']}"
    else:
        message = "强制更新成功"

    logger.info(f"[Git Update] {message}")
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
        logger.warning(t("[Git Update] git pull 失败: {message}", message=message))
        return False, f"git pull 失败: {message}"

    # 获取更新后的 commit 信息
    current_commit = await get_current_commit(repo_path)
    if current_commit:
        message = f"更新成功，当前版本: {current_commit['short_hash']}"
    else:
        message = "更新成功"

    logger.info(f"[Git Update] {message}")
    return True, message


async def get_all_plugins_status() -> List[GitStatusInfo]:
    """
    获取所有插件（包括 core 本体）的 git 状态信息。

    Returns:
        GitStatusInfo 列表
    """
    result: List[GitStatusInfo] = []

    # core 本体
    core_status = await get_git_status(CORE_PATH)
    if core_status:
        result.append(core_status)

    # 所有插件
    if PLUGINS_PATH.exists():
        for plugin_dir in sorted(PLUGINS_PATH.iterdir()):
            if plugin_dir.is_dir() and plugin_dir.name != "__pycache__":
                plugin_status = await get_git_status(plugin_dir)
                if plugin_status:
                    result.append(plugin_status)

    return result


def _resolve_plugin_path(plugin_name: str) -> Optional[Path]:
    """
    解析插件名称到实际路径。

    Args:
        plugin_name: 插件名称

    Returns:
        插件路径，不存在返回 None
    """
    if plugin_name.lower() == "gsuid_core":
        return CORE_PATH

    # 尝试精确匹配
    plugin_path = PLUGINS_PATH / plugin_name
    if plugin_path.exists():
        return plugin_path

    # 尝试大小写不敏感匹配
    for d in PLUGINS_PATH.iterdir():
        if d.is_dir() and d.name.lower() == plugin_name.lower():
            return d

    return None
