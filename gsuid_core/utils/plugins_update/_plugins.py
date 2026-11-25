import os
import re
import ast
import shutil
import asyncio
import tempfile
import subprocess
from typing import Dict, List, Union, Optional
from pathlib import Path

import aiohttp

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.server import _DefHook, core_start_def
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

from .api import CORE_PATH, PLUGINS_PATH, plugins_lib
from .utils import check_start_tool
from .git_async import (
    run_git,
    git_pull,
    git_clone,
    git_fetch,
    git_clean_xdf,
    git_reset_hard,
    git_diff_commits,
    git_is_valid_repo,
    git_get_current_branch,
    git_get_current_commit,
)
from .reload_plugin import reload_plugin_async

plugins_list: Dict[str, Dict[str, str]] = {}

# 插件 commit 版本信息（仅在运行时内部流转，不持久化）
plugin_commit_versions: Dict[str, str] = {}
AUTO_RELOAD_FAIL_PREFIX = "❌ [自动重载失败]"


def extract_update_failure(result: str | list[str]) -> str | None:
    """从 update_plugins / update_from_git_async 返回值中提取失败信息，成功返回 None。"""
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return "插件更新失败"
        if text.startswith("✅"):
            return None
        return text
    if not result:
        return "插件更新失败"
    first = result[0].lstrip()
    if first.startswith("❌") or first.startswith("更新失败"):
        return "\n".join(result)
    last = result[-1].lstrip()
    if last.startswith(AUTO_RELOAD_FAIL_PREFIX):
        return result[-1]
    return None


def _is_mirror_not_synced_error(message: str) -> bool:
    """
    判断 git 错误是否表示镜像源（cnb/gitcode）尚未同步该插件。

    典型现象：
    - 镜像源仓库不存在：404 / "repository not found"
    - 镜像源对未登录用户返回 401，触发 git 凭证提示，被 GIT_TERMINAL_PROMPT=0
      抑制后报 "terminal prompts disabled" / "could not read username"
    - 镜像源鉴权失败：403 / "authentication failed" / "permission denied"

    此类错误代表「镜像源就是没这个仓库」，应当 fallback 到 GitHub 源。
    网络抖动 / 超时 / DNS 等临时错误不在此列，避免无意义回退。
    """
    msg_lower = message.lower()
    not_synced_keywords = (
        "repository not found",
        "not found",
        " 404",
        "(404)",
        " 401",
        "(401)",
        " 403",
        "(403)",
        "authentication failed",
        "permission denied",
        "access denied",
        "forbidden",
        "credential",
        "username",
        "could not read",
        "terminal prompts disabled",
    )
    return any(kw in msg_lower for kw in not_synced_keywords)


