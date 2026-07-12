"""
Git Async 工具模块

提供统一的异步 git 命令执行基础设施，替代 gitpython 库。
所有 git 操作均通过 asyncio.create_subprocess_exec 异步执行，
兼容 Windows/Linux/macOS 等主流系统。

特性：
- 统一的 _run_git 入口，设置 GIT_TERMINAL_PROMPT=0 防止凭证提示卡死
- 提供 clone、pull、fetch、checkout、remote 管理等常用操作
- 凭证请求自动超时跳过并报告
"""

import os
import asyncio
import subprocess
from typing import Optional
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger

# git 命令默认超时时间（秒）
GIT_TIMEOUT = 30

# clone 命令超时时间（秒），仓库较大时需要更长时间
GIT_CLONE_TIMEOUT = 120


async def run_git(repo_path: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str, str]:
    """
    在指定目录下异步执行 git 命令。

    使用 create_subprocess_exec 而非 create_subprocess_shell，
    避免 Windows cmd.exe 将 %an 等解释为环境变量，同时兼容所有平台。

    通过 GIT_TERMINAL_PROMPT=0 / GCM_INTERACTIVE=Never 阻止 Git / Windows
    上的 Git Credential Manager (GCM) 在收到 401/403 时弹窗卡死流程。
    如果需要凭证（如私有仓库），命令会立即失败并把错误冒泡给调用方。

    Args:
        repo_path: 仓库路径
        *args: git 子命令及参数
        timeout: 命令超时时间（秒），默认 30 秒

    Returns:
        (returncode, stdout, stderr)
        超时时返回 (-999, "", "timeout")
    """
    cmd_str = " ".join(["git", *args])
    logger.info(t("[Git Async] 执行命令: {cmd_str} @ {repo_path}", cmd_str=cmd_str, repo_path=repo_path))

    env = os.environ.copy()
    # git 2.26+ 在收到 401/403 时不要走 terminal prompt（命令立即失败）
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Windows 上 GCM 1.1.0+ 支持，关闭交互式凭证弹窗
    env["GCM_INTERACTIVE"] = "Never"
    # git 找不到 askpass 时回退到 echo，避免再次触发弹窗
    env["GIT_ASKPASS"] = "echo"
    # 设置 HTTP 超时
    env["GIT_HTTP_TIMEOUT"] = "30"
    # 使用简单的 User-Agent 避免某些服务器拒绝
    env["GIT_HTTP_USER_AGENT"] = "git/gsuid_core"

    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                t(
                    "[Git Async] 命令超时({timeout}s): {cmd_str} @ {repo_path}",
                    timeout=timeout,
                    cmd_str=cmd_str,
                    repo_path=repo_path,
                )
            )
            try:
                process.kill()
            except ProcessLookupError:
                pass
            return (-999, "", "timeout")

        returncode = process.returncode or 0
        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()
    except NotImplementedError:
        # Windows 下如果主程序使用了不支持子进程的 SelectorEventLoop，
        # asyncio.create_subprocess_exec 会直接抛出 NotImplementedError。
        # 这里退化为在线程中执行同步 subprocess.run，避免接口 500 且无需重启进程。
        logger.warning(
            t(
                "[Git Async] 当前事件循环不支持异步子进程，切换到线程执行: {cmd_str} @ {repo_path}",
                cmd_str=cmd_str,
                repo_path=repo_path,
            )
        )
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                t(
                    "[Git Async] 命令超时({timeout}s): {cmd_str} @ {repo_path}",
                    timeout=timeout,
                    cmd_str=cmd_str,
                    repo_path=repo_path,
                )
            )
            return (-999, "", "timeout")

        returncode = completed.returncode or 0
        stdout_str = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr_str = completed.stderr.decode("utf-8", errors="replace").strip()

    if returncode != 0:
        logger.warning(
            t(
                "[Git Async] 命令失败(returncode={returncode}): {cmd_str} @ {repo_path}",
                returncode=returncode,
                cmd_str=cmd_str,
                repo_path=repo_path,
            )
        )
        if stderr_str:
            logger.warning(f"[Git Async] stderr: {stderr_str}")
    else:
        logger.success(t("[Git Async] 命令成功: {cmd_str} @ {repo_path}", cmd_str=cmd_str, repo_path=repo_path))
        if stdout_str:
            logger.debug(f"[Git Async] stdout: {stdout_str[:200]}{'...' if len(stdout_str) > 200 else ''}")

    return (returncode, stdout_str, stderr_str)


