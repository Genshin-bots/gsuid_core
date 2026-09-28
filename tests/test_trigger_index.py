"""候选索引的等价性：索引给出的候选集必须是线性扫描命中集的**超集**。

漏匹配不会让功能变红，只会让群里"命令没反应"，所以这里锁的核心不变量是
`linear_hits ⊆ candidates`，而不是"两边相等"（索引允许给出多余候选，
那部分由 `check_command` 正常否掉）。

语料覆盖现行空格容忍语义（`tests/test_trigger_ime_space.py` 钉死的那套）：
前缀↔命令字之间可插空格、中英交界可插空格、纯 ASCII 段空格必需、纯中文段空格不算。
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest

from gsuid_core.sv import SL, SV, Plugins
from gsuid_core.models import Event
from gsuid_core.trigger_index import TriggerIndex, get_trigger_index, reset_trigger_index

_GAPS = (" ", "　", "\xa0", "\t")

# (插件名前缀, force_prefix, allow_empty_prefix, [(类型, 命令字), ...])
_SVS: list[tuple[str, list[str], list[str], bool, list[tuple[str, str]]]] = [
    ("IxGenshin", ["原神"], ["gs"], False, [("command", "帮助"), ("command", "抽卡记录"), ("prefix", "角色")]),
    ("IxStarRail", ["崩铁"], [], False, [("command", "帮助"), ("fullmatch", "帮助"), ("command", "绑定uid")]),
    ("IxPlain", [], [], True, [("command", "help"), ("command", "unsend list"), ("fullmatch", "status")]),
    ("IxMixed", [], [], True, [("command", "帮助me"), ("fullmatch", "原神帮助"), ("suffix", "card图")]),
    ("IxOther", ["鸣潮"], [], False, [("command", "共鸣"), ("keyword", "关键词"), ("regex", r"^(\d+)?(练度)$")]),
    ("IxFiles", [], [], True, [("file", "png"), ("message", "")]),
]


def _make_sv(name: str, prefixes: list[str], force: list[str], allow_empty: bool, specs: list[tuple[str, str]]) -> SV:
    sv = SV.__new__(SV, name)
    sv.name = name
    sv.priority = 5
    sv.pm = 6
    sv.area = "ALL"
    sv.enabled = True
    sv.black_list = []
    sv.white_list = []
    sv.TL = {}
    sv.plugins = Plugins(
        name=name,
        prefix=list(prefixes),
        force_prefix=list(force),
        allow_empty_prefix=allow_empty,
        force=True,
    )

    async def _handler(bot, ev):  # noqa: ANN001, ANN202
        return None

    for kind, keyword in specs:
        if kind == "message":
            keyword = name  # on_message 的 keyword 实际是 uuid，这里只要占位
        getattr(sv, f"on_{kind}")(keyword)(_handler)
    return sv


@pytest.fixture()
def svs() -> Iterator[list[SV]]:
    """注册一批覆盖各类型与各空格规则的触发器，退出时还原全局 SL。"""
    saved_lst = dict(SL.lst)
    saved_plugins = dict(SL.plugins)
    for name, prefixes, force, allow_empty, specs in _SVS:
        sv = _make_sv(name, prefixes, force, allow_empty, specs)
        SL.lst[name] = sv
    reset_trigger_index()
    # 造够量，让"确实收窄了"这件事测得出来（真实部署约 900 个）
    bulk = [("command", f"命令{i}") for i in range(120)]
    for sv in (_make_sv(f"IxExtra{i}", [], [], True, bulk) for i in range(3)):
        SL.lst[sv.name] = sv
    try:
        yield list(SL.lst.values())
    finally:
        SL.lst.clear()
        SL.lst.update(saved_lst)
        SL.plugins.clear()
        SL.plugins.update(saved_plugins)
        reset_trigger_index()


def _ev(text: str, is_tome: bool = False) -> Event:
    ev = Event("OneBot", "123", "m1", "group", "999", "456", {}, 6)
    ev.raw_text = text
    ev.text = text
    ev.is_tome = is_tome
    return ev


def _linear_hits(sv_list: list[SV], ev: Event) -> set[int]:
    """现行 handler 的匹配方式：逐 SV 逐触发器 check_command。"""
    hits: set[int] = set()
    for sv in sv_list:
        for bucket in sv.TL.values():
            for trigger in bucket.values():
                if trigger.check_command(ev):
                    hits.add(id(trigger))
    return hits


def _index_candidates(index: TriggerIndex, ev: Event) -> set[int]:
    return {id(t) for t in index.candidates(ev)}


def _total_triggers(sv_list: list[SV]) -> int:
    return sum(len(bucket) for sv in sv_list for bucket in sv.TL.values())


# --------------------------------------------------------------------------
# 语料
# --------------------------------------------------------------------------

# 覆盖：前缀↔命令字空格、中英交界空格、必需空格、纯中文段空格不算、
# 前缀本身是空格开头、兄弟命令共享前缀、超长消息、全角空格、NBSP、空消息
_CORPUS: tuple[str, ...] = (
    "",
    "帮助",
    " 帮助",
    "  帮助  ",
    "原神帮助",
    "原神 帮助",
    "原神  帮助",
    "原神　帮助",
    "原神\xa0帮助",
    "原神帮助 温迪",
    "原神抽卡记录 90 温迪",
    "原神抽卡记录90",
    "原神角色",
    "原神角色 温迪",
    "原神角色列表 温迪",
    "gs帮助",
    "gs 帮助",
    "gs绑定uid100",
    "gs 绑定 uid 100",
    "gs 绑 定uid 100",
    "gs绑定 uid",
    "崩铁帮助",
    "崩铁 帮助",
    "help",
    "help me",
    "help  me",
    "help me now",
    "helpunsend list",
    "unsend list",
    "unsend   list  extra",
    "unsendlist",
    "unsend list extra",
    "status",
    "status extra",
    "帮助me",
    "帮助 me",
    "帮助meok",
    "原神帮助",
    "鸣潮共鸣",
    "鸣潮 共鸣",
    "共鸣",
    "这里有关键词出现",
    "关键词",
    "练度",
    "10001 练度",
    "压缩 card 图",
    "card图",
    "完全无关的一串闲聊内容",
    "help me help me help me",
    "原神帮助" * 40,
    "gs " + "绑定" * 60,
    "　原神　帮助　",
    "\xa0gs\xa0帮助\xa0",
)


def _fuzz_corpus(count: int, seed: int) -> list[str]:
    """在命令字与真实字符之间随机插空白，专门打空格容忍的边界。"""
    rng = random.Random(seed)
    alphabet = [
        "原",
        "神",
        "帮",
        "助",
        "抽",
        "卡",
        "记",
        "录",
        "uid",
        "bind",
        "a",
        "b",
        "1",
        "23",
        "鸣",
        "潮",
        "共",
        "鸣",
    ]
    seeds = [
        "原神帮助",
        "原神抽卡记录",
        "gs绑定uid",
        "崩铁帮助",
        "help",
        "unsend list",
        "帮助me",
        "鸣潮共鸣",
        "原神角色",
        "status",
        "card图",
        "练度",
    ]
    out: list[str] = []
    for _ in range(count):
        base = rng.choice(seeds)
        chars: list[str] = []
        for ch in base:
            if rng.random() < 0.25:
                chars.append(rng.choice(_GAPS) * rng.randint(1, 3))
            chars.append(ch)
            if rng.random() < 0.12:
                chars.append(rng.choice(alphabet))
        if rng.random() < 0.2:
            chars.insert(0, rng.choice(_GAPS) * rng.randint(1, 2))
        if rng.random() < 0.2:
            chars.append(rng.choice(_GAPS) * rng.randint(1, 2))
        out.append("".join(chars))
    return out


# --------------------------------------------------------------------------
# 核心不变量
# --------------------------------------------------------------------------


def test_candidates_superset_linear_for_corpus(svs: list[SV]) -> None:
    index = TriggerIndex()
    for text in _CORPUS:
        ev = _ev(text)
        linear = _linear_hits(svs, ev)
        indexed = _index_candidates(index, ev)
        missing = linear - indexed
        assert not missing, f"消息 {text!r}：线性扫描命中 {len(linear)} 条，索引漏了 {len(missing)} 条"


def test_candidates_superset_linear_for_fuzz(svs: list[SV]) -> None:
    index = TriggerIndex()
    for text in _fuzz_corpus(2000, seed=20260928):
        ev = _ev(text)
        linear = _linear_hits(svs, ev)
        indexed = _index_candidates(index, ev)
        missing = linear - indexed
        assert not missing, f"消息 {text!r}：索引漏了 {len(missing)} 条"


def test_candidates_superset_when_is_tome(svs: list[SV]) -> None:
    """is_tome=True 时上提 to_me 过滤也不能漏。"""
    index = TriggerIndex()
    for text in _CORPUS:
        ev = _ev(text, is_tome=True)
        assert _linear_hits(svs, ev) <= _index_candidates(index, ev), f"消息 {text!r}"


def test_candidates_actually_narrower(svs: list[SV]) -> None:
    """索引必须真的收窄，否则等于没做。冷路径下候选应是全量的一小部分。"""
    index = TriggerIndex()
    total = _total_triggers(svs)
    assert total >= 100, "触发器样本太小，测不出收窄"
    cold = _ev("今天天气真不错啊大家吃了吗")
    assert len(_index_candidates(index, cold)) * 4 < total


# --------------------------------------------------------------------------
# 具体语义回归（每条都对应现行 check_command 的一条规则）
# --------------------------------------------------------------------------


def _hits(sv_list: list[SV], text: str) -> set[str]:
    ev = _ev(text)
    out: set[str] = set()
    for sv in sv_list:
        for bucket in sv.TL.values():
            for trigger in bucket.values():
                if trigger.check_command(ev):
                    out.add(f"{trigger.prefix}|{trigger.type}|{trigger.keyword}")
    return out


def _indexed(sv_list: list[SV], text: str) -> set[str]:
    ev = _ev(text)
    index = TriggerIndex()
    keep = {id(t) for t in index.candidates(ev)}
    out: set[str] = set()
    for sv in sv_list:
        for bucket in sv.TL.values():
            for trigger in bucket.values():
                if id(trigger) in keep and trigger.check_command(ev):
                    out.add(f"{trigger.prefix}|{trigger.type}|{trigger.keyword}")
    return out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("原神 帮助", "原神|command|帮助"),
        ("原神  帮助", "原神|command|帮助"),
        ("原神　帮助", "原神|command|帮助"),
        ("gs 帮助", "gs|command|帮助"),
        ("崩铁 绑定 uid 100", "崩铁|command|绑定uid"),
        ("unsend   list  extra", "|command|unsend list"),
        ("压缩 card 图", "|suffix|card图"),
        ("鸣潮 共鸣", "鸣潮|command|共鸣"),
    ],
)
def test_gap_tolerance_still_matches(svs: list[SV], text: str, expected: str) -> None:
    """索引化后，现行容忍的空格形态仍要命中。"""
    assert expected in _hits(svs, text), f"基准线性扫描就没命中 {text!r}"
    assert expected in _indexed(svs, text), f"索引化后漏了 {text!r}"


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ("原神角色", "原神|prefix|角色"),  # prefix 要求后面还有正文
        ("status extra", "|fullmatch|status"),
        ("unsendlist", "|command|unsend list"),
        ("崩铁 绑 定uid 100", "崩铁|command|绑定uid"),  # 纯中文段内的空格不算边界
    ],
)
def test_boundary_rules_preserved(svs: list[SV], text: str, forbidden: str) -> None:
    """索引不得把不该命中的变成命中。"""
    assert forbidden not in _indexed(svs, text), f"{text!r} 不该命中 {forbidden}"


# --------------------------------------------------------------------------
# 索引自身
# --------------------------------------------------------------------------


def test_owner_maps_back_to_sv(svs: list[SV]) -> None:
    index = TriggerIndex()
    ev = _ev("原神帮助")
    for trigger in index.candidates(ev):
        owner = index.owner_of(trigger)
        assert owner is not None
        assert any(trigger in bucket.values() for bucket in owner.TL.values())


def test_get_trigger_index_rebuilds_on_new_registration(svs: list[SV]) -> None:
    first = get_trigger_index()
    before = first.version
    assert before > 0

    sv = _make_sv("IxLate", [], [], True, [("command", "迟到的命令")])
    SL.lst[sv.name] = sv
    second = get_trigger_index()
    assert second.version > before
    ev = _ev("迟到的命令")
    assert any(t.keyword == "迟到的命令" for t in second.candidates(ev))


def test_get_trigger_index_is_cached(svs: list[SV]) -> None:
    assert get_trigger_index() is get_trigger_index()