def _parse_git_error(message: str, plugin_name: str, operation: str = "pull") -> List[str]:
    """
    解析 git 错误信息，返回用户友好的提示和解决方案。

    Args:
        message: git 命令返回的原始错误信息
        plugin_name: 插件名称
        operation: 操作类型，"pull" 或 "fetch"

    Returns:
        包含错误描述和解决方案的字符串列表
    """
    msg_lower = message.lower()
    prefix = f"更新插件 {plugin_name} 失败"

    # 合并冲突
    if any(kw in msg_lower for kw in ["conflict", "merge", "automatic merge failed", "cannot be resolved"]):
        return [
            f"❌ {prefix}",
            "📋 原因：本地代码与远程代码存在合并冲突",
            "💡 解决：使用「强制更新」或「强行强制更新」覆盖本地修改",
        ]

    # 本地有未提交的修改
    if any(
        kw in msg_lower
        for kw in [
            "your local changes to the following files would be overwritten",
            "please commit your changes or stash them",
            "entry '.*' not uptodate",
            "dirty",
        ]
    ):
        return [
            f"❌ {prefix}",
            "📋 原因：本地存在未提交的修改，无法自动合并",
            "💡 解决：使用「强制更新」丢弃本地修改，或手动 stash 后重试",
        ]

    # 分支分叉
    if any(kw in msg_lower for kw in ["diverged", "need to specify how to reconcile", "refusing to merge unrelated"]):
        return [
            f"❌ {prefix}",
            "📋 原因：本地与远程分支历史分叉，无法自动合并",
            "💡 解决：使用「强制更新」重置到远程版本",
        ]

    # 网络错误
    if any(
        kw in msg_lower
        for kw in [
            "could not resolve host",
            "connection refused",
            "network is unreachable",
            "connection timed out",
            "failed to connect",
            "couldn't connect to server",
        ]
    ):
        return [
            f"❌ {prefix}",
            "📋 原因：网络连接失败，无法访问远程仓库",
            "💡 解决：检查网络连接，或配置 Git 镜像源加速访问",
        ]

    # SSL / 证书错误
    if any(kw in msg_lower for kw in ["ssl", "certificate", "cert"]):
        return [
            f"❌ {prefix}",
            "📋 原因：SSL 证书验证失败",
            "💡 解决：检查系统时间是否正确，或尝试更新 CA 证书",
        ]

    # 远程仓库不存在 / URL 错误
    if any(
        kw in msg_lower
        for kw in [
            "repository not found",
            "does not appear to be a git repository",
            "not found",
            "404",
        ]
    ):
        return [
            f"❌ {prefix}",
            "📋 原因：远程仓库地址无效或仓库已被删除",
            "💡 解决：检查插件仓库地址是否正确，可能需要重新安装插件",
        ]

    # 认证 / 权限错误
    if any(
        kw in msg_lower
        for kw in [
            "permission denied",
            "authentication failed",
            "403",
            "401",
            "credential",
            "fatal: could not read",
            "username",
        ]
    ):
        return [
            f"❌ {prefix}",
            "📋 原因：仓库认证失败，可能是私有仓库需要登录凭证",
            "💡 解决：检查 Git 凭证配置，或确认仓库访问权限",
        ]

    # 超时
    if "timeout" in msg_lower:
        return [
            f"⏭️ 跳过更新插件 {plugin_name}",
            "⚠️ git 操作超时，可能需要 git 凭证或网络不稳定",
        ]

    # 磁盘空间不足
    if any(kw in msg_lower for kw in ["no space left", "disk full", "not enough space"]):
        return [
            f"❌ {prefix}",
            "📋 原因：磁盘空间不足",
            "💡 解决：清理磁盘空间后重试",
        ]

    # 通用兜底
    return [
        f"❌ {prefix}",
        f"📋 原因：{message[:100]}",
        "💡 解决：尝试「强制更新」，若仍失败请检查控制台日志",
    ]


is_install_dep = core_plugins_config.get_config("AutoInstallDep").data
is_reload: bool = core_plugins_config.get_config("AutoReloadPlugins").data


async def check_plugin_exist(name: str):
    name = name.lower()

    if name in ["core_command", "gs_test"]:
        return "❌ 内置插件不可删除！"

    for i in PLUGINS_PATH.iterdir():
        if i.stem.lower().strip("_") == name:
            return i
    else:
        for i in PLUGINS_PATH.iterdir():
            if name in i.name.lower():
                return i


def _remove_plugin_path(path: Path) -> tuple[bool, str]:
    """同步删除插件目录 / 文件，返回 (是否为目录, 错误信息)。

    rmtree + chmod 重试都是阻塞调用，调用方必须放进 asyncio.to_thread（见 §4.2）。
    """
    name = path.name
    was_dir = path.is_dir()
    if not was_dir:
        path.unlink()
        return False, ""
    locked_msg = f"⚠️ 插件目录 {name} 部分文件被锁定,请手动删除或重启后重试!"

    # Windows下处理被锁定的文件
    def onerror(func, path, exc):
        """处理删除文件时的权限错误"""
        import stat

        # 检查文件是否只读
        if not os.access(path, os.W_OK):
            # 尝试移除只读属性
            try:
                os.chmod(path, stat.S_IWUSR)
                func(path)
            except Exception:
                pass  # 忽略二次错误
        else:
            raise exc

    try:
        shutil.rmtree(path)
    except PermissionError:
        try:
            shutil.rmtree(path, onerror=onerror)
            if path.exists():
                _try_manual_delete(path)
        except Exception:
            return True, locked_msg
    if path.exists():
        return True, locked_msg
    return True, ""


async def uninstall_plugin(path: Path):
    name = path.name
    if not await asyncio.to_thread(path.exists):
        return f"❌ 插件 {name} 不存在!"

    from gsuid_core.server import read_meta_plugin_info
    from gsuid_core.meta_plugins import unregister_meta_plugin

    # meta 信息必须在删除前读完
    meta_info = await asyncio.to_thread(read_meta_plugin_info, path)
    was_dir, err = await asyncio.to_thread(_remove_plugin_path, path)
    if err:
        return err

    warn = ""
    if meta_info is not None:
        unregister_meta_plugin(name)
        logger.warning(t("log.plugin.uninstall_meta_warn", plugin_name=name))
        warn = f"⚠️ 已卸载基础设施插件 {name}，依赖它的插件下次调用会失败。\n"

    try:
        from gsuid_core.help.utils import clean_plugin_help

        clean_plugin_help(name)
    except Exception as e:
        logger.warning(t("log.plugin.gscore_fail_help_cache", plugin_name=name, e=e))

    kind = "目录" if was_dir else "文件"
    return f"{warn}✅ 插件{kind} {name} 删除成功!"


