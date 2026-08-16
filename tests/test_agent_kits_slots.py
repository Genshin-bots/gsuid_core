"""套件槽位契约：互斥替换 / 密封槽 / 关槽副作用 / 块顺序单源 / 两入口同源。

「默认全开时用户可见行为与改造前一致」是套件化唯一真正的验收标准；
「关槽时副作用为零且有告警」是它的对偶。
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from gsuid_core.ai_core.kits import (
    OFF,
    KIT_SLOTS,
    CONTEXT_BLOCK_ORDER,
    AgentKit,
    KitSlotError,
    slot_of,
    clear_kits,
    enable_kit,
    disable_kit,
    slot_health,
    disable_slot,
    occupants_of,
    is_known_block,
    register_agent_kit,
)
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, hook_count, clear_hooks, on_agent_hook


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _FakeKit(AgentKit):
    """最小套件：只在 H06 填一个块，便于观察「关槽后副作用为零」。"""

    def register(self) -> None:
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=120, kit_id=self.kit_id)(self.inject)

    async def inject(self, ctx: AgentHookContext) -> None:
        ctx.set_context_block("memory", f"from:{self.kit_id}")


@pytest.fixture
def isolated():
    clear_hooks()
    clear_kits()
    yield
    clear_hooks()
    clear_kits()


def test_block_order_is_the_single_source(isolated) -> None:
    """块名表是唯一顺序定义；A 写 memory、C 写 relationship，名字不许各自造。"""
    assert CONTEXT_BLOCK_ORDER.index("mood") < CONTEXT_BLOCK_ORDER.index("relationship")
    assert CONTEXT_BLOCK_ORDER.index("identity") < CONTEXT_BLOCK_ORDER.index("history")
    assert CONTEXT_BLOCK_ORDER.index("history") < CONTEXT_BLOCK_ORDER.index("memory")
    assert CONTEXT_BLOCK_ORDER[-1] == "plugin_hints", "第三方 hint 恒在最后"
    for name in ("memory", "relationship", "mood", "identity", "history", "plugin_hints"):
        assert is_known_block(name)
    assert not is_known_block("whatever")


def test_slot_table_covers_18_replaceable_units(isolated) -> None:
    assert len(KIT_SLOTS) == 18
    names = {s.name for s in KIT_SLOTS}
    # 槽名不含点号：点号既是槽名一部分又是配置层级分隔符会有解析歧义
    assert all("." not in n for n in names), names
    assert "tool_assembly" in names and "tool.assemble" not in names
    # 密封槽：出站话术态与身份锚
    sealed = {s.name for s in KIT_SLOTS if s.sealed}
    assert sealed == {"speech", "persona_identity"}, sealed
    # 入站观察允许多占（记忆观察 ≠ 表情观察）
    assert not slot_of("inbound_observe").exclusive
    assert slot_of("memory").exclusive


def test_exclusive_slot_replacement_unloads_the_old_occupant(isolated) -> None:
    a = register_agent_kit(_FakeKit(kit_id="a.mem", slot="memory", display_name="A"))
    b = register_agent_kit(_FakeKit(kit_id="b.mem", slot="memory", display_name="B"))

    assert enable_kit(a.kit_id)
    assert hook_count() == 1
    assert occupants_of("memory") == ("a.mem",)

    assert enable_kit(b.kit_id)
    assert hook_count() == 1, "互斥槽必须先卸旧占用者，不许静默双挂"
    assert occupants_of("memory") == ("b.mem",)

    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    from gsuid_core.ai_core.hooks import fire_hooks

    _run(fire_hooks(AgentHookPoint.COMPOSE_CONTEXT, ctx))
    assert ctx.blocks["memory"] == "from:b.mem", "旧套件的 hook 没卸净"


def test_non_exclusive_slot_allows_fan_out(isolated) -> None:
    x = register_agent_kit(_FakeKit(kit_id="x.obs", slot="inbound_observe", display_name="X"))
    y = register_agent_kit(_FakeKit(kit_id="y.obs", slot="inbound_observe", display_name="Y"))
    assert enable_kit(x.kit_id) and enable_kit(y.kit_id)
    assert set(occupants_of("inbound_observe")) == {"x.obs", "y.obs"}


def test_sealed_slot_refuses_replacement_unless_explicitly_allowed(isolated) -> None:
    default = register_agent_kit(_FakeKit(kit_id="gscore.speech", slot="speech", display_name="D", sealed=True))
    mine = register_agent_kit(_FakeKit(kit_id="mine.speech", slot="speech", display_name="M"))
    assert enable_kit(default.kit_id)
    with pytest.raises(KitSlotError, match="密封"):
        enable_kit(mine.kit_id)
    assert occupants_of("speech") == ("gscore.speech",)
    assert enable_kit(mine.kit_id, allow_replace_sealed=True)
    assert occupants_of("speech") == ("mine.speech",)


def test_unknown_slot_is_rejected_at_registration(isolated) -> None:
    with pytest.raises(KitSlotError, match="未知槽名"):
        register_agent_kit(_FakeKit(kit_id="bad.kit", slot="no_such_slot", display_name="bad"))


def test_disabling_a_slot_has_zero_side_effect(isolated) -> None:
    """关槽 = 不注册 = 自然跳过。内核里不该有 ``if enable_x``。"""
    from gsuid_core.ai_core.hooks import fire_hooks

    kit = register_agent_kit(_FakeKit(kit_id="a.mem", slot="memory", display_name="A"))
    enable_kit(kit.kit_id)
    assert disable_slot("memory") == 1
    assert occupants_of("memory") == ()
    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    _run(fire_hooks(AgentHookPoint.COMPOSE_CONTEXT, ctx))
    assert "memory" not in ctx.blocks, "关槽后仍有块 = 副作用没清"


def test_owned_tools_are_unregistered_on_disable(isolated) -> None:
    """关槽或替换时必须卸掉 owns_tools，否则「套件没了、模型还看见空壳工具」。"""
    from gsuid_core.ai_core.register import find_tool_base, get_registered_tools

    registry = get_registered_tools()
    victim = "set_user_favorability"
    original = None
    for cat, tools in registry.items():
        if victim in tools:
            original = (cat, tools[victim])
            break
    if original is None:
        pytest.skip("set_user_favorability 未注册（buildin_tools 未导入）")

    kit = register_agent_kit(
        _FakeKit(kit_id="a.mem", slot="memory", display_name="A", owns_tools=(victim,)),
    )
    enable_kit(kit.kit_id)
    assert find_tool_base(victim) is not None
    disable_kit(kit.kit_id)
    assert find_tool_base(victim) is None
    # 复原，避免污染同进程后续测试
    registry[original[0]][victim] = original[1]


def test_removed_compat_tools_are_unregistered() -> None:
    import gsuid_core.ai_core.buildin_tools  # noqa: F401
    from gsuid_core.ai_core.register import find_tool_base

    for name in (
        "query_user_memory",
        "update_user_favorability",
        "read_persisted_output",
        "set_session_reply_mute",
        "clear_session_reply_mute",
    ):
        assert find_tool_base(name) is None, name


def test_slot_config_resolution_and_off(isolated, monkeypatch) -> None:
    from gsuid_core.ai_core.kits import resolve_slot_config
    from gsuid_core.ai_core.configs.ai_config import ai_config

    key = "kit_slots.memory"
    item = ai_config.get_config(key)
    previous = item.data
    try:
        item.data = OFF
        assert resolve_slot_config("memory") == (), "off 必须解析成空元组（无占用者）"
        item.data = "my_plugin.mem"
        assert resolve_slot_config("memory") == ("my_plugin.mem",)
        # 互斥槽只取第一个，避免配置写了多个却静默双挂
        item.data = "a.mem,b.mem"
        assert resolve_slot_config("memory") == ("a.mem",)
    finally:
        item.data = previous


def test_slot_health_surfaces_empty_slots(isolated) -> None:
    health = slot_health()
    assert set(health) == {s.name for s in KIT_SLOTS}
    assert all(occ == [] for occ in health.values()), "隔离环境下应全空"


def test_all_first_party_kits_load_and_occupy_their_slots(isolated) -> None:
    """默认配置下 18 个槽全部有占用者——这是「默认全开」的前提。"""
    from gsuid_core.ai_core.kits import load_enabled_kits

    enabled = _run(load_enabled_kits(run_init_steps=False))
    assert len(enabled) >= 18, enabled
    empty = [name for name, occ in slot_health().items() if not occ]
    assert not empty, f"这些槽没有占用者: {empty}"


def test_p0_hook_points_are_wired_by_first_party_kits(isolated) -> None:
    """套件化不能让点位变成空壳：P0 点位必须有第一方占用者。"""
    from gsuid_core.ai_core.kits import load_enabled_kits
    from gsuid_core.ai_core.hooks import list_hooks

    _run(load_enabled_kits(run_init_steps=False))
    wired = list_hooks()
    required: List[str] = [
        "ON_INBOUND",
        "BEFORE_AI_CHAT",
        "AFTER_SESSION",
        "CLASSIFY",
        "REACTIVE_GATE",
        "RETRIEVE_CONTEXT",
        "COMPOSE_CONTEXT",
        "AFTER_RUN",
        "ON_TOOL_CALL",
        "ASSEMBLE_TOOLS",
        "ON_STABLE_CONTEXT",
    ]
    missing = [p for p in required if not wired.get(p)]
    assert not missing, f"这些点位没有套件挂载: {missing}"


def test_kit_init_steps_do_not_duplicate_core_init_steps() -> None:
    """一个子系统的 bring-up 只能有**一个**主：``_INIT_STEPS`` 或某个套件的 ``init_step``。

    两边都挂时每次启动都会把同一段初始化跑两遍。实测 Meme 的一次性向量迁移被跑了两次
    （各 337 秒，并强制重建了两次 collection），并因此把套件装载推到 5 分钟之后，
    使整轮基准零工具调用。
    """
    import re
    import inspect
    from typing import Callable

    from gsuid_core.ai_core.kits import list_kits
    from gsuid_core.ai_core.startup import _INIT_STEPS, _init_agent_kits

    def _awaited(fn: Callable[..., Any]) -> set[str]:
        """该初始化函数体内 ``await`` 到的调用名——用它判断两处是否在拉同一个子系统。"""
        return set(re.findall(r"await (\w+)\(", inspect.getsource(fn)))

    owned: set[str] = set()
    for _name, step in _INIT_STEPS:
        if step is _init_agent_kits:
            continue
        owned |= _awaited(step)

    for kit in list_kits():
        if kit.init_step is None:
            continue
        clash = _awaited(kit.init_step) & owned
        assert not clash, f"{kit.kit_id}.init_step 与 _INIT_STEPS 重复初始化: {sorted(clash)}"


def test_wired_flag_matches_actual_fire_sites() -> None:
    """``HOOK_POINT_SPECS[...].wired`` 必须与内核真的开火的点位一致。

    全部标 True 时，插件按发布的点位表挂 `veto_tool` / `replace_text` 会拿到一个
    永不执行的回调且无任何告警——契约文档变成了谎言。这里用源码里的 `fire_hooks`
    调用点反推真值，让「加了枚举忘了接线」当场失败。
    """
    import re
    import pathlib

    from gsuid_core.ai_core.hooks.points import WIRED_POINTS, HOOK_POINT_SPECS, AgentHookPoint

    root = pathlib.Path(__file__).resolve().parents[1] / "gsuid_core"
    fired: set[str] = set()
    for path in root.rglob("*.py"):
        if "hooks" in path.parts and path.name in ("dispatch.py", "points.py", "registry.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "fire_hooks" not in text:
            continue
        # 直接开火：fire_hooks(AgentHookPoint.X, ...)
        fired |= set(re.findall(r"fire_hooks\(\s*AgentHookPoint\.([A-Z0-9_]+)", text))
        # 循环开火：for point in (AgentHookPoint.X, AgentHookPoint.Y): … fire_hooks(point, …)
        if re.search(r"fire_hooks\(\s*point\b", text):
            fired |= set(re.findall(r"AgentHookPoint\.([A-Z0-9_]+)", text))

    declared = {p.name for p in WIRED_POINTS}
    assert declared == fired, (
        f"wired 声明与实际开火点不符\n"
        f"  声明已接线却没开火: {sorted(declared - fired)}\n"
        f"  开火了却没声明: {sorted(fired - declared)}"
    )
    assert len(HOOK_POINT_SPECS) == len(AgentHookPoint), "点位表与枚举必须一一对应"


def test_both_entries_stamp_current_time_from_one_producer() -> None:
    """生产入口与评测入口都必须补分秒级时间行，且共用同一个产出点。

    人设 system_prompt 只到「日」级（保 provider 前缀缓存），分秒由 user 侧每轮补。
    评测入口曾漏掉这一行：模型只知道日期不知道钟点，问「现在几点了」答「不知道…
    没在看时间」，而且没有任何报错。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "gsuid_core"
    produced = (root / "ai_core" / "turn_pipeline.py").read_text(encoding="utf-8")
    assert produced.count("[当前时间：") == 1, "时间行只允许有一个产出点"

    handle_ai = (root / "ai_core" / "handle_ai.py").read_text(encoding="utf-8")
    endpoint = (root / "webconsole" / "chat_with_history_api.py").read_text(encoding="utf-8")
    assert "stamp_current_time" in handle_ai, "一轮编排必须钉时间行"
    assert "run_interactive_turn(" in endpoint, "评测必须走同一轮编排（时间行在那里钉）"
    assert "[当前时间：" not in handle_ai and "[当前时间：" not in endpoint


