"""人格 ``enabled_tools``：按插件收放向量检索池，不动常驻直装工具。"""

import asyncio
from typing import Dict, List, Optional
from dataclasses import field, dataclass

import pytest

from gsuid_core.ai_core import register as ai_register
from gsuid_core.ai_core.rag import tools as rag_tools
from gsuid_core.ai_core.rag.tools import collect_domain_tools, pin_trigger_keyword_hits
from gsuid_core.ai_core.agent_node.tool_scope import (
    NEUTRAL_PLUGINS,
    ToolScope,
    parse_enabled_tools_spec,
)


def test_parse_spec_variants() -> None:
    assert parse_enabled_tools_spec(None) == (True, frozenset(), frozenset())
    assert parse_enabled_tools_spec(["*"]) == (True, frozenset(), frozenset())
    assert parse_enabled_tools_spec(["*", "!Foo"]) == (True, frozenset(), frozenset({"Foo"}))
    assert parse_enabled_tools_spec(["Foo", "Bar"]) == (False, frozenset({"Foo", "Bar"}), frozenset())
    # 空列表 = 显式「都不启用」，必须与缺省的「全选」区分
    assert parse_enabled_tools_spec([]) == (False, frozenset(), frozenset())
    # 非列表脏值按全选兜底，不让人格失去工具
    assert parse_enabled_tools_spec("Foo") == (True, frozenset(), frozenset())


def test_persona_template_has_no_tool_packs() -> None:
    """人格模板只暴露 enabled_tools；能力族固定 dynamic，不进 config.json。"""
    from gsuid_core.ai_core.persona.config import DEFAULT_PERSONA_CONFIG

    assert "enabled_tools" in DEFAULT_PERSONA_CONFIG
    assert "tool_packs" not in DEFAULT_PERSONA_CONFIG


def test_neutral_plugins_always_enabled() -> None:
    scope = ToolScope.from_spec(["*", "!core", "!gsuid_core"])
    for name in NEUTRAL_PLUGINS:
        assert scope.plugin_enabled(name), f"{name} should always be enabled"


def test_scope_is_open_shortcircuit() -> None:
    assert ToolScope.from_spec(["*"]).is_open
    assert not ToolScope.from_spec(["*", "!Foo"]).is_open
    assert not ToolScope.from_spec(["Foo"]).is_open
    assert not ToolScope.from_spec([]).is_open


@dataclass
class FakeTool:
    name: str


@dataclass
class FakeToolBase:
    name: str
    plugin: str
    capability_domain: str
    tool: FakeTool
    covers: List[str] = field(default_factory=list)
    description: str = ""


class FakeRegistry:
    def __init__(self, spec: Dict[str, tuple[str, str]]) -> None:
        self.by_name: Dict[str, FakeToolBase] = {}
        for name, (plugin, domain) in spec.items():
            self.by_name[name] = FakeToolBase(name, plugin, domain, FakeTool(name))

    def find_tool_base(self, name: str) -> Optional[FakeToolBase]:
        return self.by_name.get(name)

    def get_all_tools(self) -> Dict[str, FakeToolBase]:
        return dict(self.by_name)

    def get_tools_by_capability_domain(self, domain: str) -> List[FakeToolBase]:
        return [tb for tb in self.by_name.values() if tb.capability_domain == domain]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    def install(spec: Dict[str, tuple[str, str]]) -> FakeRegistry:
        reg = FakeRegistry(spec)
        monkeypatch.setattr(ai_register, "find_tool_base", reg.find_tool_base)
        monkeypatch.setattr(ai_register, "get_all_tools", reg.get_all_tools)
        monkeypatch.setattr(ai_register, "get_tools_by_capability_domain", reg.get_tools_by_capability_domain)
        # rag/tools.py 在 import 期就把 get_all_tools 绑到了模块名上，须一并替换
        monkeypatch.setattr(rag_tools, "get_all_tools", reg.get_all_tools)
        return reg

    return install


def test_scope_gates_by_plugin(registry) -> None:
    registry({"foo_a": ("Foo", ""), "foo_b": ("Foo", ""), "bar_a": ("Bar", ""), "core_x": ("core", "")})

    deny_foo = ToolScope.from_spec(["*", "!Foo"])
    assert deny_foo.tool_enabled("foo_a") is False
    assert deny_foo.tool_enabled("bar_a") is True
    # 框架自身工具不受收放
    assert deny_foo.tool_enabled("core_x") is True

    only_foo = ToolScope.from_spec(["Foo"])
    assert only_foo.tool_enabled("foo_b") is True
    assert only_foo.tool_enabled("bar_a") is False
    assert only_foo.tool_enabled("core_x") is True

    none_on = ToolScope.from_spec([])
    assert none_on.filter_names(["foo_a", "bar_a", "core_x"]) == ["core_x"]


