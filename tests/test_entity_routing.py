"""L0 实体路由（`search_tools_with_entity_routing`）的行为与不变量测试。

背景：嵌入做「实体 → 插件」的路由准确率只有 ~50%（`eval/tool_selection` 基线），
"tartaglia面板"（原神角色）能召回一池子异环工具。实体路由把这一步变成确定性查表。

锁两条底线：
1. **没有实体命中时，行为与普通 search_tools 完全一致**——路由是加分项，不能改变
   任何原有召回（这是它敢上生产的前提）；
2. 命中时至少留 1 个种子名额给通用最佳匹配——路由不接管整个召回，误路由也伤不深。
"""

import asyncio
from typing import Dict, List
from dataclasses import dataclass

import pytest

from gsuid_core.ai_core import register as ai_register
from gsuid_core.ai_core.rag import tools as rag_tools
from gsuid_core.ai_core.entity_index import clear_entity_index, register_entity_surface


@dataclass
class FakeTool:
    name: str


@dataclass
class FakeToolBase:
    name: str
    plugin: str
    tool: FakeTool


# 宽召回里：异环工具排在前面（模拟嵌入选错插件），鸣潮工具被埋在后面
WIDE: List[str] = [
    "nte_character",
    "nte_account",
    "send_typemap_img",
    "nte_box",
    "get_user_wuwa_char_detail",
    "get_user_wuwa_char_list",
]
PLUGIN_OF: Dict[str, str] = {
    "nte_character": "NTEUID",
    "nte_account": "NTEUID",
    "nte_box": "NTEUID",
    "send_typemap_img": "SayuStock",
    "get_user_wuwa_char_detail": "XutheringWavesUID",
    "get_user_wuwa_char_list": "XutheringWavesUID",
}


@pytest.fixture(autouse=True)
def _fake_world(monkeypatch: pytest.MonkeyPatch):
    clear_entity_index()

    registry = {n: FakeToolBase(n, PLUGIN_OF[n], FakeTool(n)) for n in PLUGIN_OF}

    def find_tool_base(name: str):
        if name not in registry:
            return None
        return registry[name]

    calls: List[Dict] = []

    async def fake_search_tools(query: str, limit: int = 10, non_category="", threshold: float = 0.38, **kw):
        calls.append({"query": query, "limit": limit, "threshold": threshold})
        return [registry[n].tool for n in WIDE][:limit]

    monkeypatch.setattr(ai_register, "find_tool_base", find_tool_base)
    monkeypatch.setattr(rag_tools, "search_tools", fake_search_tools)
    yield calls
    clear_entity_index()


def _names(tools) -> List[str]:
    return [t.name for t in tools]


def test_no_entity_hit_behaves_exactly_like_plain_search(_fake_world) -> None:
    """底线：没有实体命中 → 与普通 search_tools 逐字节一致（含 limit / threshold 透传）。"""
    out = asyncio.run(
        rag_tools.search_tools_with_entity_routing(
            query="帮我查下明天天气", route_text="帮我查下明天天气", limit=4, non_category=["self", "buildin"]
        )
    )

    assert _names(out) == WIDE[:4], "无实体命中时不得改变原有召回"
    assert len(_fake_world) == 1, "无实体命中时不得产生额外检索开销"
    assert _fake_world[0]["limit"] == 4
    assert _fake_world[0]["threshold"] == 0.38


def test_entity_hit_promotes_that_plugins_tools(_fake_world) -> None:
    """命中鸣潮实体 → 鸣潮工具被提到种子前面（此前它们被埋在宽召回第 5、6 位）。"""
    register_entity_surface("长离", "长离", "XutheringWavesUID")

    out = asyncio.run(
        rag_tools.search_tools_with_entity_routing(
            query="帮我看看长离的面板", route_text="帮我看看长离的面板", limit=4, non_category=["self", "buildin"]
        )
    )

    assert out[0].name == "get_user_wuwa_char_detail"
    assert "get_user_wuwa_char_list" in _names(out)
    # 旧行为下 top-4 全是异环/SayuStock，鸣潮一个都进不来
    assert "get_user_wuwa_char_detail" not in WIDE[:4]


def test_entity_routing_leaves_one_slot_for_general_match(_fake_world) -> None:
    """路由是加分项不是接管：至少留 1 个名额给通用最佳匹配，误路由也伤不深。"""
    register_entity_surface("长离", "长离", "XutheringWavesUID")

    out = asyncio.run(
        rag_tools.search_tools_with_entity_routing(
            query="帮我看看长离的面板", route_text="帮我看看长离的面板", limit=4, non_category=["self", "buildin"]
        )
    )

    non_routed = [t for t in out if PLUGIN_OF[t.name] != "XutheringWavesUID"]
    assert non_routed, "实体路由不得独占全部种子名额"


def test_ambiguous_entity_does_not_route(_fake_world) -> None:
    """归属歧义（如"深渊"）→ 不路由，退回普通检索。"""
    register_entity_surface("深渊", "深境螺旋", "GenshinUID")
    register_entity_surface("深渊", "逆境深塔", "XutheringWavesUID")

    out = asyncio.run(
        rag_tools.search_tools_with_entity_routing(
            query="这期深渊怎么打", route_text="这期深渊怎么打", limit=4, non_category=["self", "buildin"]
        )
    )

    assert _names(out) == WIDE[:4]
    assert len(_fake_world) == 1


def test_threshold_dropped_only_when_plugin_pruned_away(_fake_world, monkeypatch) -> None:
    """命中插件的工具被阈值砍光时，撤阈值再捞一次（插件归属已确定性确认）。"""
    register_entity_surface("长离", "长离", "XutheringWavesUID")

    calls: List[Dict] = []

    async def pruning_search(query: str, limit: int = 10, non_category="", threshold: float = 0.38, **kw):
        calls.append({"threshold": threshold})
        registry = {n: FakeToolBase(n, PLUGIN_OF[n], FakeTool(n)) for n in PLUGIN_OF}
        # 有阈值时鸣潮工具全被砍掉；撤掉阈值才捞得到
        pool = [n for n in WIDE if threshold <= 0 or PLUGIN_OF[n] != "XutheringWavesUID"]
        return [registry[n].tool for n in pool][:limit]

    monkeypatch.setattr(rag_tools, "search_tools", pruning_search)

    out = asyncio.run(
        rag_tools.search_tools_with_entity_routing(
            query="帮我看看长离的面板", route_text="帮我看看长离的面板", limit=4, non_category=["self", "buildin"]
        )
    )

    assert [c["threshold"] for c in calls] == [0.38, 0.0], "未在被阈值砍光后撤阈值重捞"
    assert "get_user_wuwa_char_detail" in _names(out)


def test_routed_plugin_with_no_tools_falls_back_cleanly(_fake_world) -> None:
    """实体命中的插件压根没注册工具 → 老实退回普通召回，不炸。"""
    register_entity_surface("某实体", "某实体", "PluginWithoutTools")

    out = asyncio.run(
        rag_tools.search_tools_with_entity_routing(
            query="某实体是什么", route_text="某实体是什么", limit=4, non_category=["self", "buildin"]
        )
    )

    assert _names(out) == WIDE[:4]
