"""AI 历史列表在日志目录没变时不应重读每个文件。

页面每次打开都会拉列表、概览和分类。这三处共用 ``_build_unified_list_cached``。
目录签名没变时，第二次调用不得再解析日志文件。
"""

from __future__ import annotations

import os
import json
from pathlib import Path

import pytest

import gsuid_core.webconsole.ai_session_logs_api as mod
from gsuid_core.webconsole.ai_session_logs_api import SessionLogSummary


class _EmptyRegistry:
    def get_all_ai_sessions(self) -> dict[str, object]:
        return {}

    def get_ai_session(self, _session_id: str) -> None:
        return None


def _write_log(directory: Path, name: str, session_id: str, chain_id: str) -> Path:
    payload = {
        "session_id": session_id,
        "session_uuid": chain_id,
        "persona_name": "p",
        "create_by": "Chat",
        "is_subagent": False,
        "chain_id": chain_id,
        "segment_index": 0,
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_100.0,
        "ended_at": 1_700_000_100.0,
        "entry_count": 1,
        "type_counts": {"user_message": 1},
        "linked_agents": [],
        "linked_agent_count": 0,
        "entries": [{"type": "user_message", "timestamp": 1.0, "data": {"text": name}}],
    }
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _ids(cards: list[SessionLogSummary]) -> list[str]:
    return sorted(card["session_id"] for card in cards)


def test_unchanged_log_directory_is_not_reparsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "session_logs"
    subs = tmp_path / "subagents"
    logs.mkdir()
    subs.mkdir()
    _write_log(logs, "a.json", "sess-a", "chain-a")
    changed = _write_log(logs, "b.json", "sess-b", "chain-b")

    monkeypatch.setattr(mod, "AI_SESSION_LOGS_PATH", logs)
    monkeypatch.setattr(mod, "AI_SUBAGENT_LOGS_PATH", subs)
    monkeypatch.setattr(mod, "_PERSIST_CACHE_PATH", tmp_path / "summary_cache.json")
    monkeypatch.setattr(mod, "_BASE_SUMMARY_CACHE", {})
    monkeypatch.setattr(mod, "_unified_cache", None)
    monkeypatch.setattr(mod, "_index_cache", None)
    monkeypatch.setattr(mod, "_cache_loaded", False)
    monkeypatch.setattr(mod, "_cache_dirty", False)
    monkeypatch.setattr(mod, "_last_persist_save", 0.0)
    monkeypatch.setattr(mod, "get_ai_session_registry", lambda: _EmptyRegistry())

    calls = {"n": 0}
    original = mod._parse_log_file_base

    def _counting(path: Path, st: os.stat_result | None = None) -> SessionLogSummary | None:
        calls["n"] += 1
        return original(path, st)

    monkeypatch.setattr(mod, "_parse_log_file_base", _counting)

    first = mod._build_unified_list_cached()
    parsed_once = calls["n"]
    assert _ids(first) == ["sess-a", "sess-b"]
    assert parsed_once == 2

    second = mod._build_unified_list_cached()
    assert _ids(second) == ["sess-a", "sess-b"]
    assert calls["n"] == parsed_once

    payload = json.loads(changed.read_text(encoding="utf-8"))
    payload["persona_name"] = "renamed-persona"
    changed.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    third = mod._build_unified_list_cached()
    assert _ids(third) == ["sess-a", "sess-b"]
    assert calls["n"] > parsed_once
