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
    KIND_LABEL,
    WORK_KINDS,
    MEMORY_KINDS,
    KNOWLEDGE_KINDS,
    CogKind,
    CogScope,
    CognitiveHit,
    kinds_from_names,
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


def test_kind_taxonomy_is_complete_and_labelled() -> None:
    """六类语义互不覆盖，且每类都有面向模型的中文标签。"""
    assert set(KIND_LABEL) == set(CogKind)
    assert MEMORY_KINDS < ALL_KINDS
    assert KNOWLEDGE_KINDS == {CogKind.KNOWLEDGE}
    assert WORK_KINDS == {CogKind.TOOL_OUTPUT, CogKind.ARTIFACT}
    # ⑧ 每轮默认切片不含知识/落盘（延迟不回退）
    assert CogKind.KNOWLEDGE not in MEMORY_KINDS
    assert CogKind.TOOL_OUTPUT not in MEMORY_KINDS
    # 偏好在默认切片里：它是「须遵守」的硬约束
    assert CogKind.PREFERENCE in MEMORY_KINDS


def test_scope_and_kinds_have_no_internal_default() -> None:
    """两个真实 bug 的共同根因是「可选参数被内部兜底成看起来合理的值」。"""
    from gsuid_core.ai_core.cognition import search_cognition

    sig = inspect.signature(search_cognition)
    for name in ("kinds", "scope"):
        param = sig.parameters[name]
        assert param.default is inspect.Parameter.empty, f"{name} 不许有默认值"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_dual_route_enable_system2_is_required() -> None:
    """``enable_system2`` 必填：函数默认值曾是 True 而生产配置默认关，工具路径偷跑。"""
    from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

    param = inspect.signature(dual_route_retrieve).parameters["enable_system2"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    # group_id 也必须是关键字参数，避免位置传参把 user_id 错位成 group
    assert inspect.signature(dual_route_retrieve).parameters["group_id"].kind is inspect.Parameter.KEYWORD_ONLY


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


def test_weak_hits_are_folded_not_labelled_high_confidence() -> None:
    """弱相关折成一句，不贴高置信标签（曾把群友赌博片段标成高置信）。"""
    hits = [
        CognitiveHit(kind=CogKind.FACT, id="a", title="强相关", summary="", score=1.0, high_confidence=True),
        CognitiveHit(kind=CogKind.EPISODE, id="b", title="弱相关", summary="", score=0.1, high_confidence=False),
    ]
    block = render_cognition_block("q", hits)
    assert "强相关" in block
    assert "[片段]" not in block, "弱相关条目不该被逐条渲染"
    assert "另有 1 条弱相关" in block


def test_kinds_from_names_ignores_unknown() -> None:
    assert kinds_from_names({"knowledge", "fact"}) == frozenset({CogKind.KNOWLEDGE, CogKind.FACT})
    assert kinds_from_names({"nonsense"}) == frozenset()
    assert kinds_from_names({" Knowledge "}) == frozenset({CogKind.KNOWLEDGE})


def test_relative_score_floor_marks_high_confidence() -> None:
    """相对分下限：只有过门槛的条目才允许标高置信。"""
    from gsuid_core.ai_core.cognition import search_cognition

    strong = CognitiveHit(kind=CogKind.FACT, id="s", title="strong", summary="", score=1.0)
    weak = CognitiveHit(kind=CogKind.FACT, id="w", title="weak", summary="", score=0.05)

    async def _fake_memory(query: str, *, kinds: Any, scope: Any, limit: int) -> Any:
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
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=_empty),
    ):
        hits = _run(search_cognition("q", kinds=MEMORY_KINDS, scope=CogScope(user_id="u1"), limit=10))

    by_id = {h.id: h for h in hits}
    assert by_id["s"].high_confidence
    assert not by_id["w"].high_confidence


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
                    scope=CogScope(user_id="u1", bot_id="botA", group_id="g1"),
                    limit=8,
                )
            )

    keys = captured["scope_keys"]
    assert make_scope_key(ScopeType.SELF, "botA") in keys
    assert make_scope_key(ScopeType.GROUP, "g1") in keys
    assert make_scope_key(ScopeType.USER_GLOBAL, "u1") in keys


