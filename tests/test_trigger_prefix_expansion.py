"""回归守门：无前缀语义的触发类型(file/meta/message)不得按前缀展开。

历史 bug：on_file 随插件前缀数注册 N 份等价 Trigger，而 _check_file 匹配时
不读 prefix，导致一次文件上传执行 N 次处理函数（GenshinUID 抽卡记录导入）。
"""

import pytest

from gsuid_core.sv import SL, SV, Plugins
from gsuid_core.models import Event


@pytest.fixture()
def multi_prefix_sv():
    """GenshinUID 风格：force_prefix + 用户配置前缀，共 3 个生效前缀。

    SV.__init__ 依赖调用栈推导插件名（要求路径含 plugins/），测试里改用
    __new__ 构造最小 SV 壳，_on 仅依赖 TL 与 plugins 两个属性。
    """
    name = "TestPrefixExpansionUID"
    sv = SV.__new__(SV, name)
    sv.name = name
    sv.priority = 5
    sv.TL = {}
    sv.plugins = Plugins(
        name=name,
        prefix=["原神", "原"],
        force_prefix=["gs"],
        allow_empty_prefix=False,
        force=True,
    )
    SL.lst[name] = sv
    yield sv
    SL.lst.pop(name, None)
    SL.plugins.pop(name, None)


def _matched_triggers(sv: SV, ev: Event) -> list:
    """复刻 handler 的匹配收集：遍历全部触发器，返回命中列表。"""
    matched = []
    for trigger_dict in sv.TL.values():
        for trigger in trigger_dict.values():
            if trigger.check_command(ev):
                matched.append(trigger)
    return matched


def _file_event() -> Event:
    ev = Event("OneBot", "123", "msg1", "group", "999", "456", {}, 6)
    ev.file_name = "uif.json"
    ev.file = "https://example.com/uif.json"
    ev.file_type = "url"
    return ev


def test_on_file_registers_single_trigger_with_many_prefixes(multi_prefix_sv: SV):
    @multi_prefix_sv.on_file("json")
    async def import_handler(bot, ev): ...

    file_triggers = multi_prefix_sv.TL.get("file", {})
    assert len(file_triggers) == 1, f"file 触发器被前缀展开: {list(file_triggers)}"
    assert _matched_triggers(multi_prefix_sv, _file_event()), "json 文件应命中"
    assert len(_matched_triggers(multi_prefix_sv, _file_event())) == 1


def test_on_file_prefix_false_still_single_trigger(multi_prefix_sv: SV):
    # XutheringWavesUID 的历史规避写法，根治后行为保持一致
    @multi_prefix_sv.on_file("json", prefix=False)
    async def import_handler(bot, ev): ...

    assert len(multi_prefix_sv.TL.get("file", {})) == 1
    assert len(_matched_triggers(multi_prefix_sv, _file_event())) == 1


def test_text_triggers_keep_prefix_expansion(multi_prefix_sv: SV):
    @multi_prefix_sv.on_fullmatch("帮助")
    async def help_handler(bot, ev): ...

    @multi_prefix_sv.on_command("查询")
    async def query_handler(bot, ev): ...

    fullmatch = multi_prefix_sv.TL.get("fullmatch", {})
    command = multi_prefix_sv.TL.get("command", {})
    assert len(fullmatch) == 3, f"fullmatch 前缀展开被误伤: {list(fullmatch)}"
    assert len(command) == 3, f"command 前缀展开被误伤: {list(command)}"

    ev = Event("OneBot", "123", "msg2", "group", "999", "456", {}, 6)
    ev.raw_text = "gs帮助"
    assert len(_matched_triggers(multi_prefix_sv, ev)) == 1
    ev.raw_text = "原神帮助"
    assert len(_matched_triggers(multi_prefix_sv, ev)) == 1
    ev.raw_text = "帮助"
    assert not _matched_triggers(multi_prefix_sv, ev), "allow_empty_prefix=False 不应裸匹配"


def test_on_meta_registers_single_trigger(multi_prefix_sv: SV):
    @multi_prefix_sv.on_meta("poke")
    async def poke_handler(bot, ev): ...

    meta_triggers = multi_prefix_sv.TL.get("meta", {})
    assert len(meta_triggers) == 1

    ev = Event("OneBot", "123", "msg3", "group", "999", "456", {}, 6)
    ev.meta_event_type = "poke"
    assert len(_matched_triggers(multi_prefix_sv, ev)) == 1
    ev.meta_event_type = None
    assert not _matched_triggers(multi_prefix_sv, ev)
