import re
from typing import Dict, List, Tuple, Optional

import aiohttp
from git.repo import Repo

from gsuid_core.logger import logger

from .api import PLUGINS_PATH, proxy_url, plugins_lib

plugins_list: Dict[str, str] = {}


async def refresh_list() -> List[str]:
    refresh_list = []
    async with aiohttp.ClientSession() as session:
        logger.info(f'稍等...开始刷新插件列表, 地址: {plugins_lib}')
        async with session.get(plugins_lib) as resp:
            content = await resp.text()
            _plugins_list: List[Tuple[str, str]] = re.findall(
                r'\[([^]]+)\]\(([^)]+)\)', content
            )
            for i in _plugins_list:
                if i[0].lower() not in plugins_list:
                    refresh_list.append(i[0])
                    logger.info(f'[刷新插件列表] 列表新增插件 {i[0]}')
                plugins_list[i[0].lower()] = i[1]
    return refresh_list


async def get_plugins_url(name: str) -> Optional[str]:
    if not plugins_list:
        await refresh_list()

    if name in plugins_list:
        return plugins_list[name]
    else:
        for _n in plugins_list:
            sim = len(set(_n) & set(name))
            if sim >= 0.5 * len(_n):
                return plugins_list[_n]
        else:
            return None


def install_plugins(url: str) -> bool:
    plugin_name = url.split('/')[-1]
    git_path = f'{proxy_url}{url}.git'
    logger.info(f'稍等...开始安装插件, 地址: {git_path}')
    Repo.clone_from(
        git_path, PLUGINS_PATH / plugin_name, single_branch=True, depth=1
    )
    return True
