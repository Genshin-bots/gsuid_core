import re
from typing import Literal, Callable

from gsuid_core.models import Event


class Trigger:
    def __init__(
        self,
        type: Literal[
            'prefix',
            'suffix',
            'keyword',
            'fullmatch',
            'command',
            'file',
            'regex',
            'message',
        ],
        keyword: str,
        func: Callable,
        prefix: str = '',
        block: bool = False,
        to_me: bool = False,
    ):
        self.type = type
        self.prefix = prefix
        self.keyword = keyword
        self.func = func
        self.block = block
        self.to_me = to_me

    def check_command(self, ev: Event) -> bool:
        msg = ev.raw_text
        if self.to_me:
            if ev.is_tome:
                pass
            else:
                return False
        if self.type == 'file':
            return self._check_file(self.keyword, ev)
        return getattr(self, f'_check_{self.type}')(self.keyword, msg)

    def _check_prefix(self, prefix: str, msg: str) -> bool:
        if msg.startswith(self.prefix + prefix) and not self._check_fullmatch(
            prefix, msg
        ):
            return True
        return False

    def _check_command(self, command: str, msg: str) -> bool:
        if msg.startswith(self.prefix + command):
            return True
        return False

    def _check_suffix(self, suffix: str, msg: str) -> bool:
        if (
            msg.startswith(self.prefix)
            and msg.endswith(suffix)
            and not self._check_fullmatch(suffix, msg)
        ):
            return True
        return False

    def _check_keyword(self, keyword: str, msg: str) -> bool:
        if keyword in msg and msg.startswith(self.prefix):
            return True
        return False

    def _check_fullmatch(self, keyword: str, msg: str) -> bool:
        if msg == keyword and msg.startswith(self.prefix):
            return True
        return False

    def _check_file(self, file_type: str, ev: Event) -> bool:
        if ev.file:
            if ev.file_name and ev.file_name.split('.')[-1] == file_type:
                return True
        return False

    def _check_regex(self, pattern: str, msg: str) -> bool:
        if msg.startswith(self.prefix):
            _msg = msg.replace(self.prefix, '')
            command_list = re.findall(pattern, _msg)
            if command_list:
                return True
        return False

    def _check_message(self, keyword: str, msg: str):
        return True

    async def get_command(self, msg: Event) -> Event:
        if self.type != 'regex':
            msg.command = self.keyword
            msg.text = msg.raw_text.replace(self.keyword, '')
            if self.prefix:
                msg.text = msg.text.replace(self.prefix, '')
        else:
            command_group = re.search(self.keyword, msg.raw_text)
            if command_group:
                msg.regex_dict = command_group.groupdict()
                msg.regex_group = command_group.groups()
                msg.command = '|'.join(
                    [i if i is not None else '' for i in list(msg.regex_group)]
                )
            text_list = re.split(self.keyword, msg.raw_text)
            msg.text = '|'.join(
                [i if i is not None else '' for i in text_list]
            )
        return msg