def _try_manual_delete(path: Path):
    """手动尝试删除目录内容"""
    import stat

    for item in path.rglob("*"):
        try:
            if item.is_file():
                os.chmod(item, stat.S_IWUSR)
                item.unlink()
            elif item.is_dir():
                os.chmod(item, stat.S_IWUSR)
                shutil.rmtree(item, ignore_errors=True)
        except Exception:
            pass
    # 最后尝试删除根目录
    try:
        os.chmod(path, stat.S_IWUSR)
        path.rmdir()
    except Exception:
        pass


# 传入一个path对象
def run_install(path: Optional[Path] = None) -> int:
    tools = check_start_tool()
    if tools == "python":
        logger.warning(t("log.plugin.pip_pdm_poetry_install"))
        return -200

    if path is None:
        path = CORE_PATH

    # 检测path是否是一个目录
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")

    # 异步执行poetry install命令，并返回返回码
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf8"

    if tools == "uv":
        # 运行中 core.exe 被占用，重装项目本身会 os error 5，连新依赖一起装不上
        CMD = "uv sync --inexact --no-install-project"
    else:
        CMD = f"{tools} install"

    proc = subprocess.run(
        CMD,
        cwd=path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        env=env,
        encoding="utf-8",
        text=True,
    )

    output = proc.stdout  # 获取输出
    error = proc.stderr  # 获取错误信息

    logger.info(output)
    if error:
        logger.error(error)

    retcode = -1 if proc.returncode is None else proc.returncode
    if "No dependencies to install or update" in output:
        retcode = 200
    return retcode


def check_retcode(retcode: int) -> str:
    if retcode == 200:
        return "无需更新依赖！"
    elif retcode == 0:
        return "新增/更新依赖成功!"
    else:
        return f"更新失败, 错误码{retcode}"


async def update_all_plugins(level: int = 0) -> List[str]:
    log_list = []
    for plugin in PLUGINS_PATH.iterdir():
        # 非 git 插件（手动拷入 / 从 zip 安装）没有远程可拉，跳过而不是记一条失败
        if _is_plugin(plugin) and (plugin / ".git").exists():
            log_list.extend(await update_from_git_async(level, plugin))
    return log_list


def _is_plugin(plugin: Path) -> bool:
    if plugin.is_dir() and plugin.name != "__pycache__" and plugin.name != "core_command":
        return True
    return False


async def set_proxy_all_plugins(proxy: Optional[str] = None) -> List[str]:
    log_list = []
    for plugin in PLUGINS_PATH.iterdir():
        if _is_plugin(plugin):
            log_list.append(await set_proxy(plugin, proxy))
    log_list.append(await set_proxy(CORE_PATH, proxy))
    return log_list


async def refresh_list() -> List[str]:
    refresh_list = []
    async with aiohttp.ClientSession(trust_env=True) as session:
        logger.trace(t("log.plugin.plugins_lib", plugins_lib=plugins_lib))
        async with session.get(plugins_lib) as resp:
            _plugins_list: Dict[str, Dict[str, Dict[str, str]]] = await resp.json()
            for i in _plugins_list["plugins"]:
                if i.lower() not in plugins_list:
                    refresh_list.append(i)
                    logger.debug(t("log.plugin.event_info", i=i))
                plugins_list[i.lower()] = _plugins_list["plugins"][i]
    return refresh_list


async def get_plugins_list() -> Dict[str, Dict[str, str]]:
    if not plugins_list:
        await refresh_list()
    return plugins_list


async def get_local_plugins_list() -> Dict[str, Dict[str, str]]:
    """获取本地已安装的插件列表"""
    local_plugins: Dict[str, Dict[str, str]] = {}
    for plugin_dir in PLUGINS_PATH.iterdir():
        if _is_plugin(plugin_dir):
            plugin_name = plugin_dir.name
            local_plugins[plugin_name.lower()] = {
                "name": plugin_name,
                "link": str(plugin_dir),
                "info": f"本地插件：{plugin_name}",
                "branch": "main",
            }
    return local_plugins


