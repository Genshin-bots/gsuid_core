"""输入法在前缀/中英交界插空格时，命令仍应命中，参数内部空格保留。"""

from __future__ import annotations

import asyncio

import pytest

from gsuid_core.sv import SL, SV, Plugins
from gsuid_core.models import Event


@pytest.fixture()
def sv():
    name = "TestImeSpaceUID"
    plugin = Plugins(
        name=name,
        prefix=["原神"],
        force_prefix=["gs"],
        allow_empty_prefix=False,
        force=True,
    )
    handle = SV.__new__(SV, name)
    handle.name = name
    handle.priority = 5
    handle.TL = {}
    handle.plugins = plugin
    SL.lst[name] = handle
    yield handle
    SL.lst.pop(name, None)
    SL.plugins.pop(name, None)


def _ev(text: str) -> Event:
    ev = Event("OneBot", "123", "m1", "group", "999", "456", {}, 6)
    ev.raw_text = text
    ev.text = text
    return ev


def _hits(sv: SV, text: str) -> list[str]:
    ev = _ev(text)
    found: list[str] = []
    for trigger_dict in sv.TL.values():
        for trigger in trigger_dict.values():
            if trigger.check_command(ev):
                found.append(f"{trigger.prefix}:{trigger.type}:{trigger.keyword}")
    return found


def _text_of(sv: SV, text: str, keyword: str, prefix: str) -> str:
    ev = _ev(text)
    for trigger_dict in sv.TL.values():
        for trigger in trigger_dict.values():
            if trigger.keyword == keyword and trigger.prefix == prefix and trigger.check_command(ev):
                got = asyncio.run(trigger.get_command(ev))
                return got.text
    raise AssertionError(f"未命中 {prefix}{keyword} <- {text!r}")


def test_command_accepts_gap_between_prefix_and_keyword(sv: SV) -> None:
    @sv.on_command("查询")
    async def query(bot, ev): ...

    assert "gs:command:查询" in _hits(sv, "gs查询")
    assert "gs:command:查询" in _hits(sv, "gs 查询")
    assert "gs:command:查询" in _hits(sv, "gs\u3000查询")
    assert "gs:command:查询" in _hits(sv, "  gs  查询  ")
    assert "原神:command:查询" in _hits(sv, "原神 查询")
    assert "gs:command:查询" not in _hits(sv, "gsx查询")
    assert "gs:command:查询" not in _hits(sv, "gs 请查询")
    assert _text_of(sv, "gs 查询 123 456", "查询", "gs") == "123 456"
    assert _text_of(sv, "gs查询  123", "查询", "gs") == "123"
    assert _text_of(sv, "gs 查询", "查询", "gs") == ""


def test_cjk_internal_space_is_argument_not_part_of_keyword(sv: SV) -> None:
    @sv.on_command(("查询", "查询深渊"))
    async def query(bot, ev): ...

    hits = _hits(sv, "gs 查询 深渊")
    assert "gs:command:查询" in hits
    assert "gs:command:查询深渊" not in hits
    assert _text_of(sv, "gs 查询 深渊", "查询", "gs") == "深渊"
    assert "gs:command:查询深渊" in _hits(sv, "gs 查询深渊")


def test_script_boundary_inside_keyword(sv: SV) -> None:
    @sv.on_command("绑定uid")
    async def bind(bot, ev): ...

    assert "gs:command:绑定uid" in _hits(sv, "gs绑定uid100")
    assert "gs:command:绑定uid" in _hits(sv, "gs 绑定 uid 100")
    assert "gs:command:绑定uid" in _hits(sv, "gs\u3000绑定\u00a0uid\u3000100")
    assert _text_of(sv, "gs 绑定 uid 100", "绑定uid", "gs") == "100"
    assert "gs:command:绑定uid" not in _hits(sv, "gs 绑 定uid 100")


def test_keyword_space_is_required_when_registered(sv: SV) -> None:
    @sv.on_command("unsend list")
    async def unsend(bot, ev): ...

    assert "gs:command:unsend list" in _hits(sv, "gs unsend list extra")
    assert "gs:command:unsend list" in _hits(sv, "gs  unsend   list  extra")
    assert _text_of(sv, "gs  unsend   list  extra", "unsend list", "gs") == "extra"
    assert "gs:command:unsend list" not in _hits(sv, "gs unsendlist")


