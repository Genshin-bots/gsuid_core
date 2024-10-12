import os
import re
import time
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, List, Union, Optional
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from git.repo import Repo
from git.exc import GitCommandError, NoSuchPathError, InvalidGitRepositoryError

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

from .api import CORE_PATH, PLUGINS_PATH, plugins_lib

plugins_list: Dict[str, Dict[str, str]] = {}

start_venv: str = core_plugins_config.get_config('StartVENV').data
is_install_dep = core_plugins_config.get_config('AutoInstallDep').data


def check_start_tool(is_pip: bool = False):
    PDM = 'pdm'
    POETRY = 'poetry'
    OTHER = start_venv.strip()

    if is_pip:
        PIP = ' run python -m pip'
        PDM += PIP
        POETRY += ' run pip'

        if OTHER == 'python':
            OTHER = 'python -m pip'
        else:
            OTHER += PIP

    path = Path(__file__).parent.parent.parent.parent
    pdm_python_path = path / '.pdm-python'

    if start_venv == 'auto':
        if pdm_python_path.exists():
            command = PDM
        else:
            command = POETRY
    elif start_venv == 'pdm':
        command = PDM
    elif start_venv == 'poetry':
        command = POETRY
    else:
        command = start_venv.strip()

    return command


async def check_plugin_exist(name: str):
    name = name.lower()
    if name in ['core_command', 'gs_test']:
        return '内置插件不可删除！'
    for i in PLUGINS_PATH.iterdir():
        if i.stem.lower() == name:
            return i


async def uninstall_plugin(path: Path):
    if not path.exists():
        return '该插件不存在!'
    path.unlink()
    return '删除成功!'


# 传入一个path对象
def run_install(path: Optional[Path] = None) -> int:
    tools = check_start_tool()
    if tools == 'pip':
        logger.warning('你使用的是PIP环境, 无需进行 PDM/Poetry install!')
        return -200

    if path is None:
        path = CORE_PATH

    # 检测path是否是一个目录
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")

    # 异步执行poetry install命令，并返回返回码
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf8"

    proc = subprocess.run(
        f'{tools} install',
        cwd=path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
        env=env,
        encoding='utf-8',
        text=True,
    )

    output = proc.stdout  # 获取输出
    error = proc.stderr  # 获取错误信息

    logger.info(output)
    if error:
        logger.error(error)

    retcode = -1 if proc.returncode is None else proc.returncode
    if 'No dependencies to install or update' in output:
        retcode = 200
    return retcode


def check_retcode(retcode: int) -> str:
    if retcode == 200:
        return '无需更新依赖！'
    elif retcode == 0:
        return '新增/更新依赖成功!'
    else:
        return f'更新失败, 错误码{retcode}'


async def update_all_plugins(level: int = 0) -> List[str]:
    log_list = []
    for plugin in PLUGINS_PATH.iterdir():
        if _is_plugin(plugin):
            log_list.extend(await update_from_git_in_tread(level, plugin))
    return log_list


def _is_plugin(plugin: Path) -> bool:
    if (
        plugin.is_dir()
        and plugin.name != '__pycache__'
        and plugin.name != 'core_command'
    ):
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
    global plugins_list
    refresh_list = []
    async with aiohttp.ClientSession() as session:
        logger.trace(f'稍等...开始刷新插件列表, 地址: {plugins_lib}')
        async with session.get(plugins_lib) as resp:
            _plugins_list: Dict[str, Dict[str, Dict[str, str]]] = (
                await resp.json()
            )
            for i in _plugins_list['plugins']:
                if i.lower() not in plugins_list:
                    refresh_list.append(i)
                    logger.debug(f'[刷新插件列表] 列表新增插件 {i}')
                plugins_list[i.lower()] = _plugins_list['plugins'][i]
    return refresh_list


async def get_plugins_list() -> Dict[str, Dict[str, str]]:
    if not plugins_list:
        await refresh_list()
    return plugins_list


async def get_plugins_url(name: str) -> Optional[Dict[str, str]]:
    if not plugins_list:
        await refresh_list()

    if name in plugins_list:
        return plugins_list[name]
    else:
        for _n in plugins_list:
            if name.lower() in _n:
                return plugins_list[_n]
        else:
            return None


def install_plugins(plugins: Dict[str, str]) -> str:
    proxy_url: str = core_plugins_config.get_config('ProxyURL').data

    plugin_name = plugins['link'].split('/')[-1]
    if proxy_url and not proxy_url.endswith('/'):
        _proxy_url = proxy_url + '/'
    else:
        _proxy_url = proxy_url
    git_path = f'{_proxy_url}{plugins["link"]}.git'
    logger.info(f'稍等...开始安装插件, 地址: {git_path}')
    path = PLUGINS_PATH / plugin_name
    if path.exists():
        return '该插件已经安装过了!'
    config = {'single_branch': True, 'depth': 1}

    if plugins['branch'] != 'main':
        config['branch'] = plugins['branch']

    Repo.clone_from(git_path, path, **config)
    logger.info(f'插件{plugin_name}安装成功!')
    return f'插件{plugin_name}安装成功!发送[gs重启]以应用!'


