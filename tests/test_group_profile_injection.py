"""群画像注入：只留本群证据，全球歧义别名不进 system。"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict
from unittest.mock import patch

from gsuid_core.ai_core.memory.group_profile import (
    format_context_injection,
    format_group_term_mappings,
)


def _profile(**kwargs: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "scope_key": "group:GA",
        "tag_counts": {},
        "term_mappings": {},
        "member_alias_ids": {},
        "member_aliases": {},
        "last_updated": "",
    }
    row.update(kwargs)
    return row


def test_format_context_keeps_group_mappings_and_drops_global_alias_dump() -> None:
    async def _fake(_scope_key: str) -> Dict[str, Any]:
        return _profile(
            tag_counts={"手册": 3},
            term_mappings={"EastHill": "AcmeCorp"},
            member_alias_ids={"小甲": ["u1"]},
        )

    async def _run() -> tuple[str, str]:
        with patch("gsuid_core.ai_core.memory.group_profile.get_group_profile", new=_fake):
            stable = await format_context_injection("group:GA")
            terms = await format_group_term_mappings("group:GA")
            return stable, terms

    stable, terms = asyncio.run(_run())
    assert "小甲" in stable
    assert "EastHill" not in stable
    assert "EastHill" in terms
    assert "AcmeCorp" in terms
    assert "可能的别名歧义" not in stable
    assert "可能的别名歧义" not in terms


def test_format_context_redacts_persona_member_alias_collision() -> None:
    async def _fake(_scope_key: str) -> Dict[str, Any]:
        return _profile(
            member_alias_ids={
                "早柚": ["u123"],
                "小甲": ["u1"],
                "达妮娅": ["u9", "u8"],
            },
        )

    async def _run() -> str:
        with patch("gsuid_core.ai_core.memory.group_profile.get_group_profile", new=_fake):
            return await format_context_injection(
                "group:GA",
                persona_surfaces=("早柚", "达妮娅"),
            )

    text = asyncio.run(_run())
    assert '"早柚" = 他人称呼（不是你）' in text
    assert '"达妮娅" = 他人称呼（不是你）' in text
    assert '"早柚" = 用户u123' not in text
    assert "用户u123" not in text
    assert "用户u9" not in text
    assert "用户u8" not in text
    assert '"小甲" = 用户u1' in text


def test_fallback_stable_context_threads_persona_surfaces() -> None:
    from gsuid_core.ai_core.context_assembly import build_stable_context, build_session_system_prompt

    src = inspect.getsource(build_stable_context)
    assert "collect_persona_surfaces" in src
    assert "persona_surfaces=" in src
    call_src = inspect.getsource(build_session_system_prompt)
    assert "build_stable_context(" in call_src
    assert "persona_name" in call_src


def test_format_context_redacts_persona_surface_collision() -> None:
    async def _fake(_scope_key: str) -> Dict[str, Any]:
        return _profile(
            term_mappings={"FrostAlias": "PersonaOne", "EastHill": "AcmeCorp"},
        )

    async def _run() -> str:
        with patch("gsuid_core.ai_core.memory.group_profile.get_group_profile", new=_fake):
            return await format_context_injection(
                "group:GA",
                persona_surfaces=("PersonaOne", "FrostAlias"),
                include_term_mappings=True,
            )

    text = asyncio.run(_run())
    assert "他人昵称（不是你）" in text
    assert "AcmeCorp" in text
    assert '"EastHill" = AcmeCorp' in text
    assert '"FrostAlias" = 他人昵称（不是你）' in text


def test_format_context_source_has_no_global_alias_table() -> None:
    src = inspect.getsource(format_context_injection)
    assert "get_aliases_for_scope" not in src
    assert "可能的别名歧义" not in src
