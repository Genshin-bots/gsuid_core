"""认知层：类型契约 / RRF 融合 / 相对分下限 / 节点是索引层 / 蒸馏门。

不变量（先写死，防「一把梭合成一张表」）：
1. SQL 仍是各域真值，Qdrant 仍是索引——节点层不存第二份正文；
2. 语义类型保留（六类互不覆盖）；
3. scope/ACL 不降级，过滤下推到各后端；
4. D-11 精神保留：自动层只许目录卡 + 句柄，深读走工具。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, patch

from gsuid_core.ai_core.cognition import (
    ALL_KINDS,
    MEMORY_KINDS,
    KNOWLEDGE_KINDS,
    DEFAULT_RECALL_KINDS,
    SPEAKER_RECALL_KINDS,
    CogKind,
    CogScope,
    CognitiveHit,
    kinds_from_names,
    resolve_recall_kinds,
    query_mentions_speaker,
    strip_speaker_from_query,
)
from gsuid_core.ai_core.cognition.facade import render_cognition_block


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _empty_group_profile_patch() -> Any:
    async def _profile(scope_key: str) -> dict[str, object]:
        return {
            "scope_key": scope_key,
            "tag_counts": {},
            "term_mappings": {},
            "member_alias_ids": {},
            "member_aliases": {},
            "last_updated": "",
        }

    return patch("gsuid_core.ai_core.memory.group_profile.get_group_profile", new=_profile)


def test_scope_and_kinds_have_no_internal_default() -> None:
    """两个真实 bug 的共同根因是「可选参数被内部兜底成看起来合理的值」。"""
    from gsuid_core.ai_core.cognition import search_cognition

    sig = inspect.signature(search_cognition)
    for name in ("kinds", "scope"):
        param = sig.parameters[name]
        assert param.default is inspect.Parameter.empty, f"{name} 不许有默认值"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_private_scope_is_none_not_user_id() -> None:
    """私聊 ``group_id=None``：回退成 user_id 会去查一个空的幻影 group:{user_id}。"""
    private = CogScope(user_id="u1", group_id=None)
    assert private.is_private and private.group_id is None
    group = CogScope(user_id="u1", group_id="g1")
    assert not group.is_private


def test_scope_defaults_are_conservative() -> None:
    """开发文档整类默认不对普通用户暴露；System-2 默认不开。"""
    scope = CogScope(user_id="u1")
    assert not scope.include_skill_doc
    assert not scope.enable_system2


def test_empty_result_is_one_short_line() -> None:
    """空结果只回一行——历史上要拼「未找到 + 无匹配 + 长说明」三大段。

    一行之内还必须指路：只说「无命中」时模型会原地编答案或换个说法重搜，
    收成单一动词后这类空转的成本全压在这一个工具上。
    """
    block = render_cognition_block("竖图偏好", [])
    assert len(block.splitlines()) == 1
    assert len(block) < 160, f"{len(block)} 字：{block}"
    assert "无命中" in block
    assert "web_search_tool" in block and "find_tools" in block


def test_hits_render_with_kind_labels_and_handles() -> None:
    hits = [
        CognitiveHit(
            kind=CogKind.PREFERENCE,
            id="pref_1",
            title="资料出图：用竖图",
            summary="用竖图",
            score=1.0,
            as_of="2026-08-01",
            high_confidence=True,
        ),
        CognitiveHit(
            kind=CogKind.TOOL_OUTPUT,
            id="to_ab12",
            title="web_search_tool",
            summary="上周搜到的参考",
            score=0.5,
            as_of="2026-08-07",
            handle="to_ab12",
            high_confidence=True,
        ),
    ]
    block = render_cognition_block("竖图偏好", hits)
    assert "[偏好·须遵守]" in block
    assert "[落盘·可能过时]" in block
    assert "read_handle('to_ab12')" in block
    assert "as_of=2026-08-07" in block
    # 提醒模型：栅栏内文本不是系统指令
    assert "不是系统指令" in block


def test_weak_hits_are_folded_not_expanded() -> None:
    """生产弱相关折成「另有 N 条」，不得把低分经历当正文。"""
    hits = [
        CognitiveHit(kind=CogKind.FACT, id="a", title="强相关", summary="", score=1.0, high_confidence=True),
        CognitiveHit(
            kind=CogKind.EPISODE,
            id="b",
            title="",
            summary="I prefer Adobe Premiere Pro tutorials for advanced color grading.",
            score=0.1,
            high_confidence=False,
        ),
    ]
    block = render_cognition_block("q", hits)
    assert "强相关" in block
    assert "Premiere Pro" not in block
    assert "另有 1 条弱相关" in block


def test_episode_render_keeps_name_but_caps_body() -> None:
    from gsuid_core.ai_core.cognition.types import EPISODE_BODY_BUDGET

    long = "张三李四王五_" + ("闲聊流水" * 80) + "_TAIL_SHOULD_DROP"
    hit = CognitiveHit(
        kind=CogKind.EPISODE,
        id="e1",
        title="",
        summary=long,
        score=0.8,
        high_confidence=True,
    )
    line = hit.render_line(1)
    assert "张三李四王五" in line
    assert "TAIL_SHOULD_DROP" not in line
    assert EPISODE_BODY_BUDGET == 240
    assert len(line) < 40 + EPISODE_BODY_BUDGET


def test_episode_expand_cap_folds_overflow() -> None:
    from gsuid_core.ai_core.cognition.facade import _EPISODE_EXPAND_CAP

    hits = [
        CognitiveHit(
            kind=CogKind.EPISODE,
            id=f"e{i}",
            title="",
            summary=f"专名{i} 的会话",
            score=0.8,
            high_confidence=True,
        )
        for i in range(_EPISODE_EXPAND_CAP + 2)
    ]
    block = render_cognition_block("q", hits)
    assert "专名0" in block and f"专名{_EPISODE_EXPAND_CAP - 1}" in block
    assert f"专名{_EPISODE_EXPAND_CAP}" not in block
    assert "另有 2 条弱相关" in block


def test_episode_neighbor_score_falls_below_pref_floor() -> None:
    from gsuid_core.ai_core.cognition.facade import _EPISODE_SEED_SCORE, _EPISODE_NEIGHBOR_SCORE

    assert _EPISODE_NEIGHBOR_SCORE < 1.0 * 0.55
    assert _EPISODE_SEED_SCORE >= 1.0 * 0.55


def test_kinds_from_names_ignores_unknown() -> None:
    assert kinds_from_names({"knowledge", "fact"}) == frozenset({CogKind.KNOWLEDGE, CogKind.FACT})
    assert kinds_from_names({"nonsense"}) == frozenset()
    assert kinds_from_names({" Knowledge "}) == frozenset({CogKind.KNOWLEDGE})


def test_resolve_recall_kinds_defaults_and_speaker_query() -> None:
    uid = "user_web_01"
    empty: frozenset[CogKind] = frozenset()
    assert resolve_recall_kinds(empty, query="今天怎样", user_id=uid) == DEFAULT_RECALL_KINDS
    assert resolve_recall_kinds(empty, query=f"{uid} 所在地", user_id=uid) == SPEAKER_RECALL_KINDS
    asked = frozenset({CogKind.KNOWLEDGE})
    assert resolve_recall_kinds(asked, query=f"{uid} 所在地", user_id=uid) == asked
    assert query_mentions_speaker(f"{uid} 所在地", uid)
    assert not query_mentions_speaker("user_web_010 所在地", uid)
    assert not query_mentions_speaker("今天怎样", uid)
    assert CogKind.EPISODE in resolve_recall_kinds(empty, query=f"{uid} 所在地", user_id=uid)
    assert CogKind.KNOWLEDGE not in resolve_recall_kinds(empty, query=f"{uid} 所在地", user_id=uid)


def test_speaker_recall_does_not_open_recent_history() -> None:
    """说话人面不得因 EPISODE 误开近窗。"""
    from gsuid_core.ai_core.cognition import search_cognition

    hist_n = {"n": 0}

    async def _hist(*args: object, **kwargs: object) -> tuple[list[str], dict[str, CognitiveHit]]:
        hist_n["n"] += 1
        _ = (args, kwargs)
        return [], {}

    async def _mem(*args: object, **kwargs: object) -> tuple[list[str], dict[str, CognitiveHit]]:
        _ = (args, kwargs)
        return [], {}

    with (
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=_hist),
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=_mem),
        patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=_mem),
        patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=_mem),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=_mem),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=_mem),
    ):
        _run(
            search_cognition(
                "user_web_01 所在地",
                kinds=SPEAKER_RECALL_KINDS,
                scope=CogScope(user_id="user_web_01"),
                limit=8,
            )
        )
        speaker_n = hist_n["n"]
        _run(
            search_cognition(
                "今天怎样",
                kinds=DEFAULT_RECALL_KINDS,
                scope=CogScope(user_id="user_web_01"),
                limit=8,
            )
        )
        default_n = hist_n["n"]
    assert speaker_n == 0
    assert default_n == 1


def test_speaker_recall_skips_index_nodes() -> None:
    """说话人面不跑节点索引，避免公共实体挤掉 episode。"""
    from gsuid_core.ai_core.cognition import search_cognition

    node_n = {"n": 0}

    async def _nodes(*args: object, **kwargs: object) -> tuple[list[str], dict[str, CognitiveHit]]:
        node_n["n"] += 1
        _ = (args, kwargs)
        return [], {}

    async def _mem(*args: object, **kwargs: object) -> tuple[list[str], dict[str, CognitiveHit]]:
        _ = (args, kwargs)
        return [], {}

    with (
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=_nodes),
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=_mem),
        patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=_mem),
        patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=_mem),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=_mem),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=_mem),
    ):
        _run(
            search_cognition(
                "user_web_01 所在地",
                kinds=SPEAKER_RECALL_KINDS,
                scope=CogScope(user_id="user_web_01"),
                limit=8,
            )
        )
        speaker_nodes = node_n["n"]
        _run(
            search_cognition(
                "秧秧技能",
                kinds=DEFAULT_RECALL_KINDS,
                scope=CogScope(user_id="user_web_01"),
                limit=8,
            )
        )
        default_nodes = node_n["n"]
    assert speaker_nodes == 0
    assert default_nodes == 1


def test_strip_speaker_from_query_keeps_slot_terms() -> None:
    uid = "eval_8a2466db"
    q = f"{uid} Premiere Pro tutorials"
    assert strip_speaker_from_query(q, uid) == "Premiere Pro tutorials"
    assert strip_speaker_from_query("所在地", uid) == "所在地"
    assert strip_speaker_from_query(uid, uid) == uid


def test_relative_score_floor_marks_high_confidence() -> None:
    """相对分下限：只有过门槛的条目才允许标高置信。"""
    from gsuid_core.ai_core.cognition import search_cognition

    strong = CognitiveHit(kind=CogKind.FACT, id="s", title="strong", summary="", score=1.0)
    weak = CognitiveHit(kind=CogKind.FACT, id="w", title="weak", summary="", score=0.05)

    async def _fake_memory(query: str, *, kinds: Any, scope: Any, limit: int, **_kw: Any) -> Any:
        return ["s", "w"], {"s": strong, "w": weak}

    async def _empty(*args: Any, **kwargs: Any) -> Any:
        return [], {}

    with (
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=_fake_memory),
        patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_records", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_images", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_memes", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=_empty),
    ):
        hits = _run(search_cognition("q", kinds=MEMORY_KINDS, scope=CogScope(user_id="u1"), limit=10))

    by_id = {h.id: h for h in hits}
    assert by_id["s"].high_confidence
    assert not by_id["w"].high_confidence


def _empty_backend(*args: Any, **kwargs: Any) -> Any:
    async def _empty(*_a: Any, **_k: Any) -> Any:
        return [], {}

    return _empty


def test_fused_rank_caps_high_confidence() -> None:
    """知识/落盘融合名次收口；记忆事实/片段不过这条帽。"""
    from gsuid_core.ai_core.cognition import search_cognition

    packed = {
        f"m{i}": CognitiveHit(kind=CogKind.FACT, id=f"m{i}", title=f"t{i}", summary="", score=1.0) for i in range(6)
    }

    async def _fake_memory(query: str, *, kinds: Any, scope: Any, limit: int, **_kw: Any) -> Any:
        _ = (query, kinds, scope, limit)
        return [f"m{i}" for i in range(6)], packed

    empty = _empty_backend()
    with (
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=_fake_memory),
        patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_records", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_images", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_memes", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_outbound", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=empty),
    ):
        hits = _run(search_cognition("q", kinds=MEMORY_KINDS, scope=CogScope(user_id="u1"), limit=10))
    assert len(hits) == 6
    assert sum(1 for h in hits if h.high_confidence) == 6
    assert hits[4].high_confidence


def test_fused_rank_caps_knowledge_noise() -> None:
    """公共知识路仍只展开前 4 条高置信，避免插件文淹没记忆。"""
    from gsuid_core.ai_core.cognition import search_cognition

    packed = {
        f"k{i}": CognitiveHit(kind=CogKind.KNOWLEDGE, id=f"k{i}", title=f"kb{i}", summary="", score=1.0)
        for i in range(6)
    }

    async def _fake_kb(query: str, *, scope: Any, limit: int) -> Any:
        _ = (query, scope, limit)
        return [f"k{i}" for i in range(6)], packed

    empty = _empty_backend()
    with (
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=_fake_kb),
        patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_records", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_images", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_memes", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_outbound", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=empty),
    ):
        hits = _run(search_cognition("q", kinds=KNOWLEDGE_KINDS, scope=CogScope(user_id="u1"), limit=10))
    assert len(hits) == 6
    assert sum(1 for h in hits if h.high_confidence) == 4
    assert hits[0].high_confidence
    assert not hits[4].high_confidence


def test_memory_hits_are_not_evicted_by_knowledge_rrf() -> None:
    """记忆路先占满 limit；知识不得把个人片段挤出前排。"""
    from gsuid_core.ai_core.cognition import search_cognition

    mem_hits = {
        f"m{i}": CognitiveHit(kind=CogKind.EPISODE, id=f"m{i}", title=f"ep{i}", summary="", score=0.8) for i in range(8)
    }
    kb_hits = {
        f"k{i}": CognitiveHit(kind=CogKind.KNOWLEDGE, id=f"k{i}", title=f"kb{i}", summary="", score=1.0)
        for i in range(8)
    }

    async def _fake_memory(query: str, *, kinds: Any, scope: Any, limit: int, **_kw: Any) -> Any:
        _ = (query, kinds, scope, limit)
        return [f"m{i}" for i in range(8)], mem_hits

    async def _fake_kb(query: str, *, scope: Any, limit: int) -> Any:
        _ = (query, scope, limit)
        return [f"k{i}" for i in range(8)], kb_hits

    empty = _empty_backend()
    with (
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=_fake_memory),
        patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=_fake_kb),
        patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_records", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_images", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_memes", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_outbound", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=empty),
    ):
        hits = _run(
            search_cognition(
                "how many projects",
                kinds=MEMORY_KINDS | KNOWLEDGE_KINDS,
                scope=CogScope(user_id="u1"),
                limit=8,
            )
        )
    assert [h.id for h in hits] == [f"m{i}" for i in range(8)]
    assert all(h.kind is CogKind.EPISODE for h in hits)


def test_named_knowledge_keeps_slots_when_memory_fills_limit() -> None:
    """片段占满 limit 时，标题带专名的知识仍留下，无关知识不占名额。"""
    from gsuid_core.ai_core.cognition import search_cognition

    mem_hits = {
        f"m{i}": CognitiveHit(kind=CogKind.EPISODE, id=f"m{i}", title=f"ep{i}", summary="chat", score=0.8)
        for i in range(8)
    }
    kb_hits = {
        "k_hit": CognitiveHit(
            kind=CogKind.KNOWLEDGE,
            id="k_hit",
            title="北站手册-基础信息",
            summary="station",
            score=1.0,
        ),
        "k_miss": CognitiveHit(
            kind=CogKind.KNOWLEDGE,
            id="k_miss",
            title="无关条目",
            summary="other",
            score=0.9,
        ),
    }

    async def _fake_memory(query: str, *, kinds: Any, scope: Any, limit: int, **_kw: Any) -> Any:
        _ = (query, kinds, scope, limit)
        return [f"m{i}" for i in range(8)], mem_hits

    async def _fake_kb(query: str, *, scope: Any, limit: int) -> Any:
        _ = (query, scope, limit)
        return ["k_hit", "k_miss"], kb_hits

    empty = _empty_backend()
    with (
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=_fake_memory),
        patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=_fake_kb),
        patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_records", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_images", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_memes", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_outbound", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=empty),
    ):
        hits = _run(
            search_cognition(
                "北站手册 和另一本",
                kinds=MEMORY_KINDS | KNOWLEDGE_KINDS,
                scope=CogScope(user_id="u1"),
                limit=8,
            )
        )
    ids = [h.id for h in hits]
    assert ids[0].startswith("m")
    assert "k_hit" in ids
    assert "k_miss" not in ids
    assert hits[ids.index("k_hit")].high_confidence


def test_search_cognition_drops_weak_episodes_without_needles() -> None:
    """专名不在正文里的片段不得以「命中 24」展开。"""
    from gsuid_core.ai_core.cognition import search_cognition

    packed = {
        "ep_hit": CognitiveHit(
            kind=CogKind.EPISODE,
            id="ep_hit",
            title="",
            summary="Johnny reviewed the tuning logic.",
            score=0.8,
        ),
        "ep_miss": CognitiveHit(
            kind=CogKind.EPISODE,
            id="ep_miss",
            title="",
            summary="We discussed RAG sharding and dense search.",
            score=0.8,
        ),
    }

    async def _fake_memory(query: str, *, kinds: Any, scope: Any, limit: int, **_kw: Any) -> Any:
        _ = (query, kinds, scope, limit)
        return ["ep_hit", "ep_miss"], packed

    empty = _empty_backend()
    with (
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=_fake_memory),
        patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_records", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_images", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_memes", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_outbound", new=empty),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=empty),
    ):
        hits = _run(
            search_cognition(
                "Does Johnny have expertise?",
                kinds=MEMORY_KINDS,
                scope=CogScope(user_id="u1"),
                limit=10,
            )
        )
    ids = [h.id for h in hits]
    assert "ep_hit" in ids
    assert "ep_miss" not in ids


def test_speaker_query_keeps_location_facts_without_userid() -> None:
    """地点事实常是「住在杭州」，字面没有 user_id，不得整表过滤成零命中。"""
    from gsuid_core.ai_core.cognition.facade import _search_memory
    from gsuid_core.ai_core.memory.retrieval.types import Edge
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    def _edge(source: str, fact: str, eid: str) -> Edge:
        return Edge(
            id=eid,
            source_id=f"src_{eid}",
            target_id=f"tgt_{eid}",
            source_name=source,
            target_name="",
            fact=fact,
            weight=0.9,
            score=0.9,
            valid_at_ts=None,
            invalid_at_ts=None,
        )

    async def _fake(*args: object, **kwargs: object) -> MemoryContext:
        _ = (args, kwargs)
        return MemoryContext(
            edges=[
                _edge("某站", "发布在该网站", "e2"),
                _edge("小明", "住在杭州", "e1"),
                _edge("user_web_01", "user_web_01 喜欢早起", "e0"),
                _edge("user_web_01", "用户user_web_01提到", "e3"),
            ]
        )

    async def _no_boost(*args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        return None

    with (
        patch("gsuid_core.ai_core.memory.retrieval.dual_route.dual_route_retrieve", new=_fake),
        patch("gsuid_core.ai_core.kits.memory.eval_protocol.boost_retrieved_memory", new=_no_boost),
    ):
        ids, hits = _run(
            _search_memory(
                "user_web_01 所在地",
                kinds=SPEAKER_RECALL_KINDS,
                scope=CogScope(user_id="user_web_01"),
                limit=8,
            )
        )
    titles = [hits[i].title for i in ids]
    assert "住在杭州" in titles
    assert titles[0] == "user_web_01 喜欢早起"
    assert all(not t.endswith("提到") for t in titles)


def test_speaker_recall_kinds_return_episodes() -> None:
    """点名说话人 ID 必须带回片段。extract 关闭时只有 Episode，不含片段会假「无命中」。"""
    from gsuid_core.ai_core.cognition.facade import _search_memory
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    async def _fake(*args: object, **kwargs: object) -> MemoryContext:
        _ = (args, kwargs)
        return MemoryContext(
            episodes=[
                Episode(
                    id="e1",
                    content="I met my aunt and received a crystal chandelier.",
                    valid_at="2023-04-01 08:00:00",
                    scope_key="user_global:eval_71017276",
                    embedding=[],
                )
            ]
        )

    with patch("gsuid_core.ai_core.memory.retrieval.dual_route.dual_route_retrieve", new=_fake):
        ids, hits = _run(
            _search_memory(
                "eval_71017276 aunt crystal chandelier",
                kinds=SPEAKER_RECALL_KINDS,
                scope=CogScope(user_id="eval_71017276"),
                limit=8,
            )
        )
    assert ids
    assert any("crystal chandelier" in hits[i].summary for i in ids)


def test_search_memory_includes_episodes_with_rank_scores() -> None:
    """显式查片段才带回正文；评测与生产同一 top_k、同一片段分。"""
    from gsuid_core.ai_core.cognition.facade import _search_memory
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    captured: dict[str, int] = {}

    async def _fake(*args: object, **kwargs: object) -> MemoryContext:
        top_k = kwargs["top_k"]
        assert isinstance(top_k, int)
        captured["top_k"] = top_k
        return MemoryContext(
            episodes=[
                Episode(
                    id="e1",
                    content="I prefer Adobe Premiere Pro video editing tutorials for advanced color grading.",
                    valid_at="2023-05-30 12:00:00",
                    scope_key="user_global:u1",
                    embedding=[],
                )
            ]
        )

    with patch("gsuid_core.ai_core.memory.retrieval.dual_route.dual_route_retrieve", new=_fake):
        ids, hits = _run(
            _search_memory(
                "u1 video editing",
                kinds=frozenset({CogKind.EPISODE}),
                scope=CogScope(user_id="u1"),
                limit=8,
            )
        )
    assert ids
    ep = hits[ids[0]]
    assert ep.kind is CogKind.EPISODE
    assert "Premiere Pro" in ep.summary
    assert ep.score == 0.8
    prod_k = captured["top_k"]

    with patch("gsuid_core.ai_core.memory.retrieval.dual_route.dual_route_retrieve", new=_fake):
        eval_ids, eval_hits = _run(
            _search_memory(
                "video editing",
                kinds=frozenset({CogKind.EPISODE}),
                scope=CogScope(user_id="eval_u1", memory_eval=True),
                limit=8,
            )
        )
    assert eval_hits[eval_ids[0]].score == 0.8
    assert captured["top_k"] == prod_k


def test_one_backend_failure_only_drops_that_leg() -> None:
    """单路失败 fail-open，不影响其余（否则一个后端抖动就整轮没有回想）。"""
    from gsuid_core.ai_core.cognition import search_cognition

    good = CognitiveHit(kind=CogKind.KNOWLEDGE, id="kb", title="doc", summary="", score=0.9)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("backend down")

    async def _ok(query: str, *, scope: Any, limit: int) -> Any:
        return ["kb"], {"kb": good}

    async def _empty(*args: Any, **kwargs: Any) -> Any:
        return [], {}

    with (
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=_boom),
        patch("gsuid_core.ai_core.cognition.facade._search_knowledge_backend", new=_ok),
        patch("gsuid_core.ai_core.cognition.facade._search_fileos", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_records", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_images", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_memes", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=_empty),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=_empty),
    ):
        hits = _run(search_cognition("q", kinds=ALL_KINDS, scope=CogScope(user_id="u1"), limit=10))
    assert [h.id for h in hits] == ["kb"]


def test_blank_query_or_empty_kinds_short_circuits() -> None:
    from gsuid_core.ai_core.cognition import search_cognition

    assert _run(search_cognition("   ", kinds=ALL_KINDS, scope=CogScope(user_id="u"))) == []
    assert _run(search_cognition("q", kinds=frozenset(), scope=CogScope(user_id="u"))) == []


def test_fileos_backend_is_fail_closed_without_owner() -> None:
    """无 owner 不许全局扫表（跨用户泄漏防线）。"""
    from gsuid_core.ai_core.cognition.facade import _search_fileos

    ids, hits = _run(_search_fileos("q", scope=CogScope(user_id=""), limit=5))
    assert ids == [] and hits == {}


def test_fileos_hit_title_prefers_search_query() -> None:
    from gsuid_core.ai_core.cognition.facade import _fileos_hit_title

    assert _fileos_hit_title("query: AcmeCorp [1] 招股", "web_search_tool") == "AcmeCorp"
    assert _fileos_hit_title("[1] 招股说明书", "web_search_tool") == "web_search_tool"


def test_search_nodes_includes_self_scope_when_bot_id_present() -> None:
    """self_note 写在 self:{bot_id}，检索面必须带上，否则写入后永远召不回。"""
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.cognition.facade import _search_nodes

    captured: dict[str, Any] = {}

    async def _fake_search(
        keyword: str,
        *,
        scope_keys: list[str],
        owner_user_id: str,
        kinds: Any = None,
        limit: int = 12,
    ) -> list[Any]:
        captured["scope_keys"] = list(scope_keys)
        return []

    with patch("gsuid_core.ai_core.cognition.nodes.AICogNode.search", new=_fake_search):
        with _empty_group_profile_patch():
            _run(
                _search_nodes(
                    "我记过什么",
                    kinds=frozenset({CogKind.SELF_NOTE}),
                    scope=CogScope(user_id="u1", bot_id="onebot", bot_self_id="botA", group_id="g1"),
                    limit=8,
                )
            )

    keys = captured["scope_keys"]
    assert make_scope_key(ScopeType.SELF, "botA") in keys
    assert make_scope_key(ScopeType.SELF, "onebot") not in keys
    assert make_scope_key(ScopeType.GROUP, "g1") in keys
    assert make_scope_key(ScopeType.USER_GLOBAL, "u1") in keys


def test_search_nodes_omits_self_scope_without_bot_id() -> None:
    """bot_self_id 空时不猜 SELF key——乱拼会把别的 bot 的笔记扫进来。"""
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.cognition.facade import _search_nodes

    captured: list[str] = []

    async def _fake_search(
        keyword: str,
        *,
        scope_keys: list[str],
        owner_user_id: str,
        kinds: Any = None,
        limit: int = 12,
    ) -> list[Any]:
        captured.extend(scope_keys)
        return []

    with patch("gsuid_core.ai_core.cognition.nodes.AICogNode.search", new=_fake_search):
        with _empty_group_profile_patch():
            _run(
                _search_nodes(
                    "q",
                    kinds=ALL_KINDS,
                    scope=CogScope(user_id="u1"),
                    limit=5,
                )
            )

    assert all(not key.startswith("self:") for key in captured)
    assert make_scope_key(ScopeType.USER_GLOBAL, "u1") in captured


# ── 节点层：索引，不是第二份正文 ──


def test_node_table_stores_no_body() -> None:
    """节点只存身份 / kind / ref / 摘要 / scope / 时间 / decay。"""
    from gsuid_core.ai_core.cognition.nodes import AICogNode

    fields = set(AICogNode.model_fields)
    assert {"kind", "ref", "scope_key", "title", "summary", "as_of", "handle", "decay", "canon"} <= fields
    assert "domain" not in fields
    for forbidden in ("content", "body", "payload", "payload_inline", "text"):
        assert forbidden not in fields, f"节点表不许存正文：{forbidden}"


def test_attachment_table_stores_no_body() -> None:
    from gsuid_core.ai_core.cognition.nodes import AICogAttachment

    fields = set(AICogAttachment.model_fields)
    assert {"node_id", "slot", "title", "summary", "as_of", "source", "writable", "ref", "handle"} <= fields
    for forbidden in ("content", "body", "payload", "payload_inline", "text"):
        assert forbidden not in fields, f"挂件表不许存正文：{forbidden}"


def _unique_constraint_columns(table_args: Any) -> set:
    """从 ``__table_args__`` 里取出全部唯一约束的列名元组。

    ``__table_args__`` 是「约束对象 + 末尾一个 dict」的混合元组，用 isinstance
    精确挑出约束（``hasattr`` 不足以让类型检查器收敛）。
    """
    from sqlalchemy import UniqueConstraint

    return {tuple(col.name for col in arg.columns) for arg in table_args if isinstance(arg, UniqueConstraint)}


def test_node_identity_is_kind_plus_ref() -> None:
    from gsuid_core.ai_core.cognition.nodes import AICogNode

    names = _unique_constraint_columns(AICogNode.__table_args__)
    assert ("kind", "ref") in names, names


def test_attachment_identity_is_node_plus_ref() -> None:
    from gsuid_core.ai_core.cognition.nodes import AICogAttachment

    names = _unique_constraint_columns(AICogAttachment.__table_args__)
    assert ("node_id", "ref") in names, names


def test_edge_table_rejects_self_loops_and_duplicates() -> None:
    from gsuid_core.ai_core.cognition.nodes import AICogEdge

    names = _unique_constraint_columns(AICogEdge.__table_args__)
    assert ("src_id", "dst_id", "edge_kind") in names


# ── 蒸馏门：纯规则，宁窄勿宽 ──


def test_distill_gate_wants_facts_not_narrative() -> None:
    from gsuid_core.ai_core.cognition.distill import is_worth_distilling

    assert is_worth_distilling("本月指标 12.4%，最大回撤 3.1%，结论是下调上限")
    assert is_worth_distilling("约定：以后周报在每周五下午发")
    assert not is_worth_distilling("好的")
    assert not is_worth_distilling("今天心情不错，随便聊了聊，没什么特别的事情发生呢")


def test_chitchat_gate_still_skips_retrieval() -> None:
    """闲聊仍 0 检索（D-11 精神），主人 / 回指 / 情绪 / 实体强制检索。"""
    from gsuid_core.ai_core.kits.memory.kit import should_retrieve

    assert not should_retrieve("哈哈哈", "闲聊", "u1")
    assert not should_retrieve("嗯嗯好", "闲聊", "u1")
    assert should_retrieve("你之前说过的那个事", "闲聊", "u1")
    assert should_retrieve("我今天好难过", "闲聊", "u1")
    assert should_retrieve("把那份资料查一下", "工具", "u1")
    assert should_retrieve("那个六字以上的专有名怎么处理", "闲聊", "u1")


def test_knowledge_query_appends_group_mapping_formal() -> None:
    from gsuid_core.ai_core.cognition.facade import _knowledge_query_for_scope

    async def _profile(scope_key: str) -> dict[str, object]:
        _ = scope_key
        return {
            "scope_key": scope_key,
            "tag_counts": {},
            "term_mappings": {"EastHill": "AcmeCorp"},
            "member_alias_ids": {},
            "member_aliases": {},
            "last_updated": "",
        }

    with patch("gsuid_core.ai_core.memory.group_profile.get_group_profile", new=_profile):
        expanded = _run(_knowledge_query_for_scope("EastHill 怎么样", CogScope(user_id="u1", group_id="ST")))
        raw = _run(_knowledge_query_for_scope("East 怎么样", CogScope(user_id="u1", group_id="ST")))
    assert expanded.endswith("AcmeCorp")
    assert "AcmeCorp" not in raw


def test_repeat_query_is_short_circuited_within_a_run() -> None:
    """同一 run 内重复 query 不再打后端：认知层只读，重搜必然同结果。

    收成单一动词之后，模型「换个说法再搜一次」的空转成本全压在这一个工具上；
    不挡住就会连打到 thrash 熔断（生产实测同一 query 连打 5 次）。
    """
    from types import SimpleNamespace

    from gsuid_core.ai_core.cognition.hub import ExpandResult
    from gsuid_core.ai_core.buildin_tools.rag_search import search_cognition

    calls: list[str] = []

    async def _counting_search(query: str, *, kinds: Any, scope: Any, limit: int, **_kw: Any) -> Any:
        calls.append(query)
        return []

    deps = SimpleNamespace(
        ev=SimpleNamespace(user_id="u1", group_id="g1", session_id="s1", raw_text=""),
        bot=None,
        extra={},
        parent_session_id=None,
    )
    ctx: Any = SimpleNamespace(deps=deps)
    with (
        patch("gsuid_core.ai_core.buildin_tools.rag_search.federated_search", new=_counting_search),
        patch("gsuid_core.ai_core.register.handle_tool_result", new=AsyncMock(side_effect=lambda bot, raw: raw)),
        patch("gsuid_core.ai_core.cognition.hub.expand_hub", new=AsyncMock(return_value=ExpandResult())),
    ):
        first = _run(search_cognition(ctx, query="上周的旅行计划"))
        # 归一化：空白与大小写差异不算新 query
        second = _run(search_cognition(ctx, query=" 上周的旅行计划 "))
        third = _run(search_cognition(ctx, query="完全不同的问题"))

    assert len(calls) == 2, calls
    assert "无命中" in first
    assert "本轮已检索过" in second
    assert "仍无命中" in second
    assert "含路径卡" not in second
    assert "web_search_tool" in second, "短路回执必须指路到外部检索工具"
    assert "无命中" in third


def test_readonly_retrieval_tools_have_a_stricter_thrash_limit() -> None:
    """find_tools 连打 2 轮即空转；search_cognition 换槽会召回不同片段，阈值更宽。"""
    from gsuid_core.ai_core.agent_run.support import (
        _THRASH_SAME_TOOL_LIMIT,
        _SEARCH_COGNITION_THRASH_LIMIT,
        thrash_limit_for,
    )

    assert thrash_limit_for("find_tools") == 2
    assert thrash_limit_for("search_cognition") == _SEARCH_COGNITION_THRASH_LIMIT
    assert thrash_limit_for("web_search_tool") == _THRASH_SAME_TOOL_LIMIT
    assert thrash_limit_for("create_subagent") == _THRASH_SAME_TOOL_LIMIT


def test_cognition_tool_docstring_steers_away_from_realtime_data() -> None:
    """工具说明必须把「不查实时/外网」放在最前面并点名替代工具。

    收成单一「回想」动词后，模型会把它当通用搜索用（实测抢掉了 web_search_tool），
    所以边界必须写在描述开头、且指名道姓。
    """
    from gsuid_core.ai_core.buildin_tools.rag_search import search_cognition

    doc = search_cognition.__doc__ or ""
    assert "不查实时" in doc
    assert "web_search_tool" in doc
    assert "find_tools" in doc
    assert "专名/数字/约束" in doc
    head = doc[: doc.find("Args:")] if "Args:" in doc else doc
    assert head.index("不查实时") < head.index("什么时候用"), "边界必须先于用法"
    assert "说话人ID + 要填的槽" in doc
    assert "外部题目" in doc


def test_web_search_docstring_defers_to_speaker_recall() -> None:
    from gsuid_core.ai_core.buildin_tools.web_search import web_search_tool

    doc = web_search_tool.__doc__ or ""
    assert "search_cognition" in doc
    assert "空槽" in doc
