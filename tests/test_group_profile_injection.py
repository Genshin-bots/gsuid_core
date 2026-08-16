"""群画像注入：只留本群证据，全球歧义别名不进 system。"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict
from unittest.mock import patch

from gsuid_core.ai_core.memory.group_profile import format_context_injection


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

    async def _run() -> str:
        with patch("gsuid_core.ai_core.memory.group_profile.get_group_profile", new=_fake):
            return await format_context_injection("group:GA")

    text = asyncio.run(_run())
    assert "EastHill" in text
    assert "AcmeCorp" in text
    assert "小甲" in text
    assert "可能的别名歧义" not in text


def test_format_context_source_has_no_global_alias_table() -> None:
    src = inspect.getsource(format_context_injection)
    assert "get_aliases_for_scope" not in src
    assert "可能的别名歧义" not in src
