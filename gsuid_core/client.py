import random
import asyncio
from typing import Union

import httpx
import websockets.client
from msgspec import json as msgjson, to_builtins
from websockets.exceptions import ConnectionClosedError

from gsuid_core.i18n import t
from gsuid_core.models import Message, MessageSend, MessageReceive
from gsuid_core.segment import MessageSegment


class GsClient:
    @classmethod
    async def async_connect(
        cls,
        IP: str = "localhost",
        PORT: Union[str, int, None] = None,
        WS_TOKEN: Union[str, None] = None,
    ):
        from gsuid_core.config import core_config

        if PORT is None:
            PORT = core_config.get_config("PORT") or "8765"
        if WS_TOKEN is None:
            WS_TOKEN = str(core_config.get_config("WS_TOKEN") or "abc111")
        self = GsClient()
        cls.ws_url = f"ws://{IP}:{PORT}/ws/Nonebot?token={WS_TOKEN}"
        print(t("连接至WS链接{p0}...", p0=self.ws_url))
        cls.ws = await websockets.client.connect(  # type: ignore
            cls.ws_url, max_size=2**25, open_timeout=30
        )
        print(t("已成功链接！"))
        return self

    async def recv_msg(self):
        try:
            async for message in self.ws:
                print(msgjson.decode(message, type=MessageSend))
        except ConnectionClosedError:
            print(t("断开连接...等待5秒，尝试重连中..."))
            await asyncio.sleep(5)
            client = await self.async_connect()
            await client.start()

    async def _input(self):
        return await asyncio.get_event_loop().run_in_executor(None, lambda: input("请输入消息\n"))

    async def send_msg(self):
        while True:
            intent = await self._input()
            content = Message(type="text", data=intent)
            group_id = random.choice(["8888", "88888"])
            user_id = random.choice(["99999"])
            msg = MessageReceive(
                bot_id="console",
                # bot_id='qqgroup',
                bot_self_id="3399214199",
                user_type="direct",
                user_pm=0,
                group_id=group_id,
                user_id=user_id,
                content=[content],
            )
            msg_send = msgjson.encode(msg)
            await self.ws.send(msg_send)

    async def start(self):
        recv_task = asyncio.create_task(self.recv_msg())
        send_task = asyncio.create_task(self.send_msg())
        _, pending = await asyncio.wait(
            [recv_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


async def http_test(PORT: Union[str, int, None] = None):
    msg = to_builtins(
        MessageReceive(
            content=[
                MessageSegment.text("强制刷新"),
            ]
        )
    )

    from gsuid_core.config import core_config

    if PORT is None:
        PORT = core_config.get_config("PORT") or "8765"
    ws_token = str(core_config.get_config("WS_TOKEN") or "")
    headers = {"X-WS-Token": ws_token} if ws_token else {}

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"http://127.0.0.1:{PORT}/api/send_msg",
            json=msg,
            headers=headers,
        )
        print(response.text)
        print(response.status_code)


async def main():
    # await http_test()
    client = await GsClient().async_connect()
    await client.start()


if __name__ == "__main__":
    asyncio.run(main())
