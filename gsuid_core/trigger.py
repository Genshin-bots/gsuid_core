import re
from typing import Any, Literal, Callable, Awaitable


from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.logger import logger

import jieba

class Trigger:
    def __init__(
        self,
        type: Literal[
            "prefix",
            "suffix",
            "keyword",
            "fullmatch",
            "command",
            "file",
            "regex",
            "message",
        ],
        keyword: str,
        func: Callable,
        prefix: str = "",
        block: bool = False,
        to_me: bool = False,
    ):
        self.type = type
        self.prefix = prefix
        self.keyword = keyword
        self.func: Callable[[Bot, Event], Awaitable[Any]] = func
        self.block = block
        self.to_me = to_me

    def check_command(self, ev: Event) -> [bool, bool]:
        msg = ev.raw_text
        if self.to_me:
            if ev.is_tome:
                pass
            else:
                return False, False
        fuzzy_match = self._check_fuzzy(self.keyword, msg)
        if self.type == "file":
            return self._check_file(self.keyword, ev), fuzzy_match
        return getattr(self, f"_check_{self.type}")(self.keyword, msg), fuzzy_match

    def _check_fuzzy(self, keyword: str, msg: str) -> bool:
        if not msg.startswith(self.prefix):
            return False
        new_msg = msg.replace(self.prefix, '', 1)
        msg_set = set(jieba.cut(new_msg))
        if msg_set.__contains__(keyword):
            logger.info(f"_check_fuzzy: msg:{msg} keyword:{keyword} msg_set:{msg_set}")
            return True
        return False

    def _check_prefix(self, prefix: str, msg: str) -> bool:
        if msg.startswith(self.prefix + prefix) and not self._check_fullmatch(prefix, msg):
            return True
        return False

    def _check_command(self, command: str, msg: str) -> bool:
        if msg.startswith(self.prefix + command):
            return True
        return False

    def _check_suffix(self, suffix: str, msg: str) -> bool:
        if msg.startswith(self.prefix) and msg.endswith(suffix) and not self._check_fullmatch(suffix, msg):
            return True
        return False

    def _check_keyword(self, keyword: str, msg: str) -> bool:
        if keyword in msg and msg.startswith(self.prefix):
            return True
        return False

    def _check_fullmatch(self, keyword: str, msg: str) -> bool:
        if msg == f"{self.prefix}{keyword}" and msg.startswith(self.prefix):
            return True
        return False

    def _check_file(self, file_type: str, ev: Event) -> bool:
        if ev.file:
            if ev.file_name and ev.file_name.split(".")[-1] == file_type:
                return True
        return False

    def _check_regex(self, pattern: str, msg: str) -> bool:
        if msg.startswith(self.prefix):
            _msg = msg.replace(self.prefix, "", 1)
            command_list = re.findall(pattern, _msg)
            if command_list:
                return True
        return False

    def _check_message(self, keyword: str, msg: str):
        return True

    async def get_command(self, msg: Event) -> Event:
        if self.type != "regex":
            msg.command = self.keyword
            msg.text = msg.raw_text.replace(self.keyword, "", 1)
            if self.prefix:
                msg.text = msg.text.replace(self.prefix, "", 1)
        else:
            if self.prefix:
                msg.text = msg.text.replace(self.prefix, "", 1)
            command_group = re.search(self.keyword, msg.text)
            if command_group:
                msg.regex_dict = command_group.groupdict()
                msg.regex_group = command_group.groups()
                msg.command = "|".join([i if i is not None else "" for i in list(msg.regex_group)])
            text_list = re.split(self.keyword, msg.raw_text)
            msg.text = "|".join([i if i is not None else "" for i in text_list])
        logger.error("get_command:", msg)
        return msg
