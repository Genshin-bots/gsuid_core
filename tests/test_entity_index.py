"""实体身份索引（`ai_core/entity_index.py`）的安全约束回归测试。

这个索引会被用来做「实体 → 插件」的确定性工具路由，误命中的代价是把无关插件的工具
塞进本轮工具池、挤掉真正的语义种子。真实注册表里存在 `日` `月` `春` `夏` `仇` `竹`
这样的单字别名和 `xx` `dj` `ly` `jk` 这样的 ASCII 缩写——**护栏一旦失效，几乎每条
中文消息都会被误路由**。本文件锁死这些护栏。
"""

import pytest

from gsuid_core.ai_core.entity_index import (
    lookup_surface,
    plugins_in_text,
    clear_entity_index,
    find_entities_in_text,
    register_entity_surface,
)


@pytest.fixture(autouse=True)
def _clean_index():
    clear_entity_index()
    yield
    clear_entity_index()


# ── 护栏一：短词绝不入索引 ────────────────────────────────────────


@pytest.mark.parametrize("surface", ["日", "月", "春", "夏", "仇", "竹", "维", "🍊"])
def test_single_char_surfaces_are_rejected(surface: str) -> None:
    """单字别名真实存在于注册表；放进去会命中几乎所有中文句子。"""
    register_entity_surface(surface, "某实体", "SomePlugin")
    assert lookup_surface(surface) is None


@pytest.mark.parametrize("surface", ["xx", "dj", "ly", "jk", "bl", "kt"])
def test_short_ascii_surfaces_are_rejected(surface: str) -> None:
    """2 字母 ASCII 缩写误命中率极高（`xx` 能匹配到任何地方）。"""
    register_entity_surface(surface, "某实体", "SomePlugin")
    assert lookup_surface(surface) is None


def test_normal_surfaces_are_accepted() -> None:
    register_entity_surface("长离", "长离", "XutheringWavesUID")
    register_entity_surface("changli", "长离", "XutheringWavesUID")

    ref = lookup_surface("长离")
    assert ref is not None
    assert ref.plugins == ["XutheringWavesUID"]
    assert lookup_surface("changli") is not None


# ── 护栏二：ASCII 必须落在词边界 ──────────────────────────────────


def test_ascii_surface_requires_word_boundary() -> None:
    """`lbk` 不得命中 `flbkx`——否则英文/拼音串会疯狂误触发。"""
    register_entity_surface("lbk", "灵宝库", "SomePlugin")

    assert plugins_in_text("flbkx 是什么") == []
    assert plugins_in_text("帮我看看 lbk 的资料") == ["SomePlugin"]


def test_ascii_surface_matches_when_followed_by_cjk() -> None:
    """`\\b` 会在这里失效：CJK 也是 `\\w`，所以 "tartaglia面板" 里 `\\btartaglia\\b`
    匹配不上。英文别名紧跟中文是极常见写法，漏掉就等于路由整体失效。"""
    register_entity_surface("tartaglia", "达达利亚", "GenshinUID")

    assert plugins_in_text("tartaglia面板") == ["GenshinUID"]
    assert plugins_in_text("帮我看看tartaglia的面板") == ["GenshinUID"]
    assert plugins_in_text("xtartagliax 是啥") == [], "仍然不得被 ASCII 串包裹时命中"


def test_cjk_surface_matches_as_substring() -> None:
    """中文没有词边界，必须允许子串匹配（"玄翎秧秧" 要能命中 "玄翎"）。"""
    register_entity_surface("玄翎", "秧秧·玄翎", "XutheringWavesUID")

    assert plugins_in_text("岸岸看下我玄翎秧秧面板") == ["XutheringWavesUID"]


# ── 护栏三：归属歧义时绝不路由 ────────────────────────────────────


def test_ambiguous_surface_is_never_routed() -> None:
    """同一个词被两个插件注册（如"深渊"）→ 宁可不路由，也不猜。"""
    register_entity_surface("深渊", "深境螺旋", "GenshinUID")
    register_entity_surface("深渊", "逆境深塔", "XutheringWavesUID")

    ref = lookup_surface("深渊")
    assert ref is not None
    assert ref.is_ambiguous
    assert plugins_in_text("这期深渊怎么打") == [], "歧义 surface 竟然参与了路由"


def test_unambiguous_survives_alongside_ambiguous() -> None:
    """一句里既有歧义词又有明确实体时，明确的那个仍要路由。"""
    register_entity_surface("深渊", "深境螺旋", "GenshinUID")
    register_entity_surface("深渊", "逆境深塔", "XutheringWavesUID")
    register_entity_surface("长离", "长离", "XutheringWavesUID")

    assert plugins_in_text("长离打这期深渊行不行") == ["XutheringWavesUID"]


# ── 护栏四：无实体信号时保持沉默 ──────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["今天天气不错啊，春日和煦", "帮我查一下明天的天气", "在吗", ""],
)
def test_no_entity_means_no_routing(text: str) -> None:
    """没有可靠实体信号 → 返回空，调用方老实走向量检索。"""
    register_entity_surface("长离", "长离", "XutheringWavesUID")
    register_entity_surface("日", "日", "SomePlugin")  # 会被护栏挡掉

    assert plugins_in_text(text) == []


# ── 行为：长 surface 优先，同一实体不重复计数 ─────────────────────


def test_longest_surface_wins_and_dedups() -> None:
    """ "卡提希娅"命中后，被它覆盖的"卡提"不再重复计入同一次命中。"""
    register_entity_surface("卡提希娅", "卡提希娅", "XutheringWavesUID")
    register_entity_surface("卡提", "卡提希娅", "XutheringWavesUID")

    hits = find_entities_in_text("卡提希娅的专武是啥")

    assert [h.surface for h in hits] == ["卡提希娅"]


def test_plugin_attribution_is_required() -> None:
    """插件归属未知的 surface 不入索引——没有归属就没有路由价值。"""
    register_entity_surface("某实体名", "某实体名", "unknown")
    register_entity_surface("另一个实体", "另一个实体", "")

    assert lookup_surface("某实体名") is None
    assert lookup_surface("另一个实体") is None