async def get_plugins_url(name: str) -> Optional[Dict[str, str]]:
    """获取插件的 URL 信息，支持大小写不敏感匹配。"""
    if not plugins_list:
        await refresh_list()

    # 精确匹配（大小写敏感）
    if name in plugins_list:
        return plugins_list[name]

    # 大小写不敏感匹配
    name_lower = name.lower()
    for _n in plugins_list:
        if name_lower == _n.lower():
            return plugins_list[_n]

    # 包含匹配（大小写敏感）
    for _n in plugins_list:
        if name in _n:
            return plugins_list[_n]

    # 包含匹配（大小写不敏感）
    for _n in plugins_list:
        if name_lower in _n.lower():
            return plugins_list[_n]

    return None


async def load_installed_plugin(plugin_name: str) -> str:
    """插件目录已落地后，热加载它并返回统一结果文案。"""
    # 安装完成后直接调用 reload_plugin_async 完成首次加载并触发启动钩子。
    retcode = await reload_plugin_async(plugin_name)
    if retcode.lstrip().startswith("❌"):
        return f"❌ 插件{plugin_name}已安装, 但加载失败, 可尝试[core重启]:\n{retcode}"
    return f"✅ 插件{plugin_name}安装并加载成功!"


async def install_plugins(plugins: Dict[str, str]) -> str:
    from .git_async import git_set_remote_url
    from .git_mirror import SSH_GITHUB_TEMPLATE, _is_ssh_mode, _is_proxy_prefix

    git_mirror: str = core_plugins_config.get_config("GitMirror").data

    plugin_name = plugins["link"].split("/")[-1]

    # 始终保留一份原始 GitHub URL 作为兜底，用于镜像源未同步时的回退
    fallback_git_path = f"{plugins['link']}.git"

    # 使用 GitMirror 镜像源/代理/SSH
    if git_mirror:
        if _is_ssh_mode(git_mirror):
            # SSH 模式：ssh://git@ssh.github.com:443/{owner}/{repo}.git
            link = plugins["link"].rstrip("/")
            parts = link.split("/")
            if len(parts) >= 2:
                owner, repo = parts[-2], parts[-1]
                git_path = SSH_GITHUB_TEMPLATE.format(owner=owner, repo=repo)
            else:
                git_path = fallback_git_path
        elif _is_proxy_prefix(git_mirror):
            # 代理前缀模式：{proxy_prefix}{full_github_url}（仍指向 GitHub 源，无需 fallback）
            proxy_prefix = git_mirror.rstrip("/") + "/"
            git_path = f"{proxy_prefix}{plugins['link']}.git"
        else:
            # 镜像模式：{mirror_prefix}/{repo_name}（cnb/gitcode 可能未同步，需 fallback）
            mirror_prefix = git_mirror.rstrip("/")
            git_path = f"{mirror_prefix}/{plugin_name}"
    else:
        git_path = fallback_git_path

    # 仅当用户配置了「镜像模式」（cnb/gitcode）时才允许 fallback；
    # 代理/SSH/默认 GitHub 模式路由目标都是 GitHub，不存在「镜像未同步」场景。
    is_mirror_mode = bool(git_mirror) and not _is_ssh_mode(git_mirror) and not _is_proxy_prefix(git_mirror)
    enable_mirror_fallback = is_mirror_mode and git_path != fallback_git_path

    logger.info(t("log.plugin.git_path_git_path_start", git_path=git_path))
    path = PLUGINS_PATH / plugin_name
    if path.exists():
        return "❌ 该插件已经安装过了!"

    branch = plugins["branch"] if plugins["branch"] != "main" else None

    success, message = await git_clone(git_path, path, branch=branch, depth=1)
    used_fallback = False
    if not success and enable_mirror_fallback:
        if _is_mirror_not_synced_error(message):
            logger.warning(
                t(
                    "log.plugin.git_mirror_plugin_name",
                    git_mirror=git_mirror,
                    plugin_name=plugin_name,
                    p0=message[:120],
                    fallback_git_path=fallback_git_path,
                )
            )
            success, message = await git_clone(fallback_git_path, path, branch=branch, depth=1)
            used_fallback = True
            if success:
                # 把插件的 remote URL 也设回 GitHub 源，避免后续 update_from_git_async
                # 又去走那个没同步的镜像导致再次失败
                try:
                    ok, set_msg = await git_set_remote_url(path, fallback_git_path)
                    if ok:
                        logger.info(t("log.plugin.plugin_name_remote_url_github", plugin_name=plugin_name))
                    else:
                        logger.warning(
                            t(
                                "log.plugin.plugin_name_remote_url_set",
                                plugin_name=plugin_name,
                                set_msg=set_msg,
                            )
                        )
                except Exception as e:
                    logger.warning(t("log.plugin.plugin_name_remote_url", plugin_name=plugin_name, e=e))
        else:
            logger.info(t("log.plugin.fail_error_sync_fallback", p0=message[:120]))

    if not success:
        if used_fallback:
            return f"❌ 插件{plugin_name}安装失败：镜像源 {git_mirror} 与 GitHub 源均不可用，最后错误: {message}"
        return f"❌ 插件{plugin_name}安装失败: {message}"

    if used_fallback:
        logger.info(t("log.plugin.plugin_name_github", plugin_name=plugin_name))
    else:
        logger.info(t("log.plugin.plugin_name_2", plugin_name=plugin_name))
    return await load_installed_plugin(plugin_name)


