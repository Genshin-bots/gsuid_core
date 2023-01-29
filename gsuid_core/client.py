import asyncio
from typing import Union

import websockets.client
from model import Message, MessageReceive
from websockets.exceptions import ConnectionClosedError


class GsClient:
    @classmethod
    async def async_connect(
        cls, IP: str = 'localhost', PORT: Union[str, int] = '8765'
    ):
        self = GsClient()
        cls.ws_url = f'ws://{IP}:{PORT}/ws/Nonebot'
        print(f'连接至WS链接{self.ws_url}...')
        cls.ws = await websockets.client.connect(cls.ws_url)
        print('已成功链接！')
        return self

    async def recv_msg(self):
        try:
            async for message in self.ws:
                print(message)
        except ConnectionClosedError:
            print('断开链接...')

    async def _input(self):
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: input("请输入消息\n")
        )

    async def send_msg(self):
        while True:
            intent = await self._input()
            msg = MessageReceive(
                bot_id='Nonebot', content=[Message(type='text', data=intent)]
            )
            await self.ws.send(MessageReceive.parse_obj(msg).json())

    async def start(self):
        recv_task = asyncio.create_task(self.recv_msg())
        send_task = asyncio.create_task(self.send_msg())
        _, pending = await asyncio.wait(
            [recv_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


async def main():
    client = await GsClient().async_connect()
    await client.start()


asyncio.run(main())
