from typing import Literal, Callable

from model import MessageContent


class Trigger:
    def __init__(
        self,
        type: Literal['prefix', 'suffix', 'keyword', 'fullmatch'],
        keyword: str,
        func: Callable,
    ):
        self.type = type
        self.keyword = keyword
        self.func = func

    def check_command(self, raw_msg: MessageContent) -> bool:
        msg = raw_msg.raw_text
        return getattr(self, f'_check_{self.type}')(self.keyword, msg)

    def _check_prefix(self, prefix: str, msg: str) -> bool:
        if msg.startswith(prefix):
            return True
        return False

    def _check_suffix(self, suffix: str, msg: str) -> bool:
        if msg.endswith(suffix):
            return True
        return False

    def _check_keyword(self, keyword: str, msg: str) -> bool:
        if keyword in msg:
            return True
        return False

    def _check_fullmatch(self, keyword: str, msg: str) -> bool:
        if msg == keyword:
            return True
        return False

    async def get_command(self, msg: MessageContent) -> MessageContent:
        msg.command = self.keyword
        msg.text = msg.raw_text.replace(self.keyword, '')
        return msg
