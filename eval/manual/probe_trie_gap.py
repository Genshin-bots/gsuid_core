"""验证 trie 化会漏掉的语义：空格容忍。

现行 check_command 对 "原神 帮助"（prefix="原神", keyword="帮助"）是匹配的，
因为 _after_prefix 会 lstrip 掉前缀后的空格。纯逐字 trie 走到空格就断，会漏。
本脚本把现行语义当基准，逐条比对候选集。
"""

from __future__ import annotations

from gsuid_core.models import Event
from gsuid_core.trigger import Trigger

_GAP = " \t　\xa0"
_TRIE_TYPES = frozenset({"command", "prefix", "fullmatch"})


async def _noop(bot, ev):  # noqa: ANN001, ANN201
    return None


def make_event(text: str) -> Event:
    ev = Event("t", "t", "1", "group", "g", "u", {"nickname": "n"})
    ev.raw_text = text
    ev.text = text
    return ev


# (前缀, 命令字, 类型) —— 覆盖纯中文 / 纯英文 / 混写 / 带空格 四类
SPECS: list[tuple[str, str, str]] = [
    ("原神", "帮助", "command"),
    ("原神", "抽卡记录", "command"),
    ("原神", "角色", "prefix"),
    ("", "帮助", "command"),
    ("", "help", "command"),
    ("", "help me", "command"),  # 关键字自身含空格 → _flex_gap
    ("", "帮助me", "command"),  # 中英交界 → _flex_gap
    ("", "原神帮助", "fullmatch"),
    ("原神", "帮助", "fullmatch"),
    ("", "谢谢", "suffix"),
    ("", "关键词", "keyword"),
    ("", r"^\d+$", "regex"),
    ("", "文件", "file"),
]

CASES = [
    "原神帮助",
    "原神 帮助",  # 前缀与命令之间有空格 —— 纯 trie 的断点
    " 原神帮助",  # 前缀前有空格（剥命令符后的常见形态）
    "原神  帮助",
    "帮助",
    " 帮助",
    "原神抽卡记录 90",
    "原神角色列表 温迪",
    "help",
    "help  me",
    "help me now",
    "帮助 me",
    "帮助me",
    "谢谢",
    "看图谢谢大家",
    "这个关键词出现了",
    "12345",
    "",
    "完全无关的一串",
]


class Node:
    __slots__ = ("children", "payload", "gap_ok")

    def __init__(self) -> None:
        self.children: dict[str, Node] = {}
        self.payload: list[Trigger] = []
        # 走到这里时是否允许吃掉若干空格再继续（位置 = 已吃字符数）
        self.gap_ok: bool = False


def build() -> list[Trigger]:
    return [
        Trigger(kind, kw, _noop, pfx, False, False)  # type: ignore[arg-type]
        for pfx, kw, kind in SPECS
    ]


def build_trie(triggers: list[Trigger], gap_aware: bool) -> Node:
    root = Node()
    for tr in triggers:
        if tr.type not in _TRIE_TYPES or not tr._head:
            continue
        head = tr._head
        # 允许跳空格的位置：前缀末尾；flex 关键字内部保守放开全部
        positions: set[int] = set()
        if gap_aware:
            if tr.prefix:
                positions.add(len(tr.prefix))
            if tr._flex_gap:
                positions.update(range(len(head) + 1))
        node = root
        for i, ch in enumerate(head):
            child = node.children.get(ch)
            if child is None:
                child = Node()
                node.children[ch] = child
            node = child
            if i + 1 in positions:
                node.gap_ok = True
        node.payload.append(tr)
    return root


def linear(triggers: list[Trigger], msg: str) -> list[Trigger]:
    ev = make_event(msg)
    return [t for t in triggers if t.check_command(ev)]


def walk(root: Node, text: str) -> list[Trigger]:
    out: list[Trigger] = []
    seen: set[int] = set()

    def step(node: Node, i: int) -> None:
        stack: list[tuple[Node, int]] = [(node, i)]
        while stack:
            cur, pos = stack.pop()
            for tr in cur.payload:
                if id(tr) not in seen:
                    seen.add(id(tr))
                    out.append(tr)
            if pos >= len(text):
                continue
            child = cur.children.get(text[pos])
            if child is not None:
                stack.append((child, pos + 1))
            if cur.gap_ok and text[pos] in _GAP:
                nxt = pos
                while nxt < len(text) and text[nxt] in _GAP:
                    nxt += 1
                after = cur.children.get(text[nxt]) if nxt < len(text) else None
                if after is not None:
                    stack.append((after, nxt + 1))

    step(root, 0)
    return out


def names(triggers: list[Trigger], subset: set[int]) -> str:
    label = {id(t): f"{p or ''}|{k}|{k2}" for t, (p, k, k2) in zip(triggers, SPECS)}
    return ",".join(sorted(label[i] for i in subset)) or "-"


def main() -> None:
    triggers = build()
    root_plain = build_trie(triggers, gap_aware=False)
    root_gap = build_trie(triggers, gap_aware=True)

    bad_plain: list[str] = []
    bad_gap: list[str] = []
    for msg in CASES:
        ev = make_event(msg)
        base = {id(t) for t in triggers if t.check_command(ev)}
        plain = {id(t) for t in walk(root_plain, msg.strip(_GAP)) if t.check_command(ev)}
        gap = {id(t) for t in walk(root_gap, msg.strip(_GAP)) if t.check_command(ev)}
        if plain != base:
            bad_plain.append(msg)
        if gap != base:
            bad_gap.append(msg)
            miss = names(triggers, base - gap)
            extra = names(triggers, gap - base)
            print(f"  !! 跳空格trie 不一致 [{msg!r}] 漏={miss} 多={extra}")
        mark = "✓" if gap == base else "✗"
        print(f"{msg!r:<18}{mark}  线性命中={names(triggers, base)}")

    print(f"\n纯 trie 不一致: {len(bad_plain)}/{len(CASES)} -> {bad_plain}")
    print(f"跳空格 trie 不一致: {len(bad_gap)}/{len(CASES)} -> {bad_gap}")


if __name__ == "__main__":
    main()
