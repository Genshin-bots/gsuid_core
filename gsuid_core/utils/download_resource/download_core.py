import os
import time
import asyncio
import concurrent.futures
from typing import Dict, Optional
from pathlib import Path
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .download_file import download

global_tag, global_url = "", ""
NOW_SPEED_TEST = False
_SPEED_TEST_DONE = False  # 标记是否已完成过一次测速（即使结果为空也不再重复）


def _sync_check_url(tag: str, url: str):
    """同步版测速，在线程池中并发执行，不受 event loop 阻塞影响"""
    try:
        start_time = time.time()
        with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)) as client:
            response = client.get(url)
            elapsed_time = time.time() - start_time
            if response.status_code == 200 and "Index of /" in response.text:
                logger.debug(
                    t("⌛ [测速] {tag} {url} 延时: {elapsed_time}", tag=tag, url=url, elapsed_time=elapsed_time)
                )
                return tag, url, elapsed_time
            else:
                logger.info(t("⚠  {tag} {url} 未超时但失效...", tag=tag, url=url))
                return tag, url, float("inf")
    except Exception as e:
        logger.debug(t("⚠  {tag} {url} 连接失败: {p0}", tag=tag, url=url, p0=type(e).__name__))
        return tag, url, float("inf")


def _blocking_find_fastest(urls: Dict[str, str]):
    """
    纯同步函数：在线程池内并发测速，wait 最多 10 秒。
    由 run_in_executor 调用，完全不占用 event loop 线程。
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(urls)) as executor:
        futures = {executor.submit(_sync_check_url, tag, url): tag for tag, url in urls.items()}
        done, not_done = concurrent.futures.wait(futures.keys(), timeout=35.0)
        for f in not_done:
            f.cancel()
        if not_done:
            logger.warning(t("[测速] {p0} 个节点测速超时，使用已完成结果", p0=len(not_done)))

    fastest_tag, fastest_url, fastest_time = "", "", float("inf")
    for f in done:
        try:
            tag, url, elapsed_time = f.result()
            if elapsed_time < fastest_time:
                fastest_tag, fastest_url, fastest_time = tag, url, elapsed_time
        except Exception:
            continue

    return fastest_tag, fastest_url


async def find_fastest_url(urls: Dict[str, str]):
    """
    异步入口：把同步阻塞的测速整体丢进 executor，
    event loop 在此期间完全不阻塞，可以正常调度其他协程。
    """
    loop = asyncio.get_event_loop()
    fastest_tag, fastest_url = await loop.run_in_executor(None, _blocking_find_fastest, urls)
    return fastest_tag, fastest_url


async def check_speed():
    global global_tag, global_url, NOW_SPEED_TEST, _SPEED_TEST_DONE

    # 已测速过（不管结果是否为空），直接返回缓存值，不再重复测速
    if _SPEED_TEST_DONE:
        return global_tag, global_url

    # 第一个到达的协程负责测速
    if not NOW_SPEED_TEST:
        NOW_SPEED_TEST = True
        logger.info(t("[GsCore资源下载]测速中..."))

        URL_LIB = {
            "[CNJS]": "http://cn-js-nj-1.lcf.icu:13214",
            "[TW]": "http://tw-taipei-1.lcf.icu:20532",
            "[SG]": "http://sg-1.lcf.icu:12588",
            "[US]": "http://us-lax-2.lcf.icu:12588",
            "[Azure SG]": "https://sg-2.qxqx.cf",
            "[Oracle KR]": "https://kr.qxqx.cf",
            "[Oracle JP]": "https://jp.qxqx.cf",
            "[MiniGG]": "http://file.minigg.cn/sayu-bot",
            "[Lulu]": "http://lulush.microgg.cn",
            "[TakeyaYuki]": "https://gscore.focalors.com",
            "[Elysia]": "https://silverwing.elysia.beauty",
        }

        TAG, BASE_URL = await find_fastest_url(URL_LIB)
        global_tag, global_url = TAG, BASE_URL
        _SPEED_TEST_DONE = True  # 无论结果如何，标记为已完成
        NOW_SPEED_TEST = False

        if TAG:
            logger.info(t("🚀 最快资源站: {TAG} {BASE_URL}", TAG=TAG, BASE_URL=BASE_URL))
        else:
            logger.warning(t("[测速] 未找到可用资源站，资源下载功能将不可用"))

        return TAG, BASE_URL

    # 其他协程等待测速完成
    while NOW_SPEED_TEST:
        await asyncio.sleep(0.5)

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
        logger.warning(f"Unsupported protocol error occurred while fetching {url}: {exc}")
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
    url = f"{PLUGIN_RES}/{endpoint}"
    if not url.endswith("/"):
        url += "/"

    if endpoint not in EPATH_MAP:
        if endpoint.endswith("/"):
            _endpoint = endpoint[:-1]
        _et = _endpoint.rsplit("/", 1)
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
    content_bs = BeautifulSoup(base_data, "lxml")
    pre_data_list = content_bs.find_all("pre")
    if not pre_data_list:
        logger.warning(t("{TAG} {endpoint} 页面中未找到 <pre> 标签!", TAG=TAG, endpoint=endpoint))
        return
    pre_data = pre_data_list[0]
    from bs4 import Tag

    if not isinstance(pre_data, Tag):
        logger.warning(t("{TAG} {endpoint} <pre> 标签不是有效的 Tag 对象!", TAG=TAG, endpoint=endpoint))
        return
    data_list = pre_data.find_all("a")
    size_list = [i for i in content_bs.strings]

    logger.trace(t("{TAG} 数据库 {endpoint} 中存在 {p0} 个内容!", TAG=TAG, endpoint=endpoint, p0=len(data_list)))

    temp_num = 0
    size_temp = 0
    for index, data in enumerate(data_list):
        if data["href"] == "../":  # type: ignore
            continue
        file_url = f"{url}{data['href']}"  # type: ignore
        name: str = unquote(file_url.split("/")[-1])
        size = size_list[index * 2 + 6].split(" ")[-1]
        _size: str = size.replace("\r", "")
        _size = _size.replace("\n", "")

        if _size == "-" or _size == "-\n":
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

        if not file_path.exists() or not os.stat(file_path).st_size or not is_diff:
            logger.info(
                t(
                    "{TAG} {plugin_name} 开始下载 {endpoint}/{name}",
                    TAG=TAG,
                    plugin_name=plugin_name,
                    endpoint=endpoint,
                    name=name,
                )
            )
            temp_num += 1
            size_temp += size
            TASK = asyncio.create_task(download(file_url, path, name, client, TAG))
            TASKS.append(TASK)
            if size_temp >= 1500000:
                await asyncio.gather(*TASKS)
                TASKS.clear()
    else:
        await asyncio.gather(*TASKS)
        TASKS.clear()

    if temp_num == 0:
        logger.trace(t("{TAG} 数据库 {endpoint} 无需下载!", TAG=TAG, endpoint=endpoint))
    else:
        logger.success(
            t("{TAG}数据库 {endpoint} 已下载{temp_num}个内容!", TAG=TAG, endpoint=endpoint, temp_num=temp_num)
        )
    temp_num = 0


async def download_all_file(
    plugin_name: str,
    EPATH_MAP: Dict[str, Path],
    URL: Optional[str] = None,
    TAG: Optional[str] = None,
):
    if URL:
        TAG, BASE_URL = TAG or "[Unknown]", URL
    else:
        TAG, BASE_URL = await check_speed()

    PLUGIN_RES = f"{BASE_URL}/{plugin_name}"
    if TAG is None:
        TAG = "[Unknown]"

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
            logger.success(t("🍱 [资源检查] 插件 {plugin_name} 资源库已是最新!", plugin_name=plugin_name))
