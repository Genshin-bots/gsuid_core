"""L4 能力族展开（`expand_tools_to_families`）的公平性与不变量回归测试。

生产事故（2026-07-15）：`异环面板` 能力族有 9 个成员，大于附加工具池上限 8。
旧实现把它整族展开后直接 `break`，附加池被这一个族独占，鸣潮（XutheringWavesUID）
的同类面板工具**永远进不了工具列表**——AI 只好把"玄翎秧秧"（鸣潮角色）塞进
`nte_character`（异环工具）里硬答。

锁住四条不变量：
1. 超大族不得独占附加池——其余候选族至少拿到 1 个"兜底席位"（看得见才可能选对）；
2. 整族不可截断——预算够时整族纳入，保证"能建就能改/删"；
3. 只增不减——任何场景下新实现产出的工具都是旧实现的超集（不回退已有能力）；
4. 去重与 exclude_names 永远生效。
"""

from typing import Dict, List, Optional
from dataclasses import dataclass

import pytest

from gsuid_core.ai_core import register as ai_register
from gsuid_core.ai_core.rag import tools as rag_tools

# 事故现场的真实两族：异环面板 9 个成员 > 附加池上限 8，鸣潮面板 5 个。
NTE_PANEL: List[str] = [
    "nte_account",
    "nte_character",
    "nte_box",
    "nte_stamina",
    "nte_refresh",
    "nte_explore",
    "nte_achievement",
    "nte_realestate",
    "nte_vehicle",
]
WUWA_PANEL: List[str] = [
    "get_user_wuwa_char_detail",
    "get_user_wuwa_char_list",
    "get_user_wuwa_uids",
    "get_user_wuwa_char_scores",
    "get_user_wuwa_baseinfo",
]
# CRUD 族：整族不可截断的典型（能建就能改/删）
SCHED: List[str] = [
    "add_once_task",
    "add_interval_task",
    "modify_scheduled_task",
    "cancel_scheduled_task",
    "query_scheduled_task",
]


@dataclass
class FakeTool:
    name: str


@dataclass
class FakeToolBase:
    name: str
    capability_domain: str
    tool: FakeTool


class FakeRegistry:
    """按 {能力族: [工具名]} 建表；能力族为空串表示"未声明 domain"（单工具族）。"""

    def __init__(self, spec: Dict[str, List[str]]) -> None:
        self.by_name: Dict[str, FakeToolBase] = {}
        for domain, names in spec.items():
            for name in names:
                self.by_name[name] = FakeToolBase(name, domain, FakeTool(name))

    def find_tool_base(self, name: str) -> Optional[FakeToolBase]:
        if name not in self.by_name:
            return None
        return self.by_name[name]

    def get_family_members(self, name: str) -> List[FakeToolBase]:
        """复刻 register.get_family_members 的真实语义（未注册→[]，无 domain→单工具族）。"""
        target = self.find_tool_base(name)
        if target is None:
            return []
        if not target.capability_domain:
            return [target]
        return [tb for tb in self.by_name.values() if tb.capability_domain == target.capability_domain]

    def seeds(self, *names: str) -> List[FakeTool]:
        return [self.by_name[n].tool for n in names]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    def install(spec: Dict[str, List[str]]) -> FakeRegistry:
        reg = FakeRegistry(spec)
        # expand_tools_to_families 在函数体内 import，patch 模块属性即可生效
        monkeypatch.setattr(ai_register, "find_tool_base", reg.find_tool_base)
        monkeypatch.setattr(ai_register, "get_family_members", reg.get_family_members)
        return reg

    return install


def _legacy_expand(
    seed_tools: List[FakeTool],
    reg: FakeRegistry,
    exclude_names: set,
    max_tools: int,
) -> List[FakeTool]:
    """事故版旧实现，逐行照抄。用于锁"新实现只增不减"。"""
    seen = set(exclude_names)
    out: List[FakeTool] = []
    for seed in seed_tools:
        if seed.name in seen:
            continue
        family = reg.get_family_members(seed.name)
        family_tools = [tb.tool for tb in family] if family else [seed]
        new_members = [ft for ft in family_tools if ft.name not in seen]
        if not new_members:
            continue
        if out and len(out) + len(new_members) > max_tools:
            break
        for ft in new_members:
            seen.add(ft.name)
            out.append(ft)
        if len(out) >= max_tools:
            break
    return out


def _names(tools) -> List[str]:
    return [t.name for t in tools]


# ── 1. 事故复现：超大族不得独占附加池 ───────────────────────────────