async def install_plugin(plugin_name: str) -> int:
    url = await get_plugins_url(plugin_name)
    if url is None:
        return -1
    install_plugins(url)
    return 0


def check_plugins(plugin_name: str) -> Optional[Repo]:
    path = PLUGINS_PATH / plugin_name
    if path.exists():
        try:
            repo = Repo(path)
        except InvalidGitRepositoryError:
            return None
        return repo
    else:
        return None


def check_can_update(repo: Repo) -> bool:
    try:
        remote = repo.remote()  # 获取远程仓库
        remote.fetch()  # 从远程获取最新版本
    except GitCommandError as e:
        logger.error(f'发生Git命令错误{e}!')
        return False
    local_commit = repo.commit()  # 获取本地最新提交
    remote_commit = remote.fetch()[0].commit  # 获取远程最新提交
    if (
        local_commit.hexsha == remote_commit.hexsha
    ):  # 比较本地和远程的提交哈希值
        return False
    return True


async def async_check_plugins(plugin_name: str):
    path = PLUGINS_PATH / plugin_name
    if path.exists():
        cmd = 'git fetch && git status'
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(
                f'{cmd} 执行错误 {proc.returncode}: {stderr.decode()}'
            )
        if b'Your branch is up to date' in stdout:
            return 4
        elif b'not a git repository' in stdout:
            return 3
        else:
            return 1
    return 3


async def check_status(plugin_name: str) -> int:
    return await async_check_plugins(plugin_name)


