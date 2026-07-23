"""2026-07-17 代码评审修复回归（对应 docs/AI_CORE_FIXES_HANDOVER_20260717.md 落地后的评审）。

评审结论：多个修复只在单元层验证、端到端链路失效（编号 F*/E* 为评审发现编号）：
- F2/F4 整行（…）文案被人设净化的舞台旁白规则删除 → 用户兜底与溯源尾注静默丢失；
- F3 report 制品块绕过输出防火墙；F6 干净重试末次 attempt 掉出循环；
- F7 §7 隐私过滤是调用点 opt-in；F9 untrusted 闭合标签被尾截断切掉；
- F10 fund_claim 误杀/穿透双向缺陷；F13 残句正则误杀动名兼类词；
- F15 参数规范化对正常紧凑 JSON 恒改写刷告警；E5/E4 report 占位符谎报与外科顺序。
本文件把修复后的行为冻结住。
"""

import time
import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import TextPart, ToolCallPart, ModelResponse
from pydantic_ai.exceptions import ModelHTTPError

from gsuid_core.ai_core.utils import (
    NO_RESULT_TEXT,
    ERROR_RESULT_PREFIX,
    _extract_report_blocks,
    _strip_persona_markdown,
    sanitize_error_for_user,
    has_model_visible_content,
    _compact_report_blocks_in_history,
    _canonicalize_tool_call_args_in_parts,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ─────────────────────────────────────────────
# F2/F4：系统文案必须在人设净化后存活（整行（…）会被舞台旁白规则整行删除）
# ─────────────────────────────────────────────


def test_sanitized_error_texts_survive_persona_cleanup() -> None:
    samples = [
        f"{ERROR_RESULT_PREFIX}: 内容被模型安全策略拒绝",
        f"{ERROR_RESULT_PREFIX}: 请求超时",
        f"{ERROR_RESULT_PREFIX}: status_code: 400, body: {{'x': 1}}",
        NO_RESULT_TEXT,
    ]
    for raw in samples:
        cleaned = _strip_persona_markdown(sanitize_error_for_user(raw)).strip()
        assert cleaned, raw
        assert "status_code" not in cleaned


def test_scheduled_task_tail_note_survives_persona_cleanup() -> None:
    msg = "BTC 跌破 60000，快跑\n⏰ 定时任务 sched_ab12"
    cleaned = _strip_persona_markdown(msg)
    assert "sched_ab12" in cleaned


def test_executor_failure_branch_is_sanitized() -> None:
    import inspect

    import gsuid_core.ai_core.scheduled_task.executor as ex

    src = inspect.getsource(ex.execute_scheduled_task)
    assert "ERROR_RESULT_PREFIX" in src  # F5：失败结果不得原样播报（provider body 泄漏）
    assert "any(m in result_stripped for m in SILENCE_MARKERS)" in src  # E1：SILENCE 含附言也拦


# ─────────────────────────────────────────────
# F3：report 制品块与台词同权过末端防火墙
# ─────────────────────────────────────────────


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, message, extra_metadata=None) -> None:
        self.sent.append(message)