def test_oversized_family_cannot_starve_other_plugins(registry) -> None:
    """异环面板(9) > 上限(8)：鸣潮的面板工具必须仍然出现在池里，否则 AI 无从选对。"""
    reg = registry({"异环面板": NTE_PANEL, "鸣潮面板": WUWA_PANEL})
    seeds = reg.seeds("nte_character", "get_user_wuwa_char_detail")

    out = _names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(), max_tools=8))

    assert "get_user_wuwa_char_detail" in out, "鸣潮工具被异环大族挤掉——正是本次生产事故"
    assert "nte_character" in out


def test_oversized_family_starved_others_before_the_fix(registry) -> None:
    """反向锁：旧实现在同一场景下确实把鸣潮工具饿死了（证明测试真的测到了东西）。"""
    reg = registry({"异环面板": NTE_PANEL, "鸣潮面板": WUWA_PANEL})
    seeds = reg.seeds("nte_character", "get_user_wuwa_char_detail")

    legacy = _names(_legacy_expand(seeds, reg, set(), 8))

    assert "get_user_wuwa_char_detail" not in legacy
    assert set(NTE_PANEL) == set(legacy)


# ── 2. 不得回退：排名第一的族照旧整族纳入 ──────────────────────────


def test_top_ranked_family_still_expands_whole(registry) -> None:
    """公平性不能靠截断排名第一的族来实现——它是本轮最匹配的能力，必须完整可用。"""
    reg = registry({"异环面板": NTE_PANEL, "鸣潮面板": WUWA_PANEL})
    seeds = reg.seeds("nte_character", "get_user_wuwa_char_detail")

    out = _names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(), max_tools=8))

    assert set(NTE_PANEL) <= set(out), "排名第一的族被截断了"
    # 9 个异环 + 1 个鸣潮兜底席位，超预算是刻意的（可见性优先于省 token）
    assert len(out) == 10


def test_family_not_truncated_when_budget_allows(registry) -> None:
    """预算够时整族纳入，保证"能建就能改/删"（召回 cancel 即带出 modify/query）。"""
    reg = registry({"定时任务": SCHED})
    seeds = reg.seeds("cancel_scheduled_task")

    out = _names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(), max_tools=8))

    assert set(out) == set(SCHED)


def test_no_family_is_truncated_midway(registry) -> None:
    """任一族在池里只能是：0 个 / 1 个（兜底席位）/ 整族——绝不能是"半个族"。"""
    reg = registry({"异环面板": NTE_PANEL, "鸣潮面板": WUWA_PANEL, "定时任务": SCHED})
    seeds = reg.seeds("nte_character", "get_user_wuwa_char_detail", "cancel_scheduled_task")

    out = set(_names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(), max_tools=8)))

    for domain, members in [("异环面板", NTE_PANEL), ("鸣潮面板", WUWA_PANEL), ("定时任务", SCHED)]:
        hit = out & set(members)
        assert len(hit) in (0, 1, len(members)), f"能力族 [{domain}] 被截断成了 {len(hit)}/{len(members)} 个"


# ── 3. 只增不减：新实现产出必为旧实现的超集 ────────────────────────


@pytest.mark.parametrize(
    "spec, seed_names, exclude, max_tools",
    [
        ({"异环面板": NTE_PANEL, "鸣潮面板": WUWA_PANEL}, ["nte_character", "get_user_wuwa_char_detail"], set(), 8),
        ({"异环面板": NTE_PANEL, "鸣潮面板": WUWA_PANEL}, ["get_user_wuwa_char_detail", "nte_character"], set(), 8),
        ({"定时任务": SCHED}, ["cancel_scheduled_task"], {"add_once_task", "add_interval_task"}, 8),
        ({"定时任务": SCHED, "鸣潮面板": WUWA_PANEL}, ["cancel_scheduled_task", "get_user_wuwa_uids"], set(), 8),
        ({"异环面板": NTE_PANEL}, ["nte_box"], set(), 4),
        ({"a": ["t1", "t2", "t3"], "b": ["t4", "t5"], "c": ["t6"]}, ["t1", "t4", "t6"], set(), 8),
        ({"": ["solo_trigger_tool"], "鸣潮面板": WUWA_PANEL}, ["solo_trigger_tool", "get_user_wuwa_uids"], set(), 8),
    ],
)
def test_new_expansion_is_a_superset_of_legacy(registry, spec, seed_names, exclude, max_tools) -> None:
    """核心安全网：无论什么场景，新实现都不会比旧实现少给工具（不隐性削弱召回）。"""
    reg = registry(spec)
    seeds = reg.seeds(*seed_names)

    legacy = set(_names(_legacy_expand(seeds, reg, exclude, max_tools)))
    current = set(_names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(exclude), max_tools=max_tools)))

    assert legacy <= current, f"新实现比旧实现少了这些工具: {legacy - current}"


