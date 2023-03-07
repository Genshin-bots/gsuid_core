from typing import Literal, Callable

from gsuid_core.models import Event


class Trigger:
    def __init__(
        self,
        type: Literal['prefix', 'suffix', 'keyword', 'fullmatch', 'command'],
        keyword: str,
        func: Callable,
        block: bool = False,
    ):
        self.type = type
        self.keyword = keyword
        self.func = func
        self.block = block

    def check_command(self, raw_msg: Event) -> bool:
        msg = raw_msg.raw_text
        return getattr(self, f'_check_{self.type}')(self.keyword, msg)

    def _check_prefix(self, prefix: str, msg: str) -> bool:
        if msg.startswith(prefix) and not self._check_fullmatch(prefix, msg):
            return True
        return False

    def _check_command(self, command: str, msg: str) -> bool:
        if msg.startswith(command):
            return True
        return False

    def _check_suffix(self, suffix: str, msg: str) -> bool:
        if msg.endswith(suffix) and not self._check_fullmatch(suffix, msg):
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

    async def get_command(self, msg: Event) -> Event:
        msg.command = self.keyword
        msg.text = msg.raw_text.replace(self.keyword, '')
        return msg