async def install_plugin(plugin_name: str) -> str:
    url = await get_plugins_url(plugin_name)
    if url is None:
        return f"❌ 不存在插件 {plugin_name}, 请检查名称或使用[刷新插件列表]!"
    return await install_plugins(url)


async def install_plugin_from_url(url: str, branch: Optional[str] = None) -> str:
    """
    直接从用户提供的 git URL 安装插件（不走插件商店白名单）。

    适用于安装未收录在插件商店中的自定义插件、内部仓库或第三方仓库。
    复用 ``install_plugins`` 的 GitMirror 镜像 / fallback / reload 逻辑。

    Args:
        url: git 仓库 URL，支持 HTTP(S) / SSH(SCP) 两种格式
             （如 ``https://github.com/owner/repo.git`` 、
             ``git@github.com:owner/repo.git`` 或
             ``https://gitcode.com/owner/repo``）。
             末尾的 ``.git`` 后缀可选。
        branch: 指定克隆的分支名称，可选；不传则使用仓库默认分支。

    Returns:
        人类可读的结果字符串（成功以 ✅ 开头，失败以 ❌ 开头）。
    """
    from .git_mirror import _extract_repo_name

    # ── 1. 基础校验 ────────────────────────────────────────────
    if not url or not isinstance(url, str):
        return "❌ 请提供有效的 git 仓库 URL"

    url = url.strip()
    if not url:
        return "❌ 请提供有效的 git 仓库 URL"

    valid_prefixes = ("http://", "https://", "ssh://", "git@")
    if not url.startswith(valid_prefixes):
        return "❌ URL 协议不支持, 当前仅支持 http://、https://、ssh://、git@ 开头的 git 仓库"

    # ── 2. 从 URL 提取仓库名作为插件目录名 ─────────────────────
    plugin_name = _extract_repo_name(url)
    if not plugin_name:
        return "❌ 无法从 URL 中提取仓库名, 请检查 URL 是否合法"

    # ── 3. 规范化 URL：去掉尾部斜杠和 .git 后缀，
    #         install_plugins 会按 {link}.git 的格式重新拼装 ──
    clean_url = url.rstrip("/")
    if clean_url.endswith(".git"):
        clean_url = clean_url[:-4]

    # ── 4. 构造 install_plugins 期望的 dict 并复用其完整流程 ──
    plugins_dict: Dict[str, str] = {
        "link": clean_url,
        # 不传 branch 时 install_plugins 会用 None, 走仓库默认分支
        "branch": branch.strip() if branch and branch.strip() else "main",
    }

    logger.info(
        t(
            "log.plugin.url_name_branch",
            url=url,
            plugin_name=plugin_name,
            p0=plugins_dict["branch"],
        )
    )
    return await install_plugins(plugins_dict)


async def check_plugins(plugin_name: str) -> Optional[Path]:
    path = PLUGINS_PATH / plugin_name
    if path.exists():
        if await git_is_valid_repo(path):
            return path
        return None
    else:
        return None


async def check_can_update(repo_path: Path) -> bool:
    """检查仓库是否有可用更新"""
    success, _ = await git_fetch(repo_path)
    if not success:
        return False

    branch = await git_get_current_branch(repo_path)

    # 比较本地和远程的 commit hash
    returncode_local, local_hash, _ = await run_git(repo_path, "rev-parse", "HEAD")
    returncode_remote, remote_hash, _ = await run_git(repo_path, "rev-parse", f"origin/{branch}")

    if returncode_local != 0 or returncode_remote != 0:
        return False

    return local_hash != remote_hash


async def async_check_plugins(plugin_name: str):
    path = PLUGINS_PATH / plugin_name
    if path.exists():
        try:
            success, _ = await git_fetch(path, timeout=10)
            if not success:
                return 0

            returncode, stdout, _ = await run_git(path, "status")
            if returncode != 0:
                return 0

            if "Your branch is up to date" in stdout:
                return 4
            elif "not a git repository" in stdout:
                return 3
            else:
                return 1
        except Exception as e:
            logger.warning(t("log.plugin.name_plugin", plugin_name=plugin_name, p0=str(e)))
            return 0
    return 3