# ── 4. 去重 / exclude / 边界 ────────────────────────────────────────


def test_exclude_names_never_readmitted(registry) -> None:
    """保底池里已有的工具不得被族展开重复塞进附加池。"""
    reg = registry({"定时任务": SCHED})
    seeds = reg.seeds("cancel_scheduled_task")

    out = _names(
        rag_tools.expand_tools_to_families(seeds, exclude_names={"add_once_task", "add_interval_task"}, max_tools=8)
    )

    assert "add_once_task" not in out
    assert "add_interval_task" not in out
    assert set(out) == set(SCHED) - {"add_once_task", "add_interval_task"}


def test_no_duplicates_when_multiple_seeds_share_a_family(registry) -> None:
    """同族多个种子不得各占一个席位、也不得重复入池。"""
    reg = registry({"异环面板": NTE_PANEL})
    seeds = reg.seeds("nte_character", "nte_box", "nte_stamina")

    out = _names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(), max_tools=16))

    assert len(out) == len(set(out)), "出现重复工具"
    assert set(out) == set(NTE_PANEL)


def test_domainless_seed_is_a_single_tool_family(registry) -> None:
    """未声明 capability_domain 的工具（如 by_trigger 桥接工具）按单工具族处理。"""
    reg = registry({"": ["waves_char_detail"], "异环面板": NTE_PANEL})
    seeds = reg.seeds("nte_character", "waves_char_detail")

    out = _names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(), max_tools=8))

    assert "waves_char_detail" in out, "无 domain 的桥接工具被大族挤掉了"


def test_empty_seeds_yield_empty_pool(registry) -> None:
    registry({"异环面板": NTE_PANEL})
    assert rag_tools.expand_tools_to_families([], exclude_names=set(), max_tools=8) == []


def test_seat_fallback_is_bounded_by_seed_seats(registry) -> None:
    """兜底席位有上限：候选种子再多，超预算的幅度也不会失控。"""
    spec = {"异环面板": NTE_PANEL, "f2": ["b1", "b2"], "f3": ["c1", "c2"], "f4": ["d1"], "f5": ["e1"], "f6": ["g1"]}
    reg = registry(spec)
    seeds = reg.seeds("nte_character", "b1", "c1", "d1", "e1", "g1")

    out = _names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(), max_tools=8, seed_seats=4))

    # 排名第一的族(9) 整族 + 至多 4 个落选种子的兜底席位
    assert len(out) <= len(NTE_PANEL) + 4
    assert len(out) == len(set(out))


# ── 5. 跨能力族提问：落选的种子不得被大族挤掉 ──────────────────────


def test_cross_family_query_keeps_all_seeds(registry) -> None:
    """「看看我练度 + 这角色怎么提升」跨两个族：资料库族整族放不下时，
    它命中的**每个种子**都必须仍然可用——只按族发席位会把第 2、3 个种子丢掉。"""
    kb = [
        "search_wuwa_kb",
        "filter_chars_wuwa",
        "filter_weapons_wuwa",
        "filter_echoes_wuwa",
        "get_sonata_echoes_wuwa",
        "get_char_signature_weapon_wuwa",
    ]
    reg = registry({"鸣潮面板": WUWA_PANEL, "鸣潮资料库": kb})
    # 面板族排第一（整族 5 个），资料库族放不下（5+6=11 > 8）
    seeds = reg.seeds(
        "get_user_wuwa_char_scores",  # 练度 → 面板族
        "search_wuwa_kb",  # 怎么提升 → 资料库族
        "get_char_signature_weapon_wuwa",  # 专武推荐 → 资料库族（同族第 2 个种子）
    )

    out = _names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(), max_tools=8))

    assert set(WUWA_PANEL) <= set(out), "面板族（排名第一）应整族纳入"
    assert "search_wuwa_kb" in out
    assert "get_char_signature_weapon_wuwa" in out, "同族第 2 个种子被丢了——跨族提问会缺工具"


def test_every_seed_survives_a_monopolising_family(registry) -> None:
    """通用不变量：只要种子数 ≤ seed_seats，任何种子都不会被超大族挤掉。"""
    reg = registry({"异环面板": NTE_PANEL, "鸣潮面板": WUWA_PANEL, "鸣潮资料库": ["search_wuwa_kb"]})
    seed_names = ["nte_character", "get_user_wuwa_char_detail", "search_wuwa_kb"]
    seeds = reg.seeds(*seed_names)

    out = set(_names(rag_tools.expand_tools_to_families(seeds, exclude_names=set(), max_tools=8)))

    for name in seed_names:
        assert name in out, f"种子 {name} 被挤掉了"
