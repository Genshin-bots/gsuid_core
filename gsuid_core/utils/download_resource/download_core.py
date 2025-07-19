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
NOW_SPEED_TEST = False


async def check_url(tag: str, url: str):
    async with httpx.AsyncClient() as client:
        try:
            start_time = time.time()
            response = await client.get(url)
            elapsed_time = time.time() - start_time
            if response.status_code == 200:
                if 'Index of /' in response.text:
                    logger.debug(f'⌛ [测速] {tag} {url} 延时: {elapsed_time}')
                    return tag, url, elapsed_time
                else:
                    logger.info(f'⚠  {tag} {url} 未超时但失效...')
                    return tag, url, float('inf')
            else:
                logger.info(f'⚠  {tag} {url} 超时...')
                return tag, url, float('inf')
        except aiohttp.ClientError:
            logger.info(f'⚠  {tag} {url} 超时...')
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
    global global_tag
    global global_url
    global NOW_SPEED_TEST

    if (not global_tag or not global_url) and not NOW_SPEED_TEST:
        NOW_SPEED_TEST = True
        logger.info('[GsCore资源下载]测速中...')

        URL_LIB = {
            '[CNJS]': 'http://cn-js-nj-1.lcf.icu:13214',
            '[TW]': 'http://tw-taipei-1.lcf.icu:20532',
            '[SG]': 'http://sg-1.lcf.icu:12588',
            '[US]': 'http://us-lax-2.lcf.icu:12588',
            '[Chuncheon]': 'https://kr.qxqx.cf',
            '[Seoul]': 'https://kr-s.qxqx.cf',
            '[Singapore]': 'https://sg.qxqx.cf',
            '[MiniGG]': 'http://file.minigg.cn/sayu-bot',
            '[Lulu]': 'http://lulush.microgg.cn',
            '[TakeyaYuki]': 'https://gscore.focalors.com',
        }

        TAG, BASE_URL = await find_fastest_url(URL_LIB)
        global_tag, global_url = TAG, BASE_URL

        logger.info(f"🚀 最快资源站: {TAG} {BASE_URL}")
        NOW_SPEED_TEST = False
        return TAG, BASE_URL

    if NOW_SPEED_TEST:
        while True:
            if not NOW_SPEED_TEST:
                return global_tag, global_url
            await asyncio.sleep(1)

    return global_tag, global_url


async def _get_url(url: str, client: httpx.AsyncClient) -> bytes:
    try:
        response = await client.get(url)
        return response.read()
    except httpx.HTTPStatusError as exc:
        logger.warning(f"HTTP error occurred while fetching {url}: {exc}")
        return b""
    except httpx.ConnectError as exc:
        logger.warning(f"Connect error occurred while fetching {url}: {exc}")
        return b""
    except httpx.UnsupportedProtocol as exc:
        logger.warning(
            f"Unsupported protocol error occurred while fetching {url}: {exc}"
        )
        return b""
    except httpx.RequestError as exc:
        logger.warning(f"Request error occurred while fetching {url}: {exc}")
        return b""


async def download_atag_file(
    PLUGIN_RES: str,
    endpoint: str,
    EPATH_MAP: Dict[str, Path],
    client: httpx.AsyncClient,
    TAG: str,
    plugin_name: str,
):
    TASKS = []
    url = f'{PLUGIN_RES}/{endpoint}'
    if not url.endswith('/'):
        url += '/'

    if endpoint not in EPATH_MAP:
        if endpoint.endswith('/'):
            _endpoint = endpoint[:-1]
        _et = _endpoint.rsplit('/', 1)
        _e = _et[0]
        _t = _et[1]
        if _e in EPATH_MAP:
            path = EPATH_MAP[_e] / _t
        else:
            return
    else:
        path = EPATH_MAP[endpoint]

    if not path.exists():
        path.mkdir(parents=True)

    base_data = await _get_url(url, client)
    content_bs = BeautifulSoup(base_data, 'lxml')
    pre_data_list = content_bs.find_all('pre')
    if not pre_data_list:
        logger.warning(f'{TAG} {endpoint} 页面中未找到 <pre> 标签!')
        return
    pre_data = pre_data_list[0]
    from bs4 import Tag

    if not isinstance(pre_data, Tag):
        logger.warning(f'{TAG} {endpoint} <pre> 标签不是有效的 Tag 对象!')
        return
    data_list = pre_data.find_all('a')
    size_list = [i for i in content_bs.strings]

    logger.trace(f'{TAG} 数据库 {endpoint} 中存在 {len(data_list)} 个内容!')

    temp_num = 0
    size_temp = 0
    for index, data in enumerate(data_list):
        if data['href'] == '../':  # type: ignore
            continue
        file_url = f'{url}{data["href"]}'  # type: ignore
        name: str = unquote(file_url.split('/')[-1])
        size = size_list[index * 2 + 6].split(' ')[-1]
        _size: str = size.replace('\r', '')
        _size = _size.replace('\n', '')

        if _size == '-' or _size == '-\n':
            await download_atag_file(
                PLUGIN_RES,
                f"{endpoint}/{data['href']}",  # type: ignore
                EPATH_MAP,
                client,
                TAG,
                plugin_name,
            )
            continue

        if _size.isdigit():
            size = int(_size)
        else:
            continue

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
    else:
        logger.success(f'{TAG}数据库 {endpoint} 已下载{temp_num}个内容!')
    temp_num = 0


async def download_all_file(
    plugin_name: str,
    EPATH_MAP: Dict[str, Path],
    URL: Optional[str] = None,
    TAG: Optional[str] = None,
):
    if URL:
        TAG, BASE_URL = TAG or '[Unknown]', URL
    else:
        TAG, BASE_URL = await check_speed()
        PLUGIN_RES = f'{BASE_URL}/{plugin_name}'

    if TAG is None:
        TAG = '[Unknown]'

    async with httpx.AsyncClient(timeout=httpx.Timeout(200.0)) as client:
        n = 0
        for endpoint in EPATH_MAP:
            await download_atag_file(
                PLUGIN_RES,
                endpoint,
                EPATH_MAP,
                client,
                TAG,
                plugin_name,
            )
            n += 1

        if n == len(EPATH_MAP):
            logger.success(f'🍱 [资源检查] 插件 {plugin_name} 资源库已是最新!')
