import base64
from io import BytesIO
from pathlib import Path
from base64 import b64encode
from typing import List, Union, Literal

from PIL import Image

from gsuid_core.models import Message


class MessageSegment:
    def __add__(self, other):
        return [self, other]

    @staticmethod
    def image(img: Union[str, Image.Image, bytes, Path]) -> Message:
        if isinstance(img, Image.Image):
            img = img.convert('RGB')
            result_buffer = BytesIO()
            img.save(result_buffer, format='PNG', quality=80, subsampling=0)
            img = result_buffer.getvalue()
        elif isinstance(img, bytes):
            return Message(type='image', data=f'base64://{base64.b64encode(img).decode()}')
        elif isinstance(img, Path):
            with open(str(img), 'rb') as fp:
                img = fp.read()
        else:
            if img.startswith('http'):
                return Message(type='image', data=f'link://{img}')
            if img.startswith('base64://'):
                return Message(type='image', data=img)
            with open(img, 'rb') as fp:
                img = fp.read()
        msg = Message(type='image', data=f'base64://{b64encode(img).decode()}')
        return msg

    @staticmethod
    def text(content: str) -> Message:
        return Message(type='text', data=content)

    @staticmethod
    def at(user: str) -> Message:
        return Message(type='at', data=user)

    @staticmethod
    def node(
        content_list: Union[List[Message], List[str], List[bytes]]
    ) -> Message:
        msg_list: List[Message] = []
        for msg in content_list:
            if isinstance(msg, Message):
                msg_list.append(msg)
            elif isinstance(msg, bytes):
                msg_list.append(MessageSegment.image(msg))
            else:
                if msg.startswith('base64://'):
                    msg_list.append(Message(type='image', data=msg))
                elif msg.startswith('http'):
                    msg_list.append(
                        Message(type='image', data=f'link://{msg}')
                    )
                else:
                    msg_list.append(MessageSegment.text(msg))
        return Message(type='node', data=msg_list)

    @staticmethod
    def record(content: Union[str, bytes, Path]) -> Message:
        if isinstance(content, bytes):
            pass
        elif isinstance(content, Path):
            with open(str(content), 'rb') as fp:
                content = fp.read()
        else:
            if content.startswith('base64://'):
                return Message(type='image', data=content)
            with open(content, 'rb') as fp:
                content = fp.read()
        return Message(type='record', data=f'base64://{content}')

    @staticmethod
    def file(content: Union[Path, str, bytes], file_name: str) -> Message:
        if isinstance(content, Path):
            with open(str(content), 'rb') as fp:
                file = fp.read()
        elif isinstance(content, bytes):
            file = content
        else:
            if content.startswith('http'):
                link = content
                return Message(
                    type='file',
                    data=f'{file_name}|link://{link}',
                )
            else:
                with open(content, 'rb') as fp:
                    file = fp.read()
        return Message(
            type='file',
            data=f'{file_name}|{b64encode(file).decode()}',
        )

    @staticmethod
    def log(
        type: Literal['INFO', 'WARNING', 'ERROR', 'SUCCESS'], content: str
    ) -> Message:
        return Message(type=f'log_{type}', data=content)