async def set_proxy(repo: Path, proxy: Optional[str] = None) -> str:
    plugin_name = repo.name
    proxy_url: str = core_plugins_config.get_config('ProxyURL').data

    try:
        process = await asyncio.create_subprocess_shell(
            'git remote get-url origin',
            cwd=repo,
            stdout=asyncio.subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        logger.warning(f'[core插件设置代理] 失败, {plugin_name} 非有效Git路径')
        logger.warning(f'[core插件设置代理] 错误信息: {e}')
        return f'{plugin_name} 设置代理失败, 非有效Git路径'

    stdout, _ = await process.communicate()
    original_url: str = stdout.decode().strip()

    if 'git@' in original_url:
        logger.info(
            f'[core插件设置代理] {plugin_name} git地址为SSH, 无需设置代理'
        )
        return f'{plugin_name} 无需设置代理'

    _main_url = re.search(r"https:\/\/github[\s\S]+?git", original_url)
    if _main_url:
        main_url = _main_url[0]
    else:
        logger.info(f'[core插件设置代理] {plugin_name} 未发现有效git地址')
        return f'{plugin_name} 未发现有效git地址'

    # _proxy_url = re.search(r'^(https?:\/\/.+?)\/', original_url)

    if proxy is None:
        _proxy_url = proxy_url
    else:
        _proxy_url = proxy

    if not _proxy_url.startswith(('http', 'https')):
        return '你可能输入了一个错误的git代理地址...'

    if _proxy_url and not _proxy_url.endswith('/'):
        _proxy_url += '/'

    # 设置git代理
    new_url = f"{_proxy_url}{main_url}"

    if new_url == original_url:
        logger.info(
            f'[core插件设置代理] {plugin_name} 地址与代理地址相同，无需设置'
        )
        return f'{plugin_name} 已经设过该地址了...'

    if not await async_change_plugin_url(repo, new_url):
        return f'{plugin_name} 设置代理失败'

    return f'{plugin_name} 设置代理成功!'


async def async_change_plugin_url(repo: Path, new_url: str):
    try:
        process = await asyncio.create_subprocess_shell(
            f'git remote set-url origin {new_url}',
            cwd=repo,
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f'[core插件设置远程地址] 失败, 错误信息: {e}')
        return False


def sync_change_plugin_url(repo: Path, new_url: str):
    try:
        command = f'git remote set-url origin {new_url}'
        subprocess.run(
            command,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            check=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f'[core插件设置远程地址] 失败, 错误信息: {e}')
        return False


def sync_get_plugin_url(repo: Path) -> Optional[str]:
    try:
        command = 'git remote get-url origin'
        process = subprocess.run(
            command,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            check=True,
        )
        stdout = process.stdout
        original_url = stdout.decode().strip()
        return original_url
    except subprocess.CalledProcessError as e:
        logger.error(f'[core插件设置远程地址] 失败, 错误信息: {e}')
        return None


async def update_from_git_in_tread(
    level: int = 0,
    repo_like: Union[str, Path, None] = None,
    log_key: List[str] = [],
    log_limit: int = 5,
):
    if not hasattr(asyncio, 'to_thread'):
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            result = await loop.run_in_executor(
                executor, update_from_git, level, repo_like, log_key, log_limit
            )
    else:
        result = await asyncio.to_thread(
            update_from_git, level, repo_like, log_key, log_limit
        )
    return result


def update_from_git(
    level: int = 0,
    repo_like: Union[str, Path, None] = None,
    log_key: List[str] = [],
    log_limit: int = 5,
) -> List[str]:
    try:
        if repo_like is None:
            repo = Repo(CORE_PATH)
            plugin_name = '早柚核心'
            if is_install_dep:
                run_install(CORE_PATH)
        elif isinstance(repo_like, Path):
            repo = Repo(repo_like)
            plugin_name = repo_like.name
        else:
            repo = check_plugins(repo_like)
            plugin_name = repo_like
    except InvalidGitRepositoryError:
        logger.warning('[更新] 更新失败, 非有效Repo路径!')
        return [
            '更新失败, 该路径并不是一个有效的GitRepo路径, 请使用`git clone`安装插件...'
        ]
    except NoSuchPathError:
        logger.warning('[更新] 更新失败, 该路径不存在!')
        return ['更新失败, 路径/插件不存在!']

    if not repo:
        logger.warning('[更新] 更新失败, 该插件不存在!')
        return ['更新失败, 不存在该插件!']

    o = repo.remotes.origin

    logger.info(f'[更新] 准备更新 [{plugin_name}], 更新等级为{level}')

    # 先执行git fetch
    logger.info(f'[更新][{plugin_name}] 正在执行 git fetch')

    try:
        o.fetch()
    except GitCommandError as e:
        logger.warning(f'[更新] 执行 git fetch 失败...{e}!')
        return [
            f'更新插件 {plugin_name} 中...',
            '执行 git fetch 失败, 请检查控制台...',
        ]

    try:
        default_branch = repo.git.branch('--show-current')

        commits_diff = list(
            repo.iter_commits(f'HEAD..origin/{default_branch}')
        )
    except GitCommandError as e:
        logger.warning(f'[更新] 查找默认分支失败...{e}!')
        commits_diff = list(repo.iter_commits(max_count=40))

    if level >= 2:
        logger.warning(f'[更新][{plugin_name}] 正在执行 git clean --xdf')
        logger.warning('[更新] 你有 2 秒钟的时间中断该操作...')
        if plugin_name == '早柚核心':
            return ['更新失败, 禁止强行强制更新核心...']
        time.sleep(2)
        repo.git.clean('-xdf')
    # 还原上次更改
    if level >= 1:
        logger.warning(f'[更新][{plugin_name}] 正在执行 git reset --hard')
        repo.git.reset('--hard')

    try:
        pull_log = o.pull()
        logger.info(f'[更新][{plugin_name}] {pull_log}')
        logger.info(f'[更新][{repo.head.commit.hexsha[:7]}] 获取远程最新版本')
    except GitCommandError as e:
        logger.warning(f'[更新] 更新失败...{e}!')
        return [f'更新插件 {plugin_name} 中...', '更新失败, 请检查控制台...']

    # commits = list(repo.iter_commits(max_count=40))
    if commits_diff:
        commits = commits_diff
    else:
        commits = []
    log_list = []
    if commits:
        log_list.append(f'✅本次插件 {plugin_name} , 更新内容如下：')
        for commit in commits:
            if isinstance(commit.message, str):
                if log_key:
                    for key in log_key:
                        if key in commit.message:
                            log_list.append(commit.message.replace('\n', ''))
                            if len(log_list) >= log_limit:
                                break
                else:
                    log_list.append(commit.message.replace('\n', ''))
                    if len(log_list) >= log_limit:
                        break
    else:
        log_list.append(f'✅插件 {plugin_name} 本次无更新内容！')
    return log_list


async def update_plugins(
    plugin_name: str,
    level: int = 0,
    log_key: List[str] = [],
    log_limit: int = 10,
) -> Union[str, List]:
    if not plugin_name:
        return '请后跟有效的插件名称！\n例如：core更新插件genshinuid'
    for _n in PLUGINS_PATH.iterdir():
        _name = _n.name
        sim = len(set(_name.lower()) & set(plugin_name.lower()))
        if sim >= 0.85 * len(_name):
            plugin_name = _name
            break

    log_list = await update_from_git_in_tread(
        level, plugin_name, log_key, log_limit
    )
    return log_list
    return log_list
