import re
from typing import Any, Literal, Callable, Awaitable

from gsuid_core.bot import Bot
from gsuid_core.models import Event

# 半角/全角空格、Tab、NBSP。输入法插在前缀后或中英交界，不是参数分隔。
_CMD_GAP = " \t\u3000\u00a0"


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF


def _script_boundary(left: str, right: str) -> bool:
    left_cjk = _is_cjk(left)
    right_cjk = _is_cjk(right)
    left_ascii = left.isascii() and left.isalnum()
    right_ascii = right.isascii() and right.isalnum()
    return (left_cjk and right_ascii) or (left_ascii and right_cjk)


def _consume_keyword(text: str, keyword: str) -> int | None:
    """关键字从 text 开头能吃掉多长。交界处的空格可有可无，词内空格仍必须出现。"""
    i = 0
    j = 0
    text_len = len(text)
    key_len = len(keyword)
    while j < key_len:
        if i >= text_len:
            return None
        key_ch = keyword[j]
        text_ch = text[i]
        if key_ch in _CMD_GAP:
            if text_ch not in _CMD_GAP:
                return None
            while j < key_len and keyword[j] in _CMD_GAP:
                j += 1
            while i < text_len and text[i] in _CMD_GAP:
                i += 1
            continue
        if text_ch in _CMD_GAP:
            if j > 0 and _script_boundary(keyword[j - 1], key_ch):
                while i < text_len and text[i] in _CMD_GAP:
                    i += 1
                continue
            return None
        if text_ch != key_ch:
            return None
        i += 1
        j += 1
    return i


def _needs_flex_gap(keyword: str) -> bool:
    """纯中文或纯英文命令不用逐字扫描，startswith 就够。"""
    prev = ""
    for ch in keyword:
        if ch in _CMD_GAP or (prev and _script_boundary(prev, ch)):
            return True
        prev = ch
    return False


