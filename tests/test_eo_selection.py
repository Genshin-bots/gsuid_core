"""EO 选阶段：代码行过滤、主题分、对话包 2N–3N、oracle 短语。离线无 LLM。"""

from eval.BEAM_official.oracle import (
    gold_phrases,
    best_episode_id,
    map_gold_episode_ids,
    first_source_chat_ids,
)
from gsuid_core.ai_core.memory.retrieval.types import Episode
from gsuid_core.ai_core.memory.retrieval.lexical import pack_order_dialogue
from gsuid_core.ai_core.memory.retrieval.order_reconstruct import select_by_topic_scores


def _ep(eid: str, content: str, day: str, sid: str) -> Episode:
    return {
        "id": eid,
        "content": content,
        "valid_at": f"{day} 09:00:00",
        "scope_key": "user_global:u",
        "embedding": [],
        "session_id": sid,
        "turn_index": 0,
    }


def test_select_by_topic_scores_identity() -> None:
    eps = [
        _ep("a", "user: income tracking", "2024-03-15", "s1"),
        _ep("b", "user: transaction error handling", "2024-05-02", "s2"),
        _ep("c", "user: security before deploy", "2024-07-18", "s3"),
    ]
    out = select_by_topic_scores(eps, [0.9, 0.8, 0.7], 3)
    assert [e["id"] for e in out] == ["a", "b", "c"]
    assert select_by_topic_scores(eps, [0.1], 2) == []


def test_to_prompt_includes_same_session_followup() -> None:
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    q = (
        "Can you list the order in which I brought up different aspects of developing "
        "my personal budget tracker throughout our conversations, in order? "
        "Mention three items."
    )
    eps = [
        _ep("core", "User: I started the budget tracker core authentication module.", "2024-03-14", "s1"),
        _ep("core2", "User: Still polishing the budget tracker core authentication.", "2024-03-20", "s1"),
        _ep("err", "User: Implementing transaction creation with proper error handling.", "2024-04-05", "s2"),
        _ep("sec", "User: Finalizing security hashing and deployment checklist.", "2024-04-25", "s3"),
    ]
    text = MemoryContext(episodes=eps).to_prompt_text(max_chars=8000, query=q)
    assert "【事件顺序" in text
    assert "Still polishing" in text


def test_pack_order_dialogue_cap_is_2n() -> None:
    eps = [
        _ep("a", "user: first aspect about budgets", "2024-03-15", "s1"),
        _ep("a2", "user: later note in same session about charts", "2024-03-15", "s1"),
        _ep("b", "user: second aspect about errors", "2024-05-02", "s2"),
        _ep("c", "user: third aspect about security", "2024-07-18", "s3"),
    ]
    packed = pack_order_dialogue(eps, "list the order of aspects", 6)
    assert 3 <= len(packed) <= 6
    assert packed[0]["id"] == "a"


def test_gold_phrases_from_numbered_answer() -> None:
    items = gold_phrases(
        "1. Core functionality\n2. Transaction error handling\n3. Security hardening",
        [],
    )
    assert items == [
        "Core functionality",
        "Transaction error handling",
        "Security hardening",
    ]


def test_first_source_chat_ids_nested() -> None:
    assert first_source_chat_ids([[24, 26, 28], [146], [202]]) == ["24", "146", "202"]
    assert first_source_chat_ids([4, 60, 116]) == ["4", "60", "116"]


def test_map_gold_uses_source_ids_not_regex() -> None:
    turns = [
        {"turn_id": "4", "content": "I want a budget tracker with auth and charts"},
        {"turn_id": "60", "content": "transaction create needs error handling now"},
        {"turn_id": "116", "content": "security and deploy before we go live"},
    ]
    eps = [
        {"id": "e4", "content": "I want a budget tracker with auth and charts"},
        {"id": "e60", "content": "transaction create needs error handling now"},
        {"id": "e116", "content": "security and deploy before we go live"},
    ]
    mapped, unmap = map_gold_episode_ids("no chat_id here", [], turns, eps, source_ids=["4", "60", "116"])
    assert mapped == ["e4", "e60", "e116"]
    assert unmap == []


def test_best_episode_id_token_overlap() -> None:
    rows = [
        {"id": "e1", "content": "I want income and expense tracking with basic analytics"},
        {"id": "e2", "content": "we should add try except around transaction writes"},
    ]
    assert best_episode_id("transaction error handling try except", rows) == "e2"
    assert best_episode_id("zzzz not in any turn at all xyzabc", rows) == ""
