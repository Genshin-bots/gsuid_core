"""套件槽位契约：互斥替换 / 密封槽 / 关槽副作用 / 块顺序单源。"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from collections.abc import Iterator

import pytest

from gsuid_core.ai_core.kits import (
    OFF,
    AgentKit,
    KitSlotError,
    clear_kits,
    enable_kit,
    slot_health,
    disable_slot,
    occupants_of,
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
from gsuid_core.ai_core.kits.compose import compose_dynamic_context
from gsuid_core.ai_core.configs.ai_config import ai_config


class _FakeKit(AgentKit):
    """最小套件：只在 H06 填一个块，便于观察「关槽后副作用为零」。"""

    def register(self) -> None:
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=120, kit_id=self.kit_id)(self.inject)

    async def inject(self, ctx: AgentHookContext) -> None:
        ctx.set_context_block("memory", f"from:{self.kit_id}")


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(ai_config.get_config("enable"), "data", True)
    monkeypatch.setattr(ai_config.get_config("agent_hooks_enable"), "data", True)
    clear_hooks()
    clear_kits()
    yield
    clear_hooks()
    clear_kits()


def _fired_hook_points(tree: ast.Module) -> set[str]:
    def _pt(node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "AgentHookPoint":
            return node.attr
        return None

    def _pts(node: ast.AST) -> set[str]:
        if (name := _pt(node)) is not None:
            return {name}
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return {p for elt in node.elts for p in _pts(elt)}
        return set()

    bindings: dict[str, set[str]] = {}
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if pts := _pts(node.iter):
                bindings.setdefault(node.target.id, set()).update(pts)
        elif isinstance(node, ast.Assign):
            if pts := _pts(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings.setdefault(target.id, set()).update(pts)
        elif isinstance(node, ast.Call) and node.args:
            fn = node.func
            if (isinstance(fn, ast.Name) and fn.id == "fire_hooks") or (
                isinstance(fn, ast.Attribute) and fn.attr == "fire_hooks"
            ):
                calls.append(node)

    fired: set[str] = set()
    for call in calls:
        arg0 = call.args[0]
        if (name := _pt(arg0)) is not None:
            fired.add(name)
        elif isinstance(arg0, ast.Name) and arg0.id in bindings:
            fired |= bindings[arg0.id]
    return fired


def test_block_order_and_composer(isolated: None) -> None:
    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    ctx.blocks.update({"memory": "MEM", "mood": "MOOD", "history": "HIST", "task": ""})
    full, _ = asyncio.run(compose_dynamic_context(ctx))
    assert full == "MOOD\n\nHIST\n\nMEM", full

    @on_agent_hook(AgentHookPoint.AFTER_CONTEXT, priority=400)
    async def add_hint(ctx: AgentHookContext) -> None:
        ctx.append_user_hint("本群自选：A")

    ctx_all = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    ctx_all.blocks.update(
        {
            "plan_hint": "PLAN",
            "task": "TASK",
            "memory": "MEM",
            "group_context": "GRP",
            "history": "HIST",
            "identity": "ID",
            "relationship": "REL",
            "mood": "MOOD",
        }
    )
    rendered, _ = asyncio.run(compose_dynamic_context(ctx_all))
    assert rendered.index("MOOD") < rendered.index("REL")
    assert rendered.index("ID") < rendered.index("HIST") < rendered.index("GRP") < rendered.index("MEM")
    assert rendered.index("TASK") < rendered.index("PLAN") < rendered.index("本群自选")
    assert rendered.rstrip().endswith("）")


def test_slot_exclusivity_sealing_and_config(isolated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    a = register_agent_kit(_FakeKit(kit_id="a.mem", slot="memory", display_name="A"))
    b = register_agent_kit(_FakeKit(kit_id="b.mem", slot="memory", display_name="B"))
    assert enable_kit(a.kit_id) and hook_count() == 1 and occupants_of("memory") == ("a.mem",)
    assert enable_kit(b.kit_id) and hook_count() == 1 and occupants_of("memory") == ("b.mem",)

    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    asyncio.run(fire_hooks(AgentHookPoint.COMPOSE_CONTEXT, ctx))
    assert ctx.blocks["memory"] == "from:b.mem"

    assert disable_slot("memory") == 1 and occupants_of("memory") == ()
    ctx_off = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    asyncio.run(fire_hooks(AgentHookPoint.COMPOSE_CONTEXT, ctx_off))
    assert "memory" not in ctx_off.blocks

    x = register_agent_kit(_FakeKit(kit_id="x.obs", slot="inbound_observe", display_name="X"))
    y = register_agent_kit(_FakeKit(kit_id="y.obs", slot="inbound_observe", display_name="Y"))
    assert enable_kit(x.kit_id) and enable_kit(y.kit_id)
    assert set(occupants_of("inbound_observe")) == {"x.obs", "y.obs"}

    default = register_agent_kit(
        _FakeKit(kit_id="gscore.identity", slot="persona_identity", display_name="D", sealed=True)
    )
    mine = register_agent_kit(_FakeKit(kit_id="mine.identity", slot="persona_identity", display_name="M"))
    assert enable_kit(default.kit_id)
    with pytest.raises(KitSlotError, match="密封"):
        enable_kit(mine.kit_id)
    assert occupants_of("persona_identity") == ("gscore.identity",)
    assert enable_kit(mine.kit_id, allow_replace_sealed=True) and occupants_of("persona_identity") == ("mine.identity",)

    with pytest.raises(KitSlotError, match="未知槽名"):
        register_agent_kit(_FakeKit(kit_id="bad.kit", slot="no_such_slot", display_name="bad"))

    item = ai_config.get_config("kit_slots.memory")
    for raw_val, expected in ((OFF, ()), ("my_plugin.mem", ("my_plugin.mem",)), ("a.mem,b.mem", ("a.mem",))):
        monkeypatch.setattr(item, "data", raw_val)
        assert resolve_slot_config("memory") == expected


def test_first_party_kits_and_wired_fire_sites(isolated: None) -> None:
    asyncio.run(load_enabled_kits(run_init_steps=False))
    empty = [name for name, occ in slot_health().items() if not occ]
    assert not empty, f"这些槽没有占用者: {empty}"

    wired = list_hooks()
    required = (
        "ON_INBOUND",
        "AFTER_SESSION",
        "CLASSIFY",
        "RETRIEVE_CONTEXT",
        "COMPOSE_CONTEXT",
        "AFTER_RUN",
        "ON_STABLE_CONTEXT",
    )
    missing = [p for p in required if p not in wired or not wired[p]]
    assert not missing, f"这些点位没有套件挂载: {missing}"

    root = pathlib.Path(__file__).resolve().parents[1] / "gsuid_core"
    fired: set[str] = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if "plugins" in rel.parts or "__pycache__" in rel.parts:
            continue
        if "hooks" in rel.parts and path.name in ("dispatch.py", "points.py", "registry.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "fire_hooks" not in text:
            continue
        fired |= _fired_hook_points(ast.parse(text, filename=str(path)))

    assert {p.name for p in AgentHookPoint} == fired