async def git_clone(
    url: str,
    target_path: Path,
    branch: Optional[str] = None,
    depth: int = 1,
    timeout: int = GIT_CLONE_TIMEOUT,
) -> tuple[bool, str]:
    """
    异步克隆 git 仓库。

    关键：会在命令前注入 ``-c credential.helper= -c core.askPass=``，
    **禁用**任何 credential helper（包括 Windows 上的 Git Credential Manager）。
    这是为了在 cnb/gitcode 等镜像源未同步某个仓库（返回 401/403）时，
    git 能直接失败而不是弹凭证窗把流程卡死——失败后由 install_plugins
    根据错误信息判断是否 fallback 到 GitHub 原始源。

    Args:
        url: 仓库 URL
        target_path: 目标路径
        branch: 指定分支（可选）
        depth: 克隆深度，默认 1
        timeout: 超时时间（秒），默认 120 秒

    Returns:
        (success, message)
    """
    # -c credential.helper= 覆盖 git config 里的 helper（关键）
    # -c core.askPass=     禁用 git 内置 askpass 回退
    args = [
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "clone",
        "--depth",
        str(depth),
    ]
    if branch:
        args.extend(["--branch", branch])
    args.extend([url, str(target_path)])

    returncode, stdout, stderr = await run_git(Path("."), *args, timeout=timeout)

    if returncode == -999:
        return False, f"克隆超时({timeout}s)，可能需要 git 凭证或网络问题: {url}"

    if returncode != 0:
        logger.error(t("[Git Async] clone 失败: {stderr}", stderr=stderr))
        return False, f"克隆失败: {stderr}"

    logger.info(t("[Git Async] clone 成功: {url} -> {target_path}", url=url, target_path=target_path))
    return True, "克隆成功"


async def git_fetch(repo_path: Path, timeout: int = GIT_TIMEOUT) -> tuple[bool, str]:
    """
    异步执行 git fetch。

    Args:
        repo_path: 仓库路径
        timeout: 超时时间（秒）

    Returns:
        (success, message)
    """
    returncode, _, stderr = await run_git(repo_path, "fetch", timeout=timeout)

    if returncode == -999:
        return False, f"fetch 超时({timeout}s)，可能需要 git 凭证"

    if returncode != 0:
        logger.warning(t("[Git Async] fetch 失败: {stderr}", stderr=stderr))
        return False, f"fetch 失败: {stderr}"

    return True, "fetch 成功"


async def git_pull(repo_path: Path, timeout: int = GIT_TIMEOUT) -> tuple[bool, str]:
    """
    异步执行 git pull。

    Args:
        repo_path: 仓库路径
        timeout: 超时时间（秒）

    Returns:
        (success, message)
    """
    returncode, stdout, stderr = await run_git(repo_path, "pull", timeout=timeout)

    if returncode == -999:
        return False, f"pull 超时({timeout}s)，可能需要 git 凭证"

    if returncode != 0:
        logger.warning(t("[Git Async] pull 失败: {stderr}", stderr=stderr))
        return False, f"pull 失败: {stderr}"

    return True, stdout


async def git_reset_hard(repo_path: Path, target: str = "HEAD") -> tuple[bool, str]:
    """
    异步执行 git reset --hard。

    Args:
        repo_path: 仓库路径
        target: 目标 ref，默认 "HEAD"

    Returns:
        (success, message)
    """
    returncode, _, stderr = await run_git(repo_path, "reset", "--hard", target)

    if returncode != 0:
        logger.warning(t("[Git Async] reset --hard 失败: {stderr}", stderr=stderr))
        return False, f"reset --hard 失败: {stderr}"

    return True, f"已重置到 {target}"


async def git_clean_xdf(repo_path: Path) -> tuple[bool, str]:
    """
    异步执行 git clean -xdf（删除所有未跟踪文件）。

    Args:
        repo_path: 仓库路径

    Returns:
        (success, message)
    """
    returncode, _, stderr = await run_git(repo_path, "clean", "-xdf")

    if returncode != 0:
        logger.warning(t("[Git Async] clean -xdf 失败: {stderr}", stderr=stderr))
        return False, f"clean -xdf 失败: {stderr}"

    return True, "clean 完成"


