import os
import time
import asyncio
from pathlib import Path
from typing import Dict, Optional

import aiohttp
from bs4 import BeautifulSoup
from aiohttp import TCPConnector
from aiohttp.client import ClientSession, ClientTimeout

from gsuid_core.logger import logger

from .download_file import download


async def check_url(tag: str, url: str):
    async with aiohttp.ClientSession() as session:
        try:
            start_time = time.time()
            async with session.get(url) as response:
                elapsed_time = time.time() - start_time
                if response.status == 200:
                    logger.info(f'{tag} {url} 延时: {elapsed_time}')
                    return tag, url, elapsed_time
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
        if isinstance(result, Exception):
            continue
        tag, url, elapsed_time = result
        if elapsed_time < fastest_time:
            fastest_url = url
            fastest_time = elapsed_time
            fastest_tag = tag

    return fastest_tag, fastest_url


async def check_speed():
    logger.info('[GsCore资源下载]测速中...')

    URL_LIB = {
        '[JPFRP]': 'http://jp-2.lcf.icu:13643',
        '[HKFRP]': 'http://hk-1.5gbps-2.lcf.icu:10200',
        '[qianxu-jp]': 'https://jp.qxqx.me',
        '[qianxu-kr]': 'https://kr-arm.qxqx.me',
    }

    TAG, BASE_URL = await find_fastest_url(URL_LIB)
    logger.info(f"最快资源站: {TAG} {BASE_URL}")
    return TAG, BASE_URL


async def _get_url(url: str, sess: ClientSession):
    req = await sess.get(url=url)
    return await req.read()


async def download_all_file(
    plugin_name: str,
    EPATH_MAP: Dict[str, Path],
    URL: Optional[str] = None,
    TAG: Optional[str] = None,
):
    if URL is None:
        TAG, BASE_URL = await check_speed()

        PLUGIN_RES = f'{BASE_URL}/{plugin_name}'
    else:
        PLUGIN_RES = f'{URL}/{plugin_name}'

    if TAG is None:
        TAG = '[Unknown]'

    TASKS = []
    async with ClientSession(
        connector=TCPConnector(verify_ssl=False),
        timeout=ClientTimeout(total=None, sock_connect=20, sock_read=200),
    ) as sess:
        for endpoint in EPATH_MAP:
            url = f'{PLUGIN_RES}/{endpoint}/'
            path = EPATH_MAP[endpoint]

            base_data = await _get_url(url, sess)
            content_bs = BeautifulSoup(base_data, 'lxml')
            pre_data = content_bs.find_all('pre')[0]
            data_list = pre_data.find_all('a')
            size_list = [i for i in content_bs.strings]
            logger.info(f'{TAG} 数据库 {endpoint} 中存在 {len(data_list)} 个内容!')

            temp_num = 0
            size_temp = 0
            for index, data in enumerate(data_list):
                if data['href'] == '../':
                    continue
                file_url = f'{url}{data["href"]}'
                name: str = data.text
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
                    logger.info(f'{TAG} {plugin_name} 开始下载 {endpoint}/{name}')
                    temp_num += 1
                    size_temp += size
                    TASKS.append(
                        asyncio.wait_for(
                            download(file_url, path, name, sess, TAG),
                            timeout=600,
                        )
                    )
                    if size_temp >= 1500000:
                        await asyncio.gather(*TASKS)
                        TASKS.clear()
            else:
                await asyncio.gather(*TASKS)
                TASKS.clear()

            if temp_num == 0:
                im = f'{TAG} 数据库 {endpoint} 无需下载!'
            else:
                im = f'{TAG}数据库 {endpoint} 已下载{temp_num}个内容!'
            temp_num = 0
            logger.info(im)
