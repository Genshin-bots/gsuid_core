import uuid
import random
from io import BytesIO
from pathlib import Path
from base64 import b64decode, b64encode
from typing import List, Tuple, Union, Literal, Optional

import msgspec
from PIL import Image

from gsuid_core.models import Message
from gsuid_core.data_store import image_res
from gsuid_core.message_models import Button
from gsuid_core.utils.image.convert import text2pic
from gsuid_core.utils.plugins_config.gs_config import (
    pic_upload_config,
    core_plugins_config,
)

R_enabled = core_plugins_config.get_config('AutoAddRandomText').data
R_text = core_plugins_config.get_config('RandomText').data
is_text2pic = core_plugins_config.get_config('AutoTextToPic').data
text2pic_limit = core_plugins_config.get_config('TextToPicThreshold').data
enable_pic_srv = core_plugins_config.get_config('EnablePicSrv').data
pic_srv = core_plugins_config.get_config('PicSrv').data
SERVER = pic_upload_config.get_config('PicUploadServer').data
IS_UPLOAD = pic_upload_config.get_config('PicUpload').data


pclient = None
if IS_UPLOAD:
    if SERVER == 'smms':
        from gsuid_core.utils.upload.smms import SMMS

        pclient = SMMS()
    elif SERVER == 's3':
        from gsuid_core.utils.upload.s3 import S3

        pclient = S3()


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
            pass
        elif isinstance(img, Path):
            with open(str(img), 'rb') as fp:
                img = fp.read()
        elif isinstance(img, (bytearray, memoryview)):
            img = bytes(img)
        else:
            if img.startswith('http'):
                return Message(type='image', data=f'link://{img}')
            if img.startswith('base64://'):
                return Message(type='image', data=img)
            with open(img, 'rb') as fp:
                img = fp.read()

        if enable_pic_srv:
            name = f'{uuid.uuid1()}.jpg'
            path = image_res / name
            path.write_bytes(img)
            data = f'link://{pic_srv}/genshinuid/image/{name}'
        else:
            data = f'base64://{b64encode(img).decode()}'

        msg = Message(type='image', data=data)
        return msg

    @staticmethod
    def text(content: str) -> Message:
        return Message(type='text', data=content)

    @staticmethod
    def markdown(
        content: str,
        buttons: Optional[Union[List[Button], List[List[Button]]]] = None,
    ) -> List[Message]:
        data = [Message(type='markdown', data=content)]
        if buttons:
            data.append(
                Message(type='buttons', data=msgspec.to_builtins(buttons))
            )

        return data

    @staticmethod
    def image_size(size: Tuple[int, int]) -> Message:
        return Message(type='image_size', data=size)

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
            elif isinstance(msg, (bytearray, memoryview)):
                continue
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
        elif isinstance(content, (bytearray, memoryview)):
            content = bytes(content)
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
        elif isinstance(content, (bytearray, memoryview)):
            file = bytes(content)
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


async def _convert_message(
    message: Union[Message, str, bytes]
) -> List[Message]:
    if isinstance(message, Message):
        if message.type == 'image' and pclient is not None:
            img_text: str = message.data  # type: ignore
            bio = BytesIO(b64decode(img_text[9:]))
            img = Image.open(bio)
            img_url = await pclient.upload(f'{uuid.uuid4()}.jpg', bio)
            _message = [
                MessageSegment.image(img_url if img_url else img_text),
                MessageSegment.image_size(img.size),
            ]
        else:
            _message = [message]
    elif isinstance(message, str):
        if message.startswith('base64://'):
            bio = BytesIO(b64decode(message[9:]))
            img = Image.open(bio)

            img_url = None
            if pclient is not None:
                img_url = await pclient.upload(f'{uuid.uuid4()}.jpg', bio)

            _message = [
                MessageSegment.image(img_url if img_url else message),
                MessageSegment.image_size(img.size),
            ]
        else:
            _message = [MessageSegment.text(message)]
    elif isinstance(message, bytes):
        bio = BytesIO(message)
        img = Image.open(bio)

        img_url = None
        if pclient is not None:
            img_url = await pclient.upload(f'{uuid.uuid4()}.jpg', bio)

        _message = [
            MessageSegment.image(img_url if img_url else message),
            MessageSegment.image_size(img.size),
        ]
    elif isinstance(message, (bytearray, memoryview)):
        _message = [MessageSegment.image(bytes(message))]
    else:
        _message = [message]
    return _message  # type: ignore


async def convert_message(
    message: Union[Message, List[Message], List[str], str, bytes]
) -> List[Message]:
    _message: List[Message] = []
    if isinstance(message, List):
        if all(isinstance(x, str) for x in message):
            _message.extend([MessageSegment.node(message)])
        else:
            for i in message:
                _message.extend(await _convert_message(i))
    else:
        _message = await _convert_message(message)

    if R_enabled:
        result = ''.join(
            random.choice(R_text)
            for _ in range(random.randint(1, len(R_text)))
        )
        _message.append(MessageSegment.text(result))

    if is_text2pic:
        if (
            len(_message) == 1
            and _message[0].type == 'text'
            and isinstance(_message[0].data, str)
            and len(_message[0].data) >= int(text2pic_limit)
        ):
            img = await text2pic(_message[0].data)
            _message = [MessageSegment.image(img)]

    return _message


async def to_markdown(
    message: List[Message],
    buttons: Optional[Union[List[Button], List[List[Button]]]] = None,
) -> List[Message]:
    _markdown_list = []
    _message = []
    url = None
    size = None
    for m in message:
        if m.type == 'image':
            url = m.data
        elif m.type == 'image_size':
            size = m.data
        elif m.type == 'text':
            assert isinstance(m.data, str)
            _markdown_list.append(m.data.replace('\n', '\n\n'))
        else:
            _message.append(m)

    if url is not None and size is not None:
        _markdown_list.append(f'![test #{size[0]}px #{size[1]}px]({url})')

    _markdown = '\n'.join(_markdown_list)
    _message.extend(MessageSegment.markdown(_markdown, buttons))
    return _message