def _ends_with_keyword(text: str, keyword: str) -> bool:
    """与 _consume_keyword 同一套空格规则，但从末尾对齐。"""
    i = len(text) - 1
    j = len(keyword) - 1
    while j >= 0:
        if i < 0:
            return False
        key_ch = keyword[j]
        text_ch = text[i]
        if key_ch in _CMD_GAP:
            if text_ch not in _CMD_GAP:
                return False
            while j >= 0 and keyword[j] in _CMD_GAP:
                j -= 1
            while i >= 0 and text[i] in _CMD_GAP:
                i -= 1
            continue
        if text_ch in _CMD_GAP:
            if j + 1 < len(keyword) and _script_boundary(key_ch, keyword[j + 1]):
                while i >= 0 and text[i] in _CMD_GAP:
                    i -= 1
                continue
            return False
        if text_ch != key_ch:
            return False
        i -= 1
        j -= 1
    return True


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
            "meta",
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
        # head 注册时拼好。探针是前缀首字；对不上就直接失败，不扫整句。
        self._head = prefix + keyword
        self._flex_gap = _needs_flex_gap(keyword)
        self._probe = prefix[:1] if prefix else (keyword[:1] if self._flex_gap else "")

    def check_command(self, ev: Event) -> bool:
        if self.to_me and not ev.is_tome:
            return False
        kind = self.type
        if kind == "file":
            return self._check_file(self.keyword, ev)
        if kind == "meta":
            return self._check_meta(self.keyword, ev)
        if kind == "message":
            return True
        msg = ev.raw_text
        # 普通群消息在这里返回。getattr 和逐字匹配都不能放在这条路上。
        if kind == "command":
            if msg.startswith(self._head):
                return True
        elif kind == "fullmatch":
            if msg == self._head:
                return True
        elif kind == "prefix":
            if msg.startswith(self._head) and len(msg) > len(self._head) and msg[len(self._head)] not in _CMD_GAP:
                return True
        elif kind == "suffix":
            if self.prefix and (not msg or (msg[0] not in _CMD_GAP and msg[0] != self.prefix[0])):
                return False
            return self._check_suffix(self.keyword, msg)
        elif kind == "keyword":
            if self.prefix and (not msg or (msg[0] not in _CMD_GAP and msg[0] != self.prefix[0])):
                return False
            return self._check_keyword(self.keyword, msg)
        elif kind == "regex":
            if self.prefix and (not msg or (msg[0] not in _CMD_GAP and msg[0] != self.prefix[0])):
                return False
            return self._check_regex(self.keyword, msg)
        else:
            return False
        if not msg or (msg[0] not in _CMD_GAP and msg[0] != self._probe):
            return False
        if kind == "command":
            return self._check_command(self.keyword, msg)
        if kind == "fullmatch":
            return self._check_fullmatch(self.keyword, msg)
        return self._check_prefix(self.keyword, msg)

    def _after_prefix(self, msg: str) -> str | None:
        """去掉首尾空格后切掉插件前缀。前缀和正文之间的空格一并丢掉。"""
        body = msg.strip(_CMD_GAP)
        if not self.prefix:
            return body
        if not body.startswith(self.prefix):
            return None
        return body[len(self.prefix) :].lstrip(_CMD_GAP)

    def _check_prefix(self, prefix: str, msg: str) -> bool:
        head = self._head
        if msg.startswith(head):
            if len(msg) == len(head):
                return False
            nxt = len(head)
            if msg[nxt] not in _CMD_GAP:
                return True
            while nxt < len(msg) and msg[nxt] in _CMD_GAP:
                nxt += 1
            return nxt < len(msg)
        if not msg or (msg[0] not in _CMD_GAP and msg[0] != self._probe):
            return False
        rest = self._after_prefix(msg)
        if rest is None:
            return False
        if self._flex_gap:
            if _consume_keyword(rest, prefix) is None:
                return False
        elif not rest.startswith(prefix) or rest == prefix:
            return False
        return not self._check_fullmatch(prefix, msg)

    def _check_command(self, command: str, msg: str) -> bool:
        if msg.startswith(self._head):
            return True
        # 群聊绝大多数对不上前缀首字，这里必须停，不能再进函数。
        if not msg or (msg[0] not in _CMD_GAP and msg[0] != self._probe):
            return False
        if msg[0] not in _CMD_GAP:
            if self.prefix and not msg.startswith(self.prefix):
                return False
            if (
                self.prefix
                and not self._flex_gap
                and len(msg) > len(self.prefix)
                and msg[len(self.prefix)] not in _CMD_GAP
            ):
                return False
        rest = self._after_prefix(msg)
        if rest is None:
            return False
        if self._flex_gap:
            return _consume_keyword(rest, command) is not None
        return rest.startswith(command)

    def _check_suffix(self, suffix: str, msg: str) -> bool:
        if msg.startswith(self.prefix) and msg.endswith(suffix) and msg != self._head:
            return True
        if self.prefix and (not msg or (msg[0] not in _CMD_GAP and not msg.startswith(self.prefix))):
            return False
        if not self.prefix and (not msg or msg[0] not in _CMD_GAP) and not self._flex_gap:
            return False
        rest = self._after_prefix(msg)
        if rest is None:
            return False
        if self._flex_gap:
            if not _ends_with_keyword(rest, suffix):
                return False
        elif not rest.endswith(suffix) or rest == suffix:
            return False
        return not self._check_fullmatch(suffix, msg)

    def _check_keyword(self, keyword: str, msg: str) -> bool:
        if not msg or (msg[0] not in _CMD_GAP and msg[-1] not in _CMD_GAP):
            if self.prefix and not msg.startswith(self.prefix):
                return False
            return keyword in msg
        body = msg.strip(_CMD_GAP)
        if self.prefix and not body.startswith(self.prefix):
            return False
        return keyword in body

    def _check_fullmatch(self, keyword: str, msg: str) -> bool:
        if msg == self._head:
            return True
        if not msg or (msg[0] not in _CMD_GAP and msg[0] != self._probe):
            return False
        rest = self._after_prefix(msg)
        if rest is None:
            return False
        if not self._flex_gap:
            return rest == keyword
        end = _consume_keyword(rest, keyword)
        if end is None:
            return False
        return rest[end:].strip(_CMD_GAP) == ""

    def _check_file(self, file_type: str, ev: Event) -> bool:
        if ev.file:
            if ev.file_name and ev.file_name.split(".")[-1] == file_type:
                return True
        return False

    def _check_meta(self, event_name: str, ev: Event) -> bool:
        # 事件名精确匹配；普通消息 meta_event_type 为 None 恒不相等，不会误触发
        return ev.meta_event_type == event_name

    def _check_regex(self, pattern: str, msg: str) -> bool:
        if self.prefix and (not msg or (msg[0] not in _CMD_GAP and not msg.startswith(self.prefix))):
            return False
        rest = self._after_prefix(msg)
        if rest is None:
            return False
        return bool(re.findall(pattern, rest))

    def _check_message(self, keyword: str, msg: str):
        return True

    async def get_command(self, msg: Event) -> Event:
        if self.type != "regex":
            msg.command = self.keyword
            rest = self._after_prefix(msg.raw_text)
            if rest is None:
                rest = msg.raw_text
            end = _consume_keyword(rest, self.keyword)
            if end is not None:
                msg.text = rest[end:].strip(_CMD_GAP)
            else:
                msg.text = rest.replace(self.keyword, "", 1).strip(_CMD_GAP)
        else:
            # 分组跟检查用同一段（前缀后的空格已去掉）；text 仍按原文切，避免 ^ 锚点把参数切空
            rest = self._after_prefix(msg.raw_text)
            if rest is None:
                rest = msg.raw_text
            command_group = re.search(self.keyword, rest)
            if command_group:
                msg.regex_dict = command_group.groupdict()
                msg.regex_group = command_group.groups()
                msg.command = "|".join([i if i is not None else "" for i in list(msg.regex_group)])
            text_list = re.split(self.keyword, msg.raw_text)
            msg.text = "|".join([i if i is not None else "" for i in text_list])
        return msg
