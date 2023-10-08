import asyncio
from typing import Union

import websockets.client
from msgspec import json as msgjson
from models import Message, MessageSend, MessageReceive
from websockets.exceptions import ConnectionClosedError


class GsClient:
    @classmethod
    async def async_connect(
        cls, IP: str = 'localhost', PORT: Union[str, int] = '8765'
    ):
        self = GsClient()
        cls.ws_url = f'ws://{IP}:{PORT}/ws/Nonebot'
        print(f'连接至WS链接{self.ws_url}...')
        cls.ws = await websockets.client.connect(cls.ws_url, max_size=2**25)
        print('已成功链接！')
        return self

    async def recv_msg(self):
        try:
            async for message in self.ws:
                print(msgjson.decode(message, type=MessageSend))
        except ConnectionClosedError:
            print('断开链接...')

    async def _input(self):
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: input("请输入消息\n")
        )

    async def send_msg(self):
        while True:
            intent = await self._input()
            if intent == '图片测试':
                content = [
                    Message(
                        type='file',
                        data='xxx.json|XAclpWfLF5d66dtrHx8cqq8E+',
                    )
                ]
            else:
                content = [Message(type='text', data=intent)]
            msg = MessageReceive(
                bot_id='Nonebot222',
                user_type='direct',
                user_pm=1,
                group_id=None,
                user_id='511',
                content=content,
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


async def main():
    client = await GsClient().async_connect()
    await client.start()


asyncio.run(main())