async def check_status(plugin_name: str) -> int:
    return await async_check_plugins(plugin_name)


def extract_last_url(text: str):
    if "/http" in text:
        parts = text.split("/http")
        url = "http" + parts[-1]
        return url
    elif text.startswith("https://github.com/"):
        return text
    else:
        return None


async def set_proxy(repo: Path, proxy: Optional[str] = None) -> str:
    """设置单个仓库的 git remote URL（使用 GitMirror 镜像源）"""
    from .git_mirror import set_plugin_mirror

    mirror_prefix = proxy if proxy is not None else core_plugins_config.get_config("GitMirror").data
    success, message = await set_plugin_mirror(repo, mirror_prefix)
    return message


async def update_from_git_async(
    level: int = 0,
    repo_like: Union[str, Path, None] = None,
    log_key: List[str] = [],
    log_limit: int = 5,
) -> List[str]:
    """
    异步更新 git 仓库（替代原来的 update_from_git + update_from_git_in_tread）。

    Args:
        level: 更新等级 0=普通, 1=强制(reset --hard), 2=强行强制(clean -xdf + reset)
        repo_like: 仓库路径/名称，None 表示更新 core 本体
        log_key: 过滤 commit message 的关键字
        log_limit: 最大返回日志条数

    Returns:
        日志列表
    """
    # 解析仓库路径
    if repo_like is None:
        repo_path = CORE_PATH
        plugin_name = "早柚核心"
    elif isinstance(repo_like, Path):
        repo_path = repo_like
        plugin_name = repo_like.name
    else:
        checked = await check_plugins(repo_like)
        plugin_name = repo_like
        if not checked:
            logger.warning(t("log.plugin.missing_update_fail"))
            return ["更新失败, 不存在该插件!"]
        repo_path = checked

    # 验证是否是有效的 git 仓库
    if not await git_is_valid_repo(repo_path):
        logger.warning(t("log.plugin.update_fail"))
        return ["更新失败, 该路径并不是一个有效的GitRepo路径, 请使用`git clone`安装插件..."]

    logger.info(t("log.plugin.plugin_name_level", plugin_name=plugin_name, level=level))

    # 先执行 git fetch
    logger.info(t("log.plugin.plugin_name_git_fetch", plugin_name=plugin_name))
    success, message = await git_fetch(repo_path)
    if not success:
        logger.warning(t("log.plugin.git_fetch_message", message=message))
        return _parse_git_error(message, plugin_name, operation="fetch")

    # 获取当前分支
    default_branch = await git_get_current_branch(repo_path)

    # 获取更新前的 commit log 用于差异比较
    commits_diff = await git_diff_commits(
        repo_path,
        "HEAD",
        f"origin/{default_branch}",
        max_count=40,
    )

    # level >= 2: 强行强制更新 - clean -xdf
    if level >= 2:
        logger.warning(t("log.plugin.plugin_name_git_clean_xdf", plugin_name=plugin_name))
        logger.warning(t("log.plugin.update_event"))
        if plugin_name == "早柚核心":
            return ["更新失败, 禁止强行强制更新核心..."]
        await asyncio.sleep(2)
        await git_clean_xdf(repo_path)

    # level >= 1: 强制更新 - reset --hard
    if level >= 1:
        logger.warning(t("log.plugin.plugin_name_git_reset_hard", plugin_name=plugin_name))
        await git_reset_hard(repo_path)

    # 执行 git pull
    success, pull_message = await git_pull(repo_path)
    if not success:
        logger.warning(t("log.plugin.pull_message", pull_message=pull_message))
        return _parse_git_error(pull_message, plugin_name, operation="pull")

    logger.info(t("log.plugin.plugin_name_pull_message", plugin_name=plugin_name, pull_message=pull_message))

    # 构建更新日志
    log_list: List[str] = []
    if commits_diff:
        log_list.append(f"✅本次插件 {plugin_name} , 更新内容如下：")
        for commit_msg in commits_diff:
            if log_key:
                for key in log_key:
                    if key in commit_msg:
                        log_list.append(commit_msg)
                        if len(log_list) >= log_limit:
                            break
            else:
                log_list.append(commit_msg)
                if len(log_list) >= log_limit:
                    break
    else:
        log_list.append(f"✅插件 {plugin_name} 本次无更新内容！")

    if repo_like is None and is_install_dep:
        # pull 成功后才装依赖，否则 uv sync 读到的是旧 lock。
        # 安装是长耗时子进程，挪出事件循环，避免整轮 core 无响应。
        retcode = await asyncio.to_thread(run_install, CORE_PATH)
        log_list.append(check_retcode(retcode))

    # 仅在「确实有新提交」或「强制更新(reset --hard 可能改动工作区, 且为显式操作)」时才重载,
    # 避免无更新的插件被无谓重载 —— 尤其凌晨自动更新会遍历全部插件。
    if (commits_diff or level >= 1) and plugin_name != "早柚核心" and is_reload:
        reload_result = await reload_plugin_async(plugin_name)
        if reload_result.lstrip().startswith("❌"):
            logger.warning(t("log.plugin.auto_reload_fail", plugin_name=plugin_name, result=reload_result))
            detail = reload_result.lstrip().removeprefix("❌").strip()
            log_list.append(f"{AUTO_RELOAD_FAIL_PREFIX}[{plugin_name}] {detail}")
    return log_list