async def git_get_remote_url(repo_path: Path) -> Optional[str]:
    """
    获取指定仓库的 origin remote URL。

    Args:
        repo_path: 仓库路径

    Returns:
        remote URL 字符串，如果不是 git 仓库则返回 None
    """
    if not (repo_path / ".git").exists():
        return None

    returncode, stdout, _ = await run_git(repo_path, "remote", "get-url", "origin")
    if returncode != 0 or not stdout:
        return None
    return stdout


async def git_set_remote_url(repo_path: Path, url: str) -> tuple[bool, str]:
    """
    设置指定仓库的 origin remote URL。

    Args:
        repo_path: 仓库路径
        url: 新的 remote URL

    Returns:
        (success, message)
    """
    returncode, _, stderr = await run_git(repo_path, "remote", "set-url", "origin", url)

    if returncode != 0:
        logger.error(t("[Git Async] set-url 失败: {stderr}", stderr=stderr))
        return False, f"设置 remote URL 失败: {stderr}"

    return True, f"已设置 remote URL: {url}"


async def git_get_current_branch(repo_path: Path) -> str:
    """
    获取仓库当前分支名称。

    在 detached HEAD 状态下，尝试获取默认分支名（main/master）。

    Args:
        repo_path: 仓库路径

    Returns:
        分支名称
    """
    returncode, stdout, _ = await run_git(repo_path, "branch", "--show-current")

    if returncode == 0 and stdout:
        return stdout

    # detached HEAD 状态，尝试获取远程默认分支
    returncode, stdout, _ = await run_git(
        repo_path,
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "--short",
    )

    if returncode == 0 and stdout and "/" in stdout:
        return stdout.split("/", 1)[1]

    # fallback: 尝试 main 和 master
    for branch_name in ("main", "master"):
        returncode, _, _ = await run_git(
            repo_path,
            "rev-parse",
            "--verify",
            f"origin/{branch_name}",
        )
        if returncode == 0:
            return branch_name

    return "main"


async def git_get_current_commit(repo_path: Path) -> str:
    """
    获取仓库当前 commit hash（短格式）。

    Args:
        repo_path: 仓库路径

    Returns:
        当前 commit hash（7位短格式），如果不是 git 仓库则返回空字符串
    """
    if not (repo_path / ".git").exists():
        return ""

    returncode, stdout, _ = await run_git(repo_path, "rev-parse", "--short", "HEAD")

    if returncode != 0 or not stdout:
        return ""

    return stdout


async def git_get_log(
    repo_path: Path,
    ref: str = "HEAD",
    max_count: int = 5,
) -> list[str]:
    """
    获取 git log 的 commit message 列表。

    Args:
        repo_path: 仓库路径
        ref: 起始 ref，默认 "HEAD"
        max_count: 最大返回数量

    Returns:
        commit message 列表
    """
    returncode, stdout, stderr = await run_git(
        repo_path,
        "log",
        ref,
        f"-{max_count}",
        "--format=%s",
    )

    if returncode != 0 or not stdout:
        return []

    return [line.strip() for line in stdout.split("\n") if line.strip()]


async def git_diff_commits(
    repo_path: Path,
    from_ref: str,
    to_ref: str,
    max_count: int = 40,
) -> list[str]:
    """
    获取两个 ref 之间的 commit message 列表。

    Args:
        repo_path: 仓库路径
        from_ref: 起始 ref
        to_ref: 结束 ref
        max_count: 最大返回数量

    Returns:
        commit message 列表
    """
    returncode, stdout, stderr = await run_git(
        repo_path,
        "log",
        f"{from_ref}..{to_ref}",
        f"-{max_count}",
        "--format=%s",
    )

    if returncode != 0 or not stdout:
        return []

    return [line.strip() for line in stdout.split("\n") if line.strip()]


async def git_is_valid_repo(repo_path: Path) -> bool:
    """
    检查路径是否是有效的 git 仓库。

    Args:
        repo_path: 仓库路径

    Returns:
        是否是有效的 git 仓库
    """
    if not repo_path.exists() or not repo_path.is_dir():
        return False

    returncode, _, _ = await run_git(repo_path, "rev-parse", "--git-dir")
    return returncode == 0
