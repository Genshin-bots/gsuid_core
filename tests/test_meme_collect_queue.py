"""表情包采集：H00 hook 只做入队，下载/入库由后台 worker 消费。

回归 fe331524 的 bug：observe 曾在 ON_INBOUND 200ms 预算内内联下载图片，
被 wait_for 掐死；且 URL 已提前标记，同一图之后永久跳采。
"""

import asyncio
from unittest.mock import patch

from gsuid_core.models import Event, Message
from gsuid_core.ai_core.meme import observer
from gsuid_core.ai_core.meme.config import meme_config


def _ev(url: str) -> Event:
    return Event(
        group_id="g1",
        user_id="u1",
        content=[Message(type="image", data=f"link://{url}")],
    )


def test_observe_returns_within_hook_budget_and_worker_consumes() -> None:
    async def _run() -> None:
        cfg = meme_config.get_config("meme_auto_collect")
        old = cfg.data
        cfg.data = True
        got = asyncio.Event()
        seen: list[str] = []

        async def _slow_download(url: str):
            seen.append(url)
            got.set()
            await asyncio.sleep(60)
            return None

        try:
            with patch.object(observer, "_download_image", _slow_download):
                # hook 预算内必须返回：修复前内联下载会在这里被 200ms 掐死
                await asyncio.wait_for(
                    observer.observe_message_for_memes(_ev("http://m.test/a.png")),
                    timeout=0.2,
                )
                # 队列 + worker 接管下载
                await asyncio.wait_for(got.wait(), timeout=2)
                assert seen == ["http://m.test/a.png"]

                # 同 URL 重发不重复入队
                await observer.observe_message_for_memes(_ev("http://m.test/a.png"))
                await asyncio.sleep(0.05)
                assert seen == ["http://m.test/a.png"]
        finally:
            cfg.data = old
            await observer.stop_collect_worker()

    asyncio.run(_run())


def test_queue_full_drop_unmarks_url() -> None:
    async def _run() -> None:
        cfg = meme_config.get_config("meme_auto_collect")
        old = cfg.data
        cfg.data = True

        async def _noop(url: str, source_group: str, source_user: str) -> None:
            return None

        try:
            with patch.object(observer, "_process_image", _noop):
                while not observer._collect_queue.full():
                    observer._collect_queue.put_nowait(
                        observer.MemeCollectRecord(
                            url=f"http://m.test/d{observer._collect_queue.qsize()}",
                            source_group="g",
                            source_user="u",
                        )
                    )
                url = "http://m.test/dropped.png"
                await observer.observe_message_for_memes(_ev(url))
                # 入队失败必须撤销去重标记，否则该 URL 被永久跳采
                assert url not in observer._processed_urls
        finally:
            cfg.data = old
            await observer.stop_collect_worker()
            while not observer._collect_queue.empty():
                observer._collect_queue.get_nowait()

    asyncio.run(_run())
