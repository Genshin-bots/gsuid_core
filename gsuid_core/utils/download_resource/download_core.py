import os
import time
import asyncio
from pathlib import Path
from urllib.parse import unquote
from typing import Dict, Optional

import httpx
import aiohttp
from bs4 import BeautifulSoup

from gsuid_core.logger import logger

from .download_file import download

global_tag, global_url = '', ''


async def check_url(tag: str, url: str):
    async with httpx.AsyncClient() as client:
        try:
            start_time = time.time()
            response = await client.get(url)
            elapsed_time = time.time() - start_time
            if response.status_code == 200:
                if 'Index of /' in response.text:
                    logger.debug(f'{tag} {url} 延时: {elapsed_time}')
                    return tag, url, elapsed_time
                else:
                    logger.info(f'{tag} {url} 未超时但失效...')
                    return tag, url, float('inf')
            else:
                logger.info(f'{tag} {url} 超时...')
                return tag, url, float('inf')
        except aiohttp.ClientError:
            logger.info(f'{tag} {url} 超时...')
            return tag, url, float('inf')


async def find_fastest_url(urls: Dict[str, str]):
    tasks = []
    for tag in urls:
        tasks.append(asyncio.create_task(check_url(tag, urls[tag])))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    fastest_tag: str = ''
    fastest_url: str = ''
    fastest_time = float('inf')

    for result in results:
        if isinstance(result, (Exception, BaseException)):
            continue
        tag, url, elapsed_time = result
        if elapsed_time < fastest_time:
            fastest_url = url
            fastest_time = elapsed_time
            fastest_tag = tag

    return fastest_tag, fastest_url


async def check_speed():
    logger.info('[GsCore资源下载]测速中...')

    global global_tag
    global global_url

    URL_LIB = {
        '[JPFRP]': 'http://jp-3.lcf.1l1.icu:17217',
        '[HKFRP]': 'http://hk-1.lcf.1l1.icu:10200',
        '[USFRP]': 'http://us-6.lcf.1l1.icu:28596',
        '[XiaoWu]': 'http://frp.xiaowuap.com:63481',
        '[Chuncheon]': 'https://kr.qxqx.cf',
        '[Seoul]': 'https://kr-s.qxqx.cf',
        '[Singapore]': 'https://sg.qxqx.cf',
    }

    TAG, BASE_URL = await find_fastest_url(URL_LIB)
    global_tag, global_url = TAG, BASE_URL

    logger.info(f"最快资源站: {TAG} {BASE_URL}")
    return TAG, BASE_URL


async def _get_url(url: str, client: httpx.AsyncClient) -> bytes:
    try:
        response = await client.get(url)
        return response.read()
    except httpx.HTTPStatusError as exc:
        print(f"HTTP error occurred while fetching {url}: {exc}")
        return b""


async def download_all_file(
    plugin_name: str,
    EPATH_MAP: Dict[str, Path],
    URL: Optional[str] = None,
    TAG: Optional[str] = None,
):
    global global_tag, global_url

    if URL:
        TAG, BASE_URL = TAG or '[Unknown]', URL
    else:
        TAG, BASE_URL = await check_speed()
        PLUGIN_RES = f'{BASE_URL}/{plugin_name}'

    if TAG is None:
        TAG = '[Unknown]'

    TASKS = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(200.0)) as client:
        n = 0
        for endpoint in EPATH_MAP:
            url = f'{PLUGIN_RES}/{endpoint}/'
            path = EPATH_MAP[endpoint]

            if not path.exists():
                path.mkdir(parents=True)

            base_data = await _get_url(url, client)
            content_bs = BeautifulSoup(base_data, 'lxml')
            pre_data = content_bs.find_all('pre')[0]
            data_list = pre_data.find_all('a')
            size_list = [i for i in content_bs.strings]

            logger.trace(
                f'{TAG} 数据库 {endpoint} 中存在 {len(data_list)} 个内容!'
            )

            temp_num = 0
            size_temp = 0
            for index, data in enumerate(data_list):
                if data['href'] == '../':
                    continue
                file_url = f'{url}{data["href"]}'
                name: str = unquote(file_url.split('/')[-1])
                size = size_list[index * 2 + 6].split(' ')[-1]
                size = int(size.replace('\r\n', ''))
                file_path = path / name

                if file_path.exists():
                    is_diff = size == os.stat(file_path).st_size
                else:
                    is_diff = True

                if (
                    not file_path.exists()
                    or not os.stat(file_path).st_size
                    or not is_diff
                ):
                    logger.info(
                        f'{TAG} {plugin_name} 开始下载 {endpoint}/{name}'
                    )
                    temp_num += 1
                    size_temp += size
                    TASK = asyncio.create_task(
                        download(file_url, path, name, client, TAG)
                    )
                    TASKS.append(TASK)
                    if size_temp >= 1500000:
                        await asyncio.gather(*TASKS)
                        TASKS.clear()
            else:
                await asyncio.gather(*TASKS)
                TASKS.clear()

            if temp_num == 0:
                logger.trace(f'{TAG} 数据库 {endpoint} 无需下载!')
                n += 1
            else:
                logger.success(
                    f'{TAG}数据库 {endpoint} 已下载{temp_num}个内容!'
                )
            temp_num = 0

        if n == len(EPATH_MAP):
            logger.success(f'插件 {plugin_name} 资源库已是最新!')