def test_fullmatch_and_prefix_keep_their_boundary(sv: SV) -> None:
    @sv.on_fullmatch("帮助")
    async def help_cmd(bot, ev): ...

    @sv.on_prefix("查询")
    async def query(bot, ev): ...

    assert "gs:fullmatch:帮助" in _hits(sv, "gs 帮助")
    assert "gs:fullmatch:帮助" in _hits(sv, "gs帮助")
    assert "gs:fullmatch:帮助" not in _hits(sv, "gs 帮助 1")
    assert "gs:prefix:查询" not in _hits(sv, "gs 查询")
    assert "gs:prefix:查询" in _hits(sv, "gs 查询角色")
    assert _text_of(sv, "gs 查询角色", "查询", "gs") == "角色"
    assert "gs:prefix:查询" in _hits(sv, "gs 查询 角色")
    assert _text_of(sv, "gs 查询 角色", "查询", "gs") == "角色"


def test_short_ascii_prefix_does_not_eat_following_word(sv: SV) -> None:
    bare = SV.__new__(SV, "TestImeSpaceShortPrefix")
    bare.name = "TestImeSpaceShortPrefix"
    bare.priority = 5
    bare.TL = {}
    bare.plugins = Plugins(
        name="TestImeSpaceShortPrefix",
        force_prefix=["a"],
        allow_empty_prefix=True,
        force=True,
    )
    SL.lst[bare.name] = bare
    try:

        @bare.on_command("股价")
        async def price(bot, ev): ...

        assert "a:command:股价" in _hits(bare, "a股价")
        assert "a:command:股价" in _hits(bare, "a 股价 茅台")
        assert _text_of(bare, "a 股价 茅台 600519", "股价", "a") == "茅台 600519"
        assert "a:command:股价" not in _hits(bare, "apple")
        assert "a:command:股价" not in _hits(bare, "a apple")
        assert ":command:股价" in _hits(bare, "股价")
    finally:
        SL.lst.pop(bare.name, None)
        SL.plugins.pop(bare.name, None)


def test_suffix_keyword_and_regex(sv: SV) -> None:
    @sv.on_suffix("card图")
    async def suf(bot, ev): ...

    @sv.on_keyword(("关键词", "绑定uid"))
    async def kw(bot, ev): ...

    @sv.on_regex(r"^(\d+)?(练度)$")
    async def reg(bot, ev): ...

    assert "gs:suffix:card图" in _hits(sv, "gs压缩 card 图")
    assert "gs:suffix:card图" not in _hits(sv, "gs压缩 card 图 额外")
    assert "gs:keyword:关键词" in _hits(sv, "gs 这里有关键词")
    assert "gs:keyword:绑定uid" not in _hits(sv, "gs 绑定 uid")
    assert "gs:regex:^(\\d+)?(练度)$" in _hits(sv, "gs 练度")
    assert "gs:regex:^(\\d+)?(练度)$" in _hits(sv, "gs练度")
    assert "gs:regex:^(\\d+)?(练度)$" not in _hits(sv, "gs 10001 练度")

    ev = _ev("gs 练度")
    for trigger_dict in sv.TL.values():
        for trigger in trigger_dict.values():
            if trigger.type == "regex" and trigger.prefix == "gs" and trigger.check_command(ev):
                got = asyncio.run(trigger.get_command(ev))
                assert got.regex_group == (None, "练度")
                assert got.text == "gs 练度"
                return
    raise AssertionError("regex 未命中")


def test_cjk_prefix_ascii_command(sv: SV) -> None:
    @sv.on_command("uid")
    async def uid_cmd(bot, ev): ...

    assert "原神:command:uid" in _hits(sv, "原神uid123")
    assert "原神:command:uid" in _hits(sv, "原神 uid 123")
    assert _text_of(sv, "原神 uid 123", "uid", "原神") == "123"
