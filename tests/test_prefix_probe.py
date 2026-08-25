"""前缀缓存探针：system / tools / none / session_new 互补切面。"""

from __future__ import annotations

from gsuid_core.ai_core.prefix_probe import (
    PrefixSnapshot,
    hash_text,
    tools_diff,
    classify_prefix_break,
    reset_prefix_break_counts,
)


def test_prefix_break_classifies_tools_and_system() -> None:
    reset_prefix_break_counts()
    prev = PrefixSnapshot(
        history_hashes=["aaaa"],
        tools_hash=hash_text("a\nb"),
        system_hash=hash_text("sys1"),
        payloads=["user:hi"],
    )
    assert (
        classify_prefix_break(
            prev,
            history_hashes=["aaaa"],
            tools_hash=hash_text("a\nb"),
            system_hash=hash_text("sys2"),
        )
        == "system"
    )
    assert (
        classify_prefix_break(
            prev,
            history_hashes=["aaaa"],
            tools_hash=hash_text("a\nc"),
            system_hash=hash_text("sys1"),
        )
        == "tools"
    )


def test_tools_diff_and_prefix_break_none_on_identical() -> None:
    names = ["create_subagent", "find_tools", "web_search_tool"]
    prev = PrefixSnapshot(
        history_hashes=["aaaa"],
        tools_hash=hash_text("\n".join(names)),
        system_hash=hash_text("sys"),
        payloads=["user:hi"],
        tool_names=list(names),
    )
    assert (
        classify_prefix_break(
            prev,
            history_hashes=["aaaa"],
            tools_hash=hash_text("\n".join(names)),
            system_hash=hash_text("sys"),
        )
        == "none"
    )
    d = tools_diff(names, names + ["search_cognition"])
    assert d["added"] == ["search_cognition"]
    assert d["removed"] == []


def test_prefix_probe_session_new_and_multi_label() -> None:
    assert classify_prefix_break(None, history_hashes=["a"], tools_hash="t", system_hash="s") == "session_new"
    prev = PrefixSnapshot(
        history_hashes=["a"],
        tools_hash=hash_text("x"),
        system_hash=hash_text("old"),
        payloads=["user:hi"],
        tool_names=["find_tools"],
    )
    assert (
        classify_prefix_break(
            prev,
            history_hashes=["a"],
            tools_hash=hash_text("y"),
            system_hash=hash_text("new"),
        )
        == "system+tools"
    )
