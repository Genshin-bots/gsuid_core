"""Phase 5–7：线程聚类、修订问句、双时态标记、预测-校准余弦。离线无 LLM。"""

from gsuid_core.ai_core.memory.ingestion.edge import classify_edge_write
from gsuid_core.ai_core.memory.retrieval.lexical import looks_like_code_lead
from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_revision_query
from gsuid_core.ai_core.memory.lifecycle.sleep_extract import _title_key, cosine_dense


def test_cosine_dense_identical_and_orthogonal() -> None:
    assert cosine_dense([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_dense([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine_dense([], [1.0]) == 0.0


def test_title_key_normalizes() -> None:
    assert _title_key("Hello   World") == _title_key("hello world")


def test_official_eo_wording_is_order_query() -> None:
    from gsuid_core.ai_core.memory.retrieval.event_time import looks_like_order_query

    q_list = (
        "Can you list the order in which I brought up different aspects of "
        "developing my personal budget tracker throughout our conversations?"
    )
    q_walk = (
        "Can you walk me through the order in which I brought up different "
        "aspects of my app development and deployment across our conversations?"
    )
    assert looks_like_order_query(q_list)
    assert looks_like_order_query(q_walk)
    assert not looks_like_order_query("Mention ONLY and ONLY three items.")


def test_revision_query_linguistic() -> None:
    assert looks_like_revision_query("What is my current address?")
    assert looks_like_revision_query("Did I say A or did I change it?")
    assert looks_like_revision_query("我现在是住北京还是上海")
    assert not looks_like_revision_query("Can you list the order of the stages?")


def test_conflict_still_classifies() -> None:
    assert classify_edge_write("user likes coffee", "user does not like coffee") == "conflict"
    assert classify_edge_write("user lives in A", "user lives in A") == "merge"


def test_memory_event_cue_typed() -> None:
    from gsuid_core.ai_core.memory.retrieval.types import MemoryEventCue

    cue: MemoryEventCue = {
        "summary": "started tracker",
        "stated_at": "2024-03-14 09:00:00",
        "event_at": "",
        "turn_episode_id": "e1",
        "thread_id": "t1",
        "source": "llm",
    }
    assert cue["summary"].startswith("started")


def test_code_lead_not_a_stage() -> None:
    assert looks_like_code_lead("id = Column(Integer, primary=True)")
    assert not looks_like_code_lead("I started tracking expenses last March")
