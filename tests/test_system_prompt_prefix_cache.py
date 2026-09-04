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
        clock_date: str | None = None,
    ) -> str:
        return f"P:{char_name}|G:{group_description}|S:{extra_stable_context}|M:{mood_key}|C:{clock_date}"

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
        assert a.endswith("|M:None|C:None")

    asyncio.run(_run())


def test_system_prompt_ignores_inject_date_in_event_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产装配不得从用户原文解析「当前时间：」写进 system（§1.7）。"""
    from gsuid_core.ai_core import persona as persona_pkg, context_assembly as ca
    from gsuid_core.ai_core.persona import group_context as group_mod

    captured: dict[str, str | None] = {}

    async def _persona(
        char_name: str,
        mood_key: str | None = None,
        group_description: str | None = None,
        extra_stable_context: str | None = None,
        clock_date: str | None = None,
    ) -> str:
        captured["clock_date"] = clock_date
        return "P"

    async def _group(*, group_id: str) -> str:
        return "g"

    async def _stable(_event: Event, _persona_name: str = "") -> str:
        return "s"

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
    ev.raw_text = "当前时间：2023/04/18 16:50\n\nhello"
    ev.text = ev.raw_text

    asyncio.run(ca.build_session_system_prompt(ev, "角色甲"))
    assert captured["clock_date"] is None
    labeled = asyncio.run(ca.build_session_system_prompt(ev, "角色甲", clock_date="2023年04月18日（星期二）"))
    assert labeled == "P"
    assert captured["clock_date"] == "2023年04月18日（星期二）"