async def resolve_plugin_name(plugin_name: str) -> str:
    """把用户输入的插件名(可能是别名 / 大小写不对)解析为实际的插件目录名。

    先按远程插件列表的 alias 归一化, 再对照本地 PLUGINS_PATH 目录名修正大小写;
    解析不到则原样返回。`更新` / `重载插件` 等指令共用此别名逻辑。
    """
    if not plugins_list:
        await refresh_list()

    pn = plugin_name.lower()
    for _n in plugins_list:
        plugin = plugins_list[_n]
        if "alias" in plugin:
            for alias in plugin["alias"]:
                if pn == alias.lower():
                    pn = _n.lower()
                    break

    for _n in PLUGINS_PATH.iterdir():
        if pn == _n.name.lower():
            return _n.name

    return plugin_name


async def update_plugins(
    plugin_name: str,
    level: int = 0,
    log_key: List[str] = [],
    log_limit: int = 10,
) -> Union[str, List[str]]:
    if not plugin_name:
        return "请后跟有效的插件名称！\n例如：core更新插件genshinuid"

    plugin_name = await resolve_plugin_name(plugin_name)

    log_list = await update_from_git_async(
        level,
        plugin_name,
        log_key,
        log_limit,
    )
    return log_list


# 注册到 on_core_start 钩子，优先级最高(priority=0)
async def _init_plugin_commit_versions() -> None:
    """
    在启动时初始化所有插件和本体的 commit 版本信息。
    该函数仅在运行时内部流转，不持久化到任何存储。
    """
    global plugin_commit_versions

    # 获取 core 的 commit 版本
    core_commit = await git_get_current_commit(CORE_PATH)
    if core_commit:
        plugin_commit_versions["core"] = core_commit

    # 获取所有插件的 commit 版本
    for plugin_dir in PLUGINS_PATH.iterdir():
        if plugin_dir.is_dir() and (plugin_dir / ".git").exists():
            commit = await git_get_current_commit(plugin_dir)
            if commit:
                plugin_commit_versions[plugin_dir.name.lower()] = commit

    # 同时检查 buildin_plugins
    buildin_path = CORE_PATH / "buildin_plugins"
    if buildin_path.exists():
        for plugin_dir in buildin_path.iterdir():
            if plugin_dir.is_dir() and (plugin_dir / ".git").exists():
                commit = await git_get_current_commit(plugin_dir)
                if commit:
                    plugin_commit_versions[plugin_dir.name.lower()] = commit

    logger.info(t("log.plugin.git_init_plugin_commit", p0=len(plugin_commit_versions)))


# 注册到 on_core_start 钩子，优先级最高
core_start_def.add(_DefHook(priority=0, func=_init_plugin_commit_versions))


def get_plugin_commit(plugin_name: str) -> str:
    """
    获取指定插件的 commit 版本。

    Args:
        plugin_name: 插件名称

    Returns:
        commit hash 短格式，未找到则返回空字符串
    """
    return plugin_commit_versions.get(plugin_name.lower(), "")


class PluginInstallError(ValueError):
    """从 zip 安装插件时的可预期失败，文案可直接展示给用户。"""


_PLUGINS_CALL = re.compile(r"""Plugins\(\s*(?:name\s*=\s*)?["']([^"']*)["']""")
_PLUGINS_KEYWORD = re.compile(r"""Plugins\(\s*name\s*=\s*["']([^"']*)["']""")
_PLUGINS_POSITIONAL = re.compile(r"""Plugins\(\s*["']([^"']*)["']""")
_NAME_ENTRY_DEPTH = 3

_zip_install_lock = asyncio.Lock()


