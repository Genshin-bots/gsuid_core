"""前缀缓存行为：同一 Event 连调两次，system prompt 字节不变。"""

from __future__ import annotations

import asyncio

import pytest

from gsuid_core.models import Event


def test_system_prompt_bytes_stable_across_two_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core import persona as persona_pkg, context_assembly as ca
    from gsuid_core.ai_core.persona import group_context as group_mod

    async def _persona(
        char_name: str,
        mood_key: str | None = None,
        group_description: str | None = None,
        extra_stable_context: str | None = None,
    ) -> str:
        return f"P:{char_name}|G:{group_description}|S:{extra_stable_context}|M:{mood_key}"

    async def _group(*, group_id: str) -> str:
        return f"group:{group_id}"

    async def _stable(_event: Event, _persona_name: str = "") -> str:
        return "stable-fixed"

    monkeypatch.setattr(persona_pkg, "build_persona_prompt", _persona)
    monkeypatch.setattr(group_mod, "get_group_context", _group)
    monkeypatch.setattr(ca, "fire_stable_context_hooks", _stable)

    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        WS_BOT_ID="ws1",
    )

    async def _run() -> None:
        a = await ca.build_session_system_prompt(ev, "角色甲")
        b = await ca.build_session_system_prompt(ev, "角色甲")
        assert a == b
        assert a.startswith("P:角色甲")
        assert a.endswith("|M:None")

    asyncio.run(_run())
