"""套件槽位契约：互斥替换 / 密封槽 / 关槽副作用 / 块顺序单源。"""

from __future__ import annotations

import re
import asyncio
import pathlib
from typing import Any, List

import pytest

from gsuid_core.ai_core.kits import (
    OFF,
    CONTEXT_BLOCK_ORDER,
    AgentKit,
    KitSlotError,
    clear_kits,
    enable_kit,
    slot_health,
    disable_slot,
    occupants_of,
    is_known_block,
    load_enabled_kits,
    register_agent_kit,
    resolve_slot_config,
)
from gsuid_core.ai_core.hooks import (
    AgentHookPoint,
    AgentHookContext,
    fire_hooks,
    hook_count,
    list_hooks,
    clear_hooks,
    on_agent_hook,
)
from gsuid_core.ai_core.hooks.points import WIRED_POINTS, HOOK_POINT_SPECS
from gsuid_core.ai_core.kits.compose import compose_dynamic_context
from gsuid_core.ai_core.configs.ai_config import ai_config


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


def test_block_order_and_composer(isolated) -> None:
    assert CONTEXT_BLOCK_ORDER.index("mood") < CONTEXT_BLOCK_ORDER.index("relationship")
    assert CONTEXT_BLOCK_ORDER.index("identity") < CONTEXT_BLOCK_ORDER.index("history")
    assert CONTEXT_BLOCK_ORDER.index("history") < CONTEXT_BLOCK_ORDER.index("group_context")
    assert CONTEXT_BLOCK_ORDER.index("group_context") < CONTEXT_BLOCK_ORDER.index("memory")
    assert CONTEXT_BLOCK_ORDER.index("task") < CONTEXT_BLOCK_ORDER.index("plan_hint")
    assert CONTEXT_BLOCK_ORDER[-1] == "plugin_hints"
    for name in (
        "memory",
        "relationship",
        "mood",
        "identity",
        "history",
        "group_context",
        "plan_hint",
        "plugin_hints",
    ):
        assert is_known_block(name)
    assert not is_known_block("whatever")

    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    ctx.blocks.update({"memory": "MEM", "mood": "MOOD", "history": "HIST", "task": ""})
    full, _ = _run(compose_dynamic_context(ctx))
    assert full == "MOOD\n\nHIST\n\nMEM", full

    @on_agent_hook(AgentHookPoint.AFTER_CONTEXT, priority=400)
    async def add_hint(ctx: AgentHookContext) -> None:
        ctx.append_user_hint("本群自选：A")

    ctx2 = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    ctx2.blocks["memory"] = "MEM"
    full2, _ = _run(compose_dynamic_context(ctx2))
    assert full2.startswith("MEM") and full2.rstrip().endswith("）")
    assert full2.index("MEM") < full2.index("本群自选")


def test_slot_exclusivity_sealing_and_config(isolated) -> None:
    a = register_agent_kit(_FakeKit(kit_id="a.mem", slot="memory", display_name="A"))
    b = register_agent_kit(_FakeKit(kit_id="b.mem", slot="memory", display_name="B"))
    assert enable_kit(a.kit_id) and hook_count() == 1 and occupants_of("memory") == ("a.mem",)
    assert enable_kit(b.kit_id) and hook_count() == 1 and occupants_of("memory") == ("b.mem",)

    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    _run(fire_hooks(AgentHookPoint.COMPOSE_CONTEXT, ctx))
    assert ctx.blocks["memory"] == "from:b.mem"

    assert disable_slot("memory") == 1 and occupants_of("memory") == ()
    ctx_off = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    _run(fire_hooks(AgentHookPoint.COMPOSE_CONTEXT, ctx_off))
    assert "memory" not in ctx_off.blocks

    x = register_agent_kit(_FakeKit(kit_id="x.obs", slot="inbound_observe", display_name="X"))
    y = register_agent_kit(_FakeKit(kit_id="y.obs", slot="inbound_observe", display_name="Y"))
    assert enable_kit(x.kit_id) and enable_kit(y.kit_id)
    assert set(occupants_of("inbound_observe")) == {"x.obs", "y.obs"}

    default = register_agent_kit(_FakeKit(kit_id="gscore.speech", slot="speech", display_name="D", sealed=True))
    mine = register_agent_kit(_FakeKit(kit_id="mine.speech", slot="speech", display_name="M"))
    assert enable_kit(default.kit_id)
    with pytest.raises(KitSlotError, match="密封"):
        enable_kit(mine.kit_id)
    assert occupants_of("speech") == ("gscore.speech",)
    assert enable_kit(mine.kit_id, allow_replace_sealed=True) and occupants_of("speech") == ("mine.speech",)

    with pytest.raises(KitSlotError, match="未知槽名"):
        register_agent_kit(_FakeKit(kit_id="bad.kit", slot="no_such_slot", display_name="bad"))

    key = "kit_slots.memory"
    item = ai_config.get_config(key)
    previous = item.data
    try:
        item.data = OFF
        assert resolve_slot_config("memory") == ()
        item.data = "my_plugin.mem"
        assert resolve_slot_config("memory") == ("my_plugin.mem",)
        item.data = "a.mem,b.mem"
        assert resolve_slot_config("memory") == ("a.mem",)
    finally:
        item.data = previous


def test_first_party_kits_and_wired_fire_sites(isolated) -> None:
    enabled = _run(load_enabled_kits(run_init_steps=False))
    assert len(enabled) >= 18, enabled
    empty = [name for name, occ in slot_health().items() if not occ]
    assert not empty, f"这些槽没有占用者: {empty}"

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

    root = pathlib.Path(__file__).resolve().parents[1] / "gsuid_core"
    fired: set[str] = set()
    for path in root.rglob("*.py"):
        if "hooks" in path.parts and path.name in ("dispatch.py", "points.py", "registry.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "fire_hooks" not in text:
            continue
        fired |= set(re.findall(r"fire_hooks\(\s*AgentHookPoint\.([A-Z0-9_]+)", text))
        if re.search(r"fire_hooks\(\s*point\b", text):
            fired |= set(re.findall(r"AgentHookPoint\.([A-Z0-9_]+)", text))

    assert {p.name for p in WIRED_POINTS} == fired
    assert len(HOOK_POINT_SPECS) == len(AgentHookPoint)