def test_search_nodes_omits_self_scope_without_bot_id() -> None:
    """bot_id 空时不猜 SELF key——乱拼会把别的 bot 的笔记扫进来。"""
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


def test_self_note_distill_and_search_share_self_scope() -> None:
    """写入侧和检索侧必须用同一把 SELF key，防止再漂移。"""
    from gsuid_core.ai_core.cognition.facade import _search_nodes
    from gsuid_core.ai_core.cognition.distill import distill_self_note

    assert "ScopeType.SELF" in inspect.getsource(distill_self_note)
    assert "ScopeType.SELF" in inspect.getsource(_search_nodes)


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


def test_edge_kinds_are_a_minimal_set() -> None:
    from gsuid_core.ai_core.cognition.nodes import CogEdgeKind

    assert {e.value for e in CogEdgeKind} == {"related", "supports", "supersedes", "derived_from"}


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


def test_distilled_facts_are_marked_self_action() -> None:
    """C6：允许回流工具/任务的**结构化结论**，但必须标明来源是「我做过的事」。"""
    src = inspect.getsource(__import__("gsuid_core.ai_core.cognition.distill", fromlist=["x"]))
    assert 'source="self_action"' in src
    # 助手台词不进群事实图
    assert "台词" in src


def test_prefetch_is_gated_and_off_by_default() -> None:
    """D-11 边界：有门（非每轮）、只在问答/工具意图、注入目录卡而非全文，且默认关。"""
    from gsuid_core.ai_core.kits.memory import kit as memory_kit
    from gsuid_core.ai_core.configs.ai_config import ai_config

    assert ai_config.get_config("cognition_prefetch_enable").data is False, "预取必须默认关，灰度后再翻"
    src = inspect.getsource(memory_kit)
    assert "cognition_prefetch_enable" in src
    assert '("问答", "工具")' in src, "预取必须有意图门"
    assert "目录卡" in src or "已检索·目录" in src


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


def test_memory_slice_keeps_the_five_budget_slots() -> None:
    """⑧ 注入必须保留 to_prompt_text 的五个配额位，否则偏好会被事实挤掉。"""
    from gsuid_core.ai_core.cognition.facade import inject_memory_slice

    src = inspect.getsource(inject_memory_slice)
    assert "to_prompt_text" in src, "不许改用通用渲染，那会丢掉偏好独立配额"
    assert "priority_speakers" in src
    assert "current_speaker_ids" in src, "第三方隐私门不能丢"
    assert "memory_inject_max_chars" in src
    assert "query=query" in src


def test_repeat_query_is_short_circuited_within_a_run() -> None:
    """同一 run 内重复 query 不再打后端：认知层只读，重搜必然同结果。

    收成单一动词之后，模型「换个说法再搜一次」的空转成本全压在这一个工具上；
    不挡住就会连打到 thrash 熔断（生产实测同一 query 连打 5 次）。
    """
    from types import SimpleNamespace

    from gsuid_core.ai_core.cognition.hub import ExpandResult
    from gsuid_core.ai_core.buildin_tools.rag_search import search_cognition

    calls: list[str] = []

    async def _counting_search(query: str, *, kinds: Any, scope: Any, limit: int) -> Any:
        calls.append(query)
        return []

    deps = SimpleNamespace(
        ev=SimpleNamespace(user_id="u1", group_id="g1", session_id="s1"),
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
    """只读检索工具没有副作用也没有新信息源，连打 2 轮就是空转。"""
    from gsuid_core.ai_core.agent_run.support import _THRASH_SAME_TOOL_LIMIT, thrash_limit_for

    for name in ("find_tools", "search_cognition"):
        assert thrash_limit_for(name) == 2, name
    # 有副作用 / 有外部信息源的工具沿用宽阈值（避免误伤 research 并行 web_search）
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
    head = doc[: doc.find("Args:")] if "Args:" in doc else doc
    assert head.index("不查实时") < head.index("什么时候用"), "边界必须先于用法"


def test_memory_budget_literal_is_gone() -> None:
    """1200 字面量把 memory_inject_max_chars 架空了，必须已删除。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "gsuid_core" / "ai_core"
    for rel in ("context_assembly.py", "kits/memory/kit.py", "cognition/facade.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "1200" not in src, f"{rel} 仍有 1200 字面量"
        assert "1197" not in src, f"{rel} 仍有 1197 字面量"
