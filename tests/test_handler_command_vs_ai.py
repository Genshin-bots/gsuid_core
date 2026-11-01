"""handle_event 分流：权限不足的 SV 不匹配命令，从而落到 AI 分支。"""

from __future__ import annotations

import pytest

from gsuid_core.sv import SL, SV, Plugins
from gsuid_core.models import Event
from gsuid_core.handler import _sv_authorized


@pytest.fixture()
def gated_sv():
    name = "TestCommandVsAiUID"
    sv = SV.__new__(SV, name)
    sv.name = name
    sv.priority = 5
    sv.enabled = True
    sv.pm = 1
    sv.area = "ALL"
    sv.black_list = []
    sv.white_list = []
    sv.TL = {}
    sv.plugins = Plugins(
        name=name,
        prefix=["测"],
        force_prefix=[],
        allow_empty_prefix=False,
        force=True,
        pm=1,
        enabled=True,
        area="ALL",
    )
    SL.lst[name] = sv
    yield sv
    SL.lst.pop(name, None)
    SL.plugins.pop(name, None)


def _ev(*, pm: int) -> Event:
    return Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        user_pm=pm,
        raw_text="测帮助",
        WS_BOT_ID="ws1",
    )


def test_high_user_pm_sv_not_authorized(gated_sv: SV) -> None:
    ev = _ev(pm=6)
    assert _sv_authorized(gated_sv, ev, 6) is False
    ev_ok = _ev(pm=1)
    assert _sv_authorized(gated_sv, ev_ok, 1) is True


def test_unauthorized_command_does_not_match(gated_sv: SV) -> None:
    @gated_sv.on_fullmatch("帮助")
    async def help_handler(bot, ev): ...

    ev = _ev(pm=6)
    matched = []
    if _sv_authorized(gated_sv, ev, ev.user_pm):
        for trigger_dict in gated_sv.TL.values():
            for trigger in trigger_dict.values():
                if trigger.check_command(ev):
                    matched.append(trigger)
    assert matched == []

    ev_ok = _ev(pm=0)
    ev_ok.raw_text = "测帮助"
    matched_ok = []
    if _sv_authorized(gated_sv, ev_ok, ev_ok.user_pm):
        for trigger_dict in gated_sv.TL.values():
            for trigger in trigger_dict.values():
                if trigger.check_command(ev_ok):
                    matched_ok.append(trigger)
    assert len(matched_ok) >= 1


def test_sv_bot_white_and_black_list(gated_sv: SV) -> None:
    ev_bot1 = _ev(pm=1)
    ev_bot1.bot_self_id = "bot1"
    ev_bot2 = _ev(pm=1)
    ev_bot2.bot_self_id = "bot2"
    ev_default = _ev(pm=1)
    ev_default.bot_self_id = ""

    # 1. 默认空名单：全部放行
    gated_sv.plugins.bot_white_list = []
    gated_sv.plugins.bot_black_list = []
    assert _sv_authorized(gated_sv, ev_bot1, 1) is True
    assert _sv_authorized(gated_sv, ev_default, 1) is True

    # 2. 白名单含空串 [""]：不误拦全盘，依然全部放行
    gated_sv.plugins.bot_white_list = [""]
    assert _sv_authorized(gated_sv, ev_bot1, 1) is True
    assert _sv_authorized(gated_sv, ev_default, 1) is True

    # 3. 有效白名单：只放行名单内的 Bot
    gated_sv.plugins.bot_white_list = ["bot1"]
    assert _sv_authorized(gated_sv, ev_bot1, 1) is True
    assert _sv_authorized(gated_sv, ev_bot2, 1) is False
    assert _sv_authorized(gated_sv, ev_default, 1) is False

    # 4. 黑名单含空串 [""]：不误杀缺省账号
    gated_sv.plugins.bot_white_list = []
    gated_sv.plugins.bot_black_list = [""]
    assert _sv_authorized(gated_sv, ev_default, 1) is True
    assert _sv_authorized(gated_sv, ev_bot1, 1) is True

    # 5. 有效黑名单：拦截名单内的 Bot，不影响缺省账号
    gated_sv.plugins.bot_black_list = ["bot1"]
    assert _sv_authorized(gated_sv, ev_bot1, 1) is False
    assert _sv_authorized(gated_sv, ev_bot2, 1) is True
    assert _sv_authorized(gated_sv, ev_default, 1) is True

    # 6. 同时配置黑白名单：黑名单优先
    gated_sv.plugins.bot_white_list = ["bot1", "bot2"]
    gated_sv.plugins.bot_black_list = ["bot1"]
    assert _sv_authorized(gated_sv, ev_bot1, 1) is False
    assert _sv_authorized(gated_sv, ev_bot2, 1) is True
