"""Agent 环 hook 总线（对外唯一入口）。

插件与第一方套件用同一张点位表、同一套能力票。用法见
``.agents/skills/gscore-development/references/13-agent-loop-hooks.md``::

    from gsuid_core.ai_core.hooks import on_agent_hook, AgentHookPoint


    @on_agent_hook(AgentHookPoint.AFTER_CONTEXT, priority=420)
    async def inject_watchlist(ctx) -> None:
        ctx.append_user_hint("本群自选：…")
"""

from gsuid_core.ai_core.hooks.models import (
    HookDecision,
    AgentHookResult,
    AgentHookContext,
    HookRegistration,
    HookCapabilityError,
)
from gsuid_core.ai_core.hooks.points import (
    HOOK_POINT_SPECS,
    HookPointSpec,
    AgentHookPoint,
    HookCapability,
    spec_for,
)
from gsuid_core.ai_core.hooks.markers import KIT_HINT_PREFIX, PLUGIN_HINT_PREFIX, is_hook_hint
from gsuid_core.ai_core.hooks.dispatch import fire_hooks, should_fire, hooks_enabled
from gsuid_core.ai_core.hooks.registry import (
    hooks_for,
    hook_count,
    list_hooks,
    clear_hooks,
    on_agent_hook,
    hooks_registered,
    drop_hooks_for_kit,
    drop_hooks_for_module,
)

__all__ = [
    "HOOK_POINT_SPECS",
    "KIT_HINT_PREFIX",
    "PLUGIN_HINT_PREFIX",
    "AgentHookContext",
    "AgentHookPoint",
    "AgentHookResult",
    "HookCapability",
    "HookCapabilityError",
    "HookDecision",
    "HookPointSpec",
    "HookRegistration",
    "clear_hooks",
    "drop_hooks_for_kit",
    "drop_hooks_for_module",
    "fire_hooks",
    "hook_count",
    "hooks_enabled",
    "hooks_for",
    "hooks_registered",
    "is_hook_hint",
    "list_hooks",
    "on_agent_hook",
    "should_fire",
    "spec_for",
]