def _plugins_name_from_source(source: str) -> set[str]:
    """从单个文件的源码里收集 ``Plugins(name=...)`` 的取值。"""
    found: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        found.update(_PLUGINS_CALL.findall(source))
        return found
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if func_name != "Plugins":
            continue
        keyword = next((kw for kw in node.keywords if kw.arg == "name"), None)
        arg = keyword.value if keyword is not None else (node.args[0] if node.args else None)
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.add(arg.value)
    return found


def _candidate_init_files(root: Path) -> list[Path]:
    """内层同名包优先，其次外层，最后浅层递归。"""
    candidates: list[Path] = []
    inner = root / root.name / "__init__.py"
    if inner.is_file():
        candidates.append(inner)
    outer = root / "__init__.py"
    if outer.is_file():
        candidates.append(outer)
    for path in sorted(root.rglob("__init__.py")):
        if path in candidates:
            continue
        relative = path.relative_to(root)
        if len(relative.parts) - 1 > _NAME_ENTRY_DEPTH:
            continue
        if any(part in {"__pycache__", "node_modules", "tests"} for part in relative.parts):
            continue
        candidates.append(path)
    return candidates


def resolve_plugin_register_name(root: Path) -> str:
    """解析插件根内声明的 ``Plugins(name=...)``。

    解析不到或为空一律抛 ``PluginInstallError``：从文件安装不接受回退猜测，
    因为 zip 的顶层目录名是用户随手打的，用它当插件名不可控。
    """
    names: set[str] = set()
    for init_file in _candidate_init_files(root):
        try:
            source = init_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        names.update(_plugins_name_from_source(source))
        if len(names) > 1:
            break

    if not names:
        raise PluginInstallError("插件注册名不能为空")
    if len(names) > 1:
        raise PluginInstallError(f"压缩包内注册了多个插件名（{', '.join(sorted(names))}），请每个插件单独打包")
    name = names.pop().strip()
    if not name:
        raise PluginInstallError("插件注册名不能为空")
    return name


async def install_plugin_from_zip(
    zip_bytes: bytes,
    *,
    plugins_path: Optional[Path] = None,
    buildin_path: Optional[Path] = None,
) -> str:
    """把上传的 zip 安装为插件目录并热加载。

    校验全部在临时目录内完成，失败时 ``plugins/`` 下一个字节都不会变。
    ``plugins_path`` / ``buildin_path`` 仅供测试注入，生产留空走真实目录。
    """
    from gsuid_core.server import BUILDIN_PLUGIN_PATH
    from gsuid_core.utils.zip_safe import (
        UnsafeArchiveError,
        remove_tree,
        single_root,
        extract_zip_safe,
    )
    from gsuid_core.utils.path_safety import is_safe_filename

    target_root = plugins_path if plugins_path is not None else PLUGINS_PATH
    buildin_root = buildin_path if buildin_path is not None else BUILDIN_PLUGIN_PATH

    if _zip_install_lock.locked():
        return "❌ 正在安装其它插件，请稍后再试"

    async with _zip_install_lock:
        staging = Path(tempfile.mkdtemp(prefix="gscore-plugin-"))
        try:
            archive_path = staging / "upload.zip"
            await asyncio.to_thread(archive_path.write_bytes, zip_bytes)

            extracted = staging / "extracted"
            try:
                await asyncio.to_thread(extract_zip_safe, archive_path, extracted)
            except UnsafeArchiveError as e:
                return f"❌ {e}"

            root = await asyncio.to_thread(single_root, extracted)
            if not await asyncio.to_thread(_looks_like_plugin, root):
                return "❌ 压缩包内没有找到 GsCore 插件（缺少 __init__.py / __full__.py / __nest__.py）"

            try:
                plugin_name = await asyncio.to_thread(resolve_plugin_register_name, root)
            except PluginInstallError as e:
                return f"❌ {e}"

            if not is_safe_filename(plugin_name):
                return f"❌ 插件注册名 {plugin_name} 非法"
            if plugin_name != root.name:
                return f"❌ 插件注册名 {plugin_name} 与压缩包目录名 {root.name} 不一致，请重新打包"

            target = target_root / plugin_name
            if await asyncio.to_thread((buildin_root / plugin_name).exists):
                return f"❌ {plugin_name} 是内置插件，不能覆盖"
            if await asyncio.to_thread(target.exists):
                return "❌ 该插件已经安装过了!"

            await asyncio.to_thread(shutil.move, str(root), str(target))

            # 与 install_plugins 行为一致：
            return await load_installed_plugin(plugin_name)
            # 备份/回滚留待后续迭代。
        finally:
            await asyncio.to_thread(remove_tree, staging)


def _looks_like_plugin(root: Path) -> bool:
    return any((root / name).is_file() for name in ("__init__.py", "__full__.py", "__nest__.py"))