@pytest.mark.anyio
async def test_report_block_hits_firewall_before_render(monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import Any

    import gsuid_core.utils.html_render as html_render
    import gsuid_core.ai_core.output_firewall as fw
    from gsuid_core.ai_core.utils import send_chat_result

    rendered: list = []

    async def fake_render(md: str, max_width: int = 0, image_format: str = "jpeg") -> bytes:
        rendered.append(md)
        return b"img"

    monkeypatch.setattr(html_render, "render_md_to_bytes", fake_render)
    monkeypatch.setattr(fw, "is_enabled", lambda: True)

    bot: Any = _FakeBot()
    text = '看图～\n\n<report title="转账说明">钱已经转过去了，请查收</report>'
    await send_chat_result(bot, text, ev=None)

    # fund_claim 命中的块整块拦截：不渲染、不发送；台词照常
    assert all("转过去" not in md for md in rendered)
    assert bot.sent, "台词部分应正常发送"


@pytest.mark.anyio
async def test_clean_report_block_still_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import Any

    import gsuid_core.utils.html_render as html_render
    from gsuid_core.ai_core.utils import send_chat_result

    rendered: list = []

    async def fake_render(md: str, max_width: int = 0, image_format: str = "jpeg") -> bytes:
        rendered.append(md)
        return b"img"

    monkeypatch.setattr(html_render, "render_md_to_bytes", fake_render)

    bot: Any = _FakeBot()
    text = '帮你整理好了～\n\n<report title="方案对比">| 维度 | A | B |</report>'
    await send_chat_result(bot, text, ev=None)

    assert any("方案对比" in md for md in rendered)
    # §3 合规垫层：制品图片带免责脚注
    assert any("不构成投资" in md for md in rendered)


# ─────────────────────────────────────────────
# E2：report 正则的引号漂移与孤儿标签
# ─────────────────────────────────────────────


def test_report_block_single_quote_title_extracted() -> None:
    remaining, blocks = _extract_report_blocks("看图～<report title='方案对比'>|A|B|</report>")
    assert blocks == [("方案对比", "|A|B|")]
    assert remaining == "看图～"


def test_unclosed_report_tag_not_shown_to_user() -> None:
    remaining, blocks = _extract_report_blocks('结论如下<report title="截断">内容还没闭合')
    assert blocks == []
    assert "<report" not in remaining
    assert "内容还没闭合" in remaining  # 内容保留走长 markdown 兜底，只剥字面标签


# ─────────────────────────────────────────────
# E5：历史占位符只写"确实发出去过"的 part
# ─────────────────────────────────────────────


def test_compact_report_blocks_respects_sent_gate() -> None:
    # 全量抹数据块（防教坏）；sent_reports 仅对真正发出的 part 写入 metadata
    sent_text = '预热一下<report title="A">正文A</report>'
    unsent_text = '被拦下的<report title="B">正文B</report>'
    msgs: list = [
        ModelResponse(parts=[TextPart(content=sent_text)]),
        ModelResponse(parts=[TextPart(content=unsent_text)]),
    ]
    replaced = _compact_report_blocks_in_history(msgs, sent_texts={sent_text})
    assert replaced == 2
    first = msgs[0].parts[0]
    second = msgs[1].parts[0]
    assert isinstance(first, TextPart)
    assert "正文A" not in first.content and "预热一下" in first.content
    assert msgs[0].metadata is not None and "sent_reports" in msgs[0].metadata
    assert "A" in msgs[0].metadata["sent_reports"]
    assert isinstance(second, TextPart)
    assert "正文B" not in second.content  # 未发送也抹结构，防教坏
    assert not msgs[1].metadata or "sent_reports" not in (msgs[1].metadata or {})


def test_compact_runs_after_history_surgery() -> None:
    """E4：占位压缩必须在出戏外科（按原文精确匹配）之后执行。"""
    import inspect

    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

    src = inspect.getsource(GsCoreAIAgent._execute_run_once)
    assert src.index("_ooc_rewrite_and_send") < src.index("_compact_report_blocks_in_history")


# ─────────────────────────────────────────────
# F15：参数规范化只在真实重复键时出手
# ─────────────────────────────────────────────


def test_canonicalize_keeps_normal_compact_json_bytes() -> None:
    part = ToolCallPart(tool_name="t", args='{"city":"北京"}', tool_call_id="c1")
    _canonicalize_tool_call_args_in_parts([part])
    assert part.args == '{"city":"北京"}'  # 原字节不动：历史与模型原始输出一致、不刷告警


def test_canonicalize_rewrites_on_duplicate_keys_only() -> None:
    part = ToolCallPart(tool_name="t", args='{"a": 1, "a": 2}', tool_call_id="c2")
    _canonicalize_tool_call_args_in_parts([part])
    assert isinstance(part.args, str)
    assert part.args.count('"a"') == 1


def test_canonicalize_detects_nested_duplicate_keys() -> None:
    part = ToolCallPart(tool_name="t", args='{"outer": {"k": 1, "k": 2}}', tool_call_id="c3")
    _canonicalize_tool_call_args_in_parts([part])
    assert isinstance(part.args, str)
    assert part.args.count('"k"') == 1


# ─────────────────────────────────────────────
# F6：干净重试在末次 attempt 上仍会真正重跑，且所有出口闭合 run
# ─────────────────────────────────────────────


class _FakeSessionLogger:
    def __init__(self) -> None:
        self.run_end = 0
        self.results: list = []
        self.errors: list = []

    def log_run_end(self) -> None:
        self.run_end += 1

    def log_result(self, text, tools) -> None:
        self.results.append(text)

    def log_error(self, kind: str, msg: str) -> None:
        self.errors.append(kind)


@pytest.mark.anyio
async def test_clean_retry_on_last_attempt_reruns_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.ai_core.gs_agent as ga

    original_get = ga.ai_config.get_config

    def fake_get(key: str):
        if key == "agent_max_run_attempts":
            return SimpleNamespace(data=1)
        if key == "agent_run_retry_delay":
            return SimpleNamespace(data=0.0)
        return original_get(key)

    monkeypatch.setattr(ga.ai_config, "get_config", fake_get)

    from typing import Any

    agent: Any = object.__new__(ga.GsCoreAIAgent)
    agent._run_sent_texts = set()
    agent._last_attempt_tool_calls = ["send_message_by_ai"]
    fake_logger = _FakeSessionLogger()
    agent._session_logger = fake_logger

    calls = {"n": 0}
    err = ModelHTTPError(status_code=400, model_name="m", body={"message": "invalid function arguments"})

    async def fake_once(**kwargs: Any) -> str:
        calls["n"] += 1
        raise err

    agent._execute_run_once = fake_once

    result = await agent._execute_run(user_message="hi")

    # max_attempts=1 也必须真的重跑一次（旧实现 continue 直接掉出循环、一次都不重试）
    assert calls["n"] == 2
    # 双次失败后必须闭合 run（log_run_end + log_result），不再留悬空 run_start
    assert fake_logger.run_end == 1
    assert result.startswith(ERROR_RESULT_PREFIX)


# ─────────────────────────────────────────────
# F13：残句正则不误杀动名兼类词的名词用法
# ─────────────────────────────────────────────


def test_dangling_fact_regex_spares_noun_usage() -> None:
    from gsuid_core.ai_core.memory.ingestion.edge import _DANGLING_FACT_RE

    positives = ["用户994534742提到", "[小C]提及", "用户A讨论了", "用户B回复了。", "居木说"]
    negatives = ["用户A积极参与了昨晚的方案讨论", "用户B一直在等主人的回复", "用户C给出了很高的评价"]
    for p in positives:
        assert _DANGLING_FACT_RE.search(p), p
    for n in negatives:
        assert not _DANGLING_FACT_RE.search(n), n


# ─────────────────────────────────────────────
# E6/F7/F9：记忆注入——说话人边界匹配、默认拒绝、栅栏闭合
# ─────────────────────────────────────────────


def _edge_dict(source_name: str, fact: str):
    from gsuid_core.ai_core.memory.retrieval.types import Edge

    edge: Edge = {
        "id": "e1",
        "source_id": "s",
        "target_id": "t",
        "source_name": source_name,
        "target_name": "t",
        "fact": fact,
        "weight": 1.0,
        "score": 1.0,
        "valid_at_ts": None,
        "invalid_at_ts": None,
    }
    return edge


def test_fact_mentions_speaker_uses_digit_boundary() -> None:
    from gsuid_core.ai_core.memory.retrieval.dual_route import _fact_mentions_speaker

    edge = _edge_dict("用户9123456780", "用户9123456780 最近在办离婚")
    assert _fact_mentions_speaker(edge, {"12345"}) is False  # 短号是长号子串：不得误判在场
    assert _fact_mentions_speaker(edge, {"9123456780"}) is True


def test_untrusted_fence_closed_under_budget_pressure() -> None:
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

    episodes = [
        Episode(
            id=f"ep{i}",
            content="长" * 400,
            valid_at="2026-07-16T12:00:00",
            scope_key="g1",
            embedding=[],
        )
        for i in range(8)
    ]
    text = MemoryContext(episodes=episodes).to_prompt_text(max_chars=600)
    assert "<untrusted" in text
    assert text.rstrip().endswith("</untrusted>")  # 闭合标签永不被尾截断切掉


# ─────────────────────────────────────────────
# F10：资金红线——误杀负例与不可放行
# ─────────────────────────────────────────────


def test_fund_claim_precision_negatives() -> None:
    from gsuid_core.ai_core.output_firewall import _fund_claim_hit

    negatives = [
        ("新版本v2发了，快去更新", ""),
        ("用v2ray的话记得更新订阅", ""),
        ("收到啦，文件已经转发了", "刚才的文件收到了吗"),
        ("游戏打了三小时才通关", ""),
    ]
    for text, user_text in negatives:
        assert _fund_claim_hit(text, user_text) is None, (text, user_text)


def test_fund_claim_positives_still_hit() -> None:
    from gsuid_core.ai_core.output_firewall import _fund_claim_hit

    positives = [
        ("钱已经转过去了", ""),
        ("唔…早柚明明发过去了…", "没收到啊"),
        ("红包发了哦", ""),
    ]
    for text, user_text in positives:
        assert _fund_claim_hit(text, user_text) is not None, (text, user_text)


def test_fund_claim_never_released_by_warn_once_gate() -> None:
    from gsuid_core.ai_core.output_firewall import NEVER_RELEASE_CATEGORIES, gate_warn_once

    assert "fund_claim" in NEVER_RELEASE_CATEGORIES
    extra: dict = {"turn_id": "turn_review_f10"}
    text = "钱已经转过去了"
    assert gate_warn_once(extra, text) is not None
    # 同轮第二次命中：身份类会放行，资金欺骗类必须继续拦
    assert gate_warn_once(extra, text) is not None


def test_fund_rewrite_warning_is_category_specific() -> None:
    from gsuid_core.ai_core.output_firewall import FirewallHit, build_rewrite_warning

    warning = build_rewrite_warning(FirewallHit(category="fund_claim", matched=["声称已完成转账/付款"]))
    assert "不得声称已转账" in warning


# ─────────────────────────────────────────────
# E10：空消息门判据与 payload 构建同源
# ─────────────────────────────────────────────


def test_has_model_visible_content_covers_all_modalities() -> None:
    from gsuid_core.models import Event

    def _ev(**overrides) -> Event:
        fields: dict = {"bot_id": "onebot", "bot_self_id": "1", "msg_id": "m", "user_type": "group"}
        fields.update(overrides)
        return Event(**fields)

    assert has_model_visible_content(_ev()) is False
    assert has_model_visible_content(_ev(text="在吗")) is True
    assert has_model_visible_content(_ev(image_id_list=["img_1"])) is True
    assert has_model_visible_content(_ev(audio_id="aud_1")) is True
    # 评审发现：旧门只查 audio_id，仅填 audio_id_list 的纯语音消息会被静默丢弃
    assert has_model_visible_content(_ev(audio_id_list=["aud_2"])) is True
    assert has_model_visible_content(_ev(file="base64data")) is True


# ─────────────────────────────────────────────
# F12：私聊任务在群聊上下文一律脱敏
# ─────────────────────────────────────────────


class _FakeKanbanTask:
    def __init__(self, ordinal: int, name: str, group_id) -> None:
        self.id = f"id_{ordinal}"
        self.ordinal = ordinal
        self.display_name = name
        self.group_id = group_id
        self.status = "running"
        self.updated_at = None
        self.recurring_trigger = None
        self.goal = name
        self.agent_profile = None


@pytest.mark.anyio
async def test_private_task_masked_in_group_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.ai_core.planning.context as ctx_mod

    async def fake_list_for_owner(user_id, only_active=True, root_only=True):
        return [_FakeKanbanTask(1, "调研跳槽公司名单", None)]

    async def fake_get_task_tree(task_id):
        return None, []

    monkeypatch.setattr(ctx_mod.AIAgentTask, "list_for_owner", fake_list_for_owner)
    monkeypatch.setattr(ctx_mod.kanban_manager, "get_task_tree", fake_get_task_tree)

    in_group = await ctx_mod.build_task_context("u1", current_group_id="914411529")
    assert "跳槽" not in in_group
    assert "其他会话" in in_group

    in_private = await ctx_mod.build_task_context("u1", current_group_id=None)
    assert "跳槽" in in_private


@pytest.mark.anyio
async def test_has_actionable_task_scoped_by_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """E15：他群/私聊任务不得在本群触发 kanban 工具族挂载。"""
    import gsuid_core.ai_core.planning.context as ctx_mod

    async def fake_list_for_owner(user_id, only_active=True, root_only=True):
        return [_FakeKanbanTask(1, "任务", "group_A")]

    monkeypatch.setattr(ctx_mod.AIAgentTask, "list_for_owner", fake_list_for_owner)

    assert await ctx_mod.has_actionable_task("u1") is True
    assert await ctx_mod.has_actionable_task("u1", current_group_id="group_A") is True
    assert await ctx_mod.has_actionable_task("u1", current_group_id="group_B") is False


# ─────────────────────────────────────────────
# F1/F14：好感度信号与 run 级发送去重
# ─────────────────────────────────────────────


def test_last_run_sent_visible_reply_property() -> None:
    import gsuid_core.ai_core.gs_agent as ga

    agent = object.__new__(ga.GsCoreAIAgent)
    agent._run_sent_texts = set()
    assert agent.last_run_sent_visible_reply is False
    agent._run_sent_texts.add("说过话了")
    assert agent.last_run_sent_visible_reply is True


def test_message_sender_dedups_via_run_registry() -> None:
    import inspect

    import gsuid_core.ai_core.buildin_tools.message_sender as ms

    src = inspect.getsource(ms)
    assert "run_sent_texts" in src  # 工具内发送与主循环共用 run 级去重集合


def test_tool_context_extra_carries_run_registry() -> None:
    import inspect

    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

    src = inspect.getsource(GsCoreAIAgent._execute_run_once)
    assert '"run_sent_texts": self._run_sent_texts' in src


# ─────────────────────────────────────────────
# E9 补充：staleness 阈值语义（bot 也算锚已在 behavior 测试冻结，这里冻结阈值来源）
# ─────────────────────────────────────────────


def test_staleness_threshold_is_module_constant() -> None:
    import inspect

    from gsuid_core.ai_core.heartbeat import decision

    sig = inspect.signature(decision.build_staleness_section)
    assert list(sig.parameters) == ["history", "now_ts"]  # E18：去掉从未被用的参数化
    assert decision.STALE_TOPIC_MINUTES_DEFAULT == 15


# 保证本文件的 asyncio 标记在无 anyio 插件差异下也可跑（与仓库其他测试一致用 anyio）
def test_sanity_asyncio_available() -> None:
    assert asyncio.get_event_loop_policy() is not None
    assert time.time() > 0