def test_kernel_tool_assembly_gate_is_config_driven_not_occupancy() -> None:
    """五层装配的让位判据必须取**配置**，不能取运行期占用表。

    占用表是 ``load_enabled_kits`` 填的；用它当判据会把「套件还没加载完」误读成
    「用户把槽拆了」，于是启动窗口内所有请求退化成零工具（find_tools 一并消失）。
    """
    from gsuid_core.ai_core.agent_run.tools import _kernel_owns_tool_assembly

    # 注册表全空（模拟「尚未加载」）时仍须由内核自管
    clear_kits()
    assert _kernel_owns_tool_assembly(), "套件未加载时必须 fail-open 由内核装配"


def test_composer_orders_blocks_regardless_of_write_order(isolated) -> None:
    """乱序写入也按块名表拼；空块丢弃。"""
    from gsuid_core.ai_core.kits.compose import compose_dynamic_context

    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    ctx.blocks.update({"memory": "MEM", "mood": "MOOD", "history": "HIST", "task": ""})
    full, _ = _run(compose_dynamic_context(ctx))
    assert full == "MOOD\n\nHIST\n\nMEM", full


def test_composer_puts_plugin_hints_last(isolated) -> None:
    from gsuid_core.ai_core.kits.compose import compose_dynamic_context

    @on_agent_hook(AgentHookPoint.AFTER_CONTEXT, priority=400)
    async def add_hint(ctx: AgentHookContext) -> None:
        ctx.append_user_hint("本群自选：A")

    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    ctx.blocks["memory"] = "MEM"
    full, _ = _run(compose_dynamic_context(ctx))
    assert full.startswith("MEM")
    assert full.rstrip().endswith("）"), full
    assert full.index("MEM") < full.index("本群自选")
