from typing import List, Literal, Callable

from model import MessageReceive


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

    def check_command(self, raw_msg: MessageReceive) -> bool:
        msg = raw_msg.content[0].data
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


class TriggerList:
    def __init__(self):
        self.lst: List[Trigger] = []

    @property
    def get_lst(self):
        return self.lst


TL = TriggerList()