def test_scope_filter_names_keeps_unknown_tools(registry) -> None:
    """未注册工具不在任何人管辖范围内 → 放行，避免误伤后加载的插件。"""
    registry({"foo_a": ("Foo", "")})
    assert ToolScope.from_spec([]).filter_names(["late_loaded_tool"]) == ["late_loaded_tool"]


def test_pin_trigger_keyword_hits_respects_scope(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """触发词钉扎直查注册表、绕过 search_tools，必须自己收口。"""
    registry({"foo_trigger": ("Foo", ""), "bar_trigger": ("Bar", "")})

    monkeypatch.setattr(
        rag_tools,
        "trigger_keyword_hits",
        lambda utterance, limit=4: [
            ai_register.find_tool_base("foo_trigger"),
            ai_register.find_tool_base("bar_trigger"),
        ],
    )

    open_result = pin_trigger_keyword_hits("任意话术", [], scope=ToolScope.from_spec(["*"]))
    assert [t.name for t in open_result] == ["foo_trigger", "bar_trigger"]

    only_bar = pin_trigger_keyword_hits("任意话术", [], scope=ToolScope.from_spec(["Bar"]))
    assert [t.name for t in only_bar] == ["bar_trigger"]


def test_collect_domain_tools_skips_disabled_family(registry) -> None:
    """被排除插件的工具不得经族展开绕回池子。"""
    registry({"foo_seed": ("Foo", "共享域"), "bar_sibling": ("Bar", "共享域")})
    seed_base = ai_register.find_tool_base("foo_seed")
    assert seed_base is not None
    seeds = [seed_base.tool]

    # 种子被排除 → 不触发族展开，同域的已启用成员也不会被顺带带出
    dropped, slots = collect_domain_tools("测试", seeds, scope=ToolScope.from_spec(["Bar"]))
    assert dropped == []
    assert slots == 0

    # 种子启用 → 整族展开，但同域里被排除的成员仍要挡住
    kept, _slots = collect_domain_tools("测试", seeds, scope=ToolScope.from_spec(["Foo"]))
    assert [t.name for t in kept] == ["foo_seed"]


def test_align_seeds_does_not_reintroduce_denied_plugin(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """会话别名指向被排除插件时，深召回不得把该插件工具装回来。"""
    reg = registry({"foo_a": ("Foo", ""), "foo_deep": ("Foo", ""), "bar_a": ("Bar", "")})

    async def fake_search_tools(**kwargs: object) -> list[FakeTool]:
        scope = kwargs["scope"] if "scope" in kwargs else None
        hits = [reg.by_name["foo_deep"].tool]
        if isinstance(scope, ToolScope) and not scope.is_open:
            return [t for t in hits if scope.tool_enabled(t.name)]
        return hits

    monkeypatch.setattr(rag_tools, "search_tools", fake_search_tools)
    out = asyncio.run(
        rag_tools.align_seeds_to_context_plugin(
            [reg.by_name["bar_a"].tool],
            "Foo",
            "need foo",
            ToolScope.from_spec(["*", "!Foo"]),
        )
    )
    names = [t.name for t in out]
    assert "foo_deep" not in names
    assert "foo_a" not in names
    assert names == ["bar_a"]


def test_align_seeds_replaces_foreign_plugin_with_locked_one(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """别的插件占满种子时，仍按锁定插件深召回，并丢掉外来工具。"""
    reg = registry({"waves_a": ("Waves", ""), "genshin_kb": ("GenshinUID", "")})
    queries: list[str] = []

    async def fake_search_tools(**kwargs: object) -> list[FakeTool]:
        query = kwargs["query"] if "query" in kwargs else ""
        queries.append(query if isinstance(query, str) else "")
        return [reg.by_name["genshin_kb"].tool]

    monkeypatch.setattr(rag_tools, "search_tools", fake_search_tools)
    out = asyncio.run(
        rag_tools.align_seeds_to_context_plugin(
            [reg.by_name["waves_a"].tool],
            "GenshinUID",
            "风仙和沃雅妮莎都用什么武器",
            None,
        )
    )
    assert [t.name for t in out] == ["genshin_kb"]
    assert queries == ["风仙和沃雅妮莎都用什么武器"]
