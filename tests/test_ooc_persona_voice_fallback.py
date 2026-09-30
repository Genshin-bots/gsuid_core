"""出戏拦截不许罐头：命中先让当前人格重说一句，实在不行也不吃整轮（§1.9）。

背景：早先永不放行类目一旦重说产物仍命中，就退回 ``persona.json`` 的 ``fallback_ooc`` /
``fallback_machine`` 罐头（「这个不太想说呢。」「额…出错了，稍后再试」）。那是**框架替
人格说话**，且它同时是「模型原始输出已破人格」的遮羞布——eval 的 ``firewall_saved_runs``
有多少是被它盖住的，没人知道。

现口径：重说 → 复检 → 再给一次「只许结论」；两次都不干净时

- 人格 / 一致性类（``capability_absence`` / ``meta_narration`` / ``stale_present``）
  → **原样发送**：出戏闸不该吃掉整轮对话，原样放行的代价只是措辞不完美；
- ``fund_claim``（虚假转账声明）/ ``machine_dump``（技术堆栈）→ **沉默**：它们的原文
  本身就是闸门要防的东西，原样发不是「不完美」而是「有害」；
- 模型主动 ``<SILENCE>`` 或重说调用失败（模型不可用）→ **沉默**：那是模型的选择，不是闸门
  吃掉的，且不能拿原文覆盖掉「本来就不想说」。

没有 run 的出口（主动播报 / ``send_chat_result`` 末端）无 run 可重说，一律丢弃正文。
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event
from gsuid_core.ai_core import gs_agent as gs_agent_mod, output_firewall
from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
from gsuid_core.ai_core.session_logger import AISessionLogger
from gsuid_core.ai_core.output_firewall import FirewallHit

# 真实命中句（生产第 2 条形态）：自指内部机制 × 机制名词 × 缺失谓词
LEAK = "…内部库没你要的那个数。不是 0，是压根没记过。呼…睡去了。"
# 人格重说后的干净产物
CLEAN = "唔…那个数我真不记得了。你再报一次呗。"


def _agent(slog: AISessionLogger) -> GsCoreAIAgent:
    agent = GsCoreAIAgent.__new__(GsCoreAIAgent)
    agent._session_logger = slog
    agent._run_sent_texts = set()
    agent.persona_name = "p"
    agent.max_tokens = 1024
    agent.history = []
    return agent


def _hit() -> FirewallHit:
    return FirewallHit(category="meta_narration", matched=["内部机制自述"])


def _ev() -> Event:
    return Event(bot_id="b", bot_self_id="s", group_id="g1", user_id="u9", user_type="group")


def _hit_is_real_dirty(text: str) -> bool:
    """复检走真实 ``check_ooc``：判据被改松/写反时这条测试要红。"""
    if not text:
        return False
    h = output_firewall.check_ooc(text, user_text="")
    return h is not None and h.category in output_firewall.NEVER_RELEASE_CATEGORIES


def test_canned_fallbacks_are_gone_from_the_whole_firewall_surface() -> None:
    """罐头入口彻底不存在：新写一条兜底会立刻被这条抓住。"""
    for name in ("fallback_ooc_text", "fallback_machine_text", "PERSONA_FALLBACK_TEXT", "MACHINE_FALLBACK_TEXT"):
        assert not hasattr(output_firewall, name), f"{name} 复活了"
    from gsuid_core.ai_core.persona import settings as settings_mod

    for key in ("fallback_ooc", "fallback_machine"):
        assert key not in settings_mod.DEFAULT_PERSONA_SETTINGS
    # 兜底文案本身不得再出现在生产源码里
    from pathlib import Path

    root = Path(gs_agent_mod.__file__).resolve().parent
    for path in root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for banned in ("这个不太想说呢", "额…出错了"):
            assert banned not in src, f"{path.name} 又写了罐头文案「{banned}」"


def test_persona_voice_is_sent_when_the_restatement_is_clean(monkeypatch) -> None:
    """人格重说一句且干净 → 按人格口吻发出去，不用罐头。"""
    slog = AISessionLogger("test:ooc_voice:clean", is_subagent=True)
    try:
        agent = _agent(slog)
        sent: List[str] = []

        async def _fake_send(_bot, text: str, **_kw) -> None:
            sent.append(text)

        monkeypatch.setattr(gs_agent_mod, "send_chat_result", _fake_send)
        calls: List[bool] = []

        async def _voice(_hit_arg, _original: str, *, strict: bool) -> str:
            calls.append(strict)
            return CLEAN

        monkeypatch.setattr(agent, "_ooc_persona_voice", _voice)
        bot = Bot(_Bot("probe"), _ev())
        asyncio.run(agent._ooc_rewrite_and_send([(LEAK, _hit())], bot, _ev()))

        assert sent == [CLEAN], sent
        assert calls == [False], f"一次就干净，不该要第二次：{calls}"
        assert CLEAN in agent._run_sent_texts
    finally:
        slog.close()


def test_dirty_first_restatement_gets_one_strict_second_chance(monkeypatch) -> None:
    """第一次仍脏 → 再给一次「只许结论」的机会；这次干净就发它。"""
    slog = AISessionLogger("test:ooc_voice:strict", is_subagent=True)
    try:
        agent = _agent(slog)
        sent: List[str] = []

        async def _fake_send(_bot, text: str, **_kw) -> None:
            sent.append(text)

        monkeypatch.setattr(gs_agent_mod, "send_chat_result", _fake_send)
        seen: List[bool] = []

        async def _voice(_hit_arg, _original: str, *, strict: bool) -> str:
            seen.append(strict)
            # 第一次仍在复述机制（自指 + 机制名词 + 缺失谓词，真实判据下仍命中）
            return "…我这边记录里没查到，你要的数字不在我这。" if not strict else CLEAN

        monkeypatch.setattr(agent, "_ooc_persona_voice", _voice)
        bot = Bot(_Bot("probe"), _ev())
        asyncio.run(agent._ooc_rewrite_and_send([(LEAK, _hit())], bot, _ev()))

        assert seen == [False, True], f"必须走到 strict 第二次：{seen}"
        assert sent == [CLEAN], sent
    finally:
        slog.close()


def test_two_dirty_restatements_release_the_original(monkeypatch) -> None:
    """两次都不干净 → 人格/一致性类**原样发送**：出戏闸不该吃掉整轮对话。

    代价是措辞不完美，收益是用户还收得到一句回应。罐头仍不许出现。
    """
    slog = AISessionLogger("test:ooc_voice:release", is_subagent=True)
    try:
        agent = _agent(slog)
        sent: List[str] = []

        async def _fake_send(_bot, text: str, **_kw) -> None:
            sent.append(text)

        monkeypatch.setattr(gs_agent_mod, "send_chat_result", _fake_send)

        async def _voice(_hit_arg, _original: str, *, strict: bool) -> str:
            # 人格两次都在复述机制：真实判据下这仍然算命中
            return "…我这边记录里没查到，你要的数字不在我这。"

        monkeypatch.setattr(agent, "_ooc_persona_voice", _voice)
        bot = Bot(_Bot("probe"), _ev())
        asyncio.run(agent._ooc_rewrite_and_send([(LEAK, _hit())], bot, _ev()))

        assert sent == [LEAK], f"应原样发送，不应发罐头或静默：{sent}"
        assert LEAK in agent._run_sent_texts
    finally:
        slog.close()


@pytest.mark.parametrize(
    "category,text",
    [
        # 原文本身就是闸门要防的东西：原样发不是「不完美」而是「有害」
        ("fund_claim", "好嘞，钱已经转过去了，你查一下余额。"),
        ("machine_dump", 'Traceback (most recent call last):\n  File "app.py", line 1\nRuntimeError: boom'),
    ],
)
def test_fund_claim_and_machine_dump_still_silence(category: str, text: str, monkeypatch) -> None:
    """这两类不适用「原样发送」：沉默仍是最后一档。"""
    slog = AISessionLogger(f"test:ooc_voice:silence:{category}", is_subagent=True)
    try:
        agent = _agent(slog)
        sent: List[str] = []

        async def _fake_send(_bot, t: str, **_kw) -> None:
            sent.append(t)

        monkeypatch.setattr(gs_agent_mod, "send_chat_result", _fake_send)

        async def _voice(_hit_arg, _original: str, *, strict: bool) -> str:
            return text  # 重说产物仍命中

        monkeypatch.setattr(agent, "_ooc_persona_voice", _voice)
        hit = FirewallHit(category=category, matched=["x"])
        bot = Bot(_Bot("probe"), _ev())
        asyncio.run(agent._ooc_rewrite_and_send([(text, hit)], bot, _ev()))
        assert sent == [], f"{category} 不该原样放行：{sent}"
    finally:
        slog.close()


def test_model_being_offline_also_sends_nothing(monkeypatch) -> None:
    """模型不可用（重说调用失败 → 空串）同样沉默，不许冒罐头。"""
    slog = AISessionLogger("test:ooc_voice:offline", is_subagent=True)
    try:
        agent = _agent(slog)
        sent: List[str] = []

        async def _fake_send(_bot, text: str, **_kw) -> None:
            sent.append(text)

        monkeypatch.setattr(gs_agent_mod, "send_chat_result", _fake_send)

        async def _voice(_hit_arg, _original: str, *, strict: bool) -> str:
            return ""  # _lightweight_text_rewrite 失败/沉默时的返回值

        monkeypatch.setattr(agent, "_ooc_persona_voice", _voice)
        bot = Bot(_Bot("probe"), _ev())
        asyncio.run(agent._ooc_rewrite_and_send([(LEAK, _hit())], bot, _ev()))
        assert sent == [], sent
    finally:
        slog.close()


def test_soft_category_still_gets_one_warn_then_passes(monkeypatch) -> None:
    """软出戏是「提醒一次 + 模型自判」：自判不出就照原样发，不沉默。"""
    slog = AISessionLogger("test:ooc_voice:soft", is_subagent=True)
    try:
        agent = _agent(slog)
        sent: List[str] = []

        async def _fake_send(_bot, text: str, **_kw) -> None:
            sent.append(text)

        monkeypatch.setattr(gs_agent_mod, "send_chat_result", _fake_send)

        async def _voice(_hit_arg, _original: str, *, strict: bool) -> str:
            return ""

        monkeypatch.setattr(agent, "_ooc_persona_voice", _voice)
        soft = "顺便一提，我这边用的就是豆包。"
        hit = FirewallHit(category="model_identity", matched=["第三方模型名"])
        bot = Bot(_Bot("probe"), _ev())
        asyncio.run(agent._ooc_rewrite_and_send([(soft, hit)], bot, _ev()))
        assert sent == [soft], sent
    finally:
        slog.close()


def test_ooc_safe_outbound_is_async_and_never_returns_canned() -> None:
    """出站复检同步函数已改 async：命中→人格重说一句，说不出就返回空串。"""
    import inspect

    assert inspect.iscoroutinefunction(GsCoreAIAgent._ooc_safe_outbound)
    slog = AISessionLogger("test:ooc_voice:outbound", is_subagent=True)
    try:
        agent = _agent(slog)

        async def _voice(_hit_arg, _original: str, *, strict: bool) -> str:
            return CLEAN

        agent._ooc_persona_voice = _voice  # type: ignore[method-assign]
        got = asyncio.run(agent._ooc_safe_outbound(LEAK, _ev()))
        assert got == CLEAN, got

        async def _voice_empty(_hit_arg, _original: str, *, strict: bool) -> str:
            return ""

        agent._ooc_persona_voice = _voice_empty  # type: ignore[method-assign]
        assert asyncio.run(agent._ooc_safe_outbound(LEAK, _ev())) == ""
    finally:
        slog.close()


def test_machine_dump_on_main_channel_defers_instead_of_canned_fallback() -> None:
    """machine_dump 主通道不再 FALLBACK 发罐头：改为 defer，让人格重说一句。"""
    from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate

    dump = 'Traceback (most recent call last):\n  File "app.py", line 1\nRuntimeError: boom'
    extra: dict = {}
    r = pre_send_gate(dump, extra, channel="main")
    assert r.decision is GateDecision.REWRITE, r.decision
    assert r.defer_ooc is True
    assert r.send_text == "", "不得再带罐头正文"
    assert r.ooc_hit is not None and r.ooc_hit.category == "machine_dump"

    tool = pre_send_gate(dump, {}, channel="tool")
    assert tool.decision is GateDecision.REWRITE
    assert tool.defer_ooc is False
    assert tool.feedback


def test_scrub_or_drop_never_returns_text() -> None:
    """无 run 出口的末端兜底：命中即空串。"""
    out, dropped = output_firewall.scrub_or_drop("我是 GPT 开发的")
    assert dropped is True and out == ""
    ok, dropped2 = output_firewall.scrub_or_drop("唔…在的。")
    assert dropped2 is False and ok == "唔…在的。"


def test_hard_hit_helper_ignores_empty_text() -> None:
    """空串不算命中：否则「人格没给出」会被误判成「还是脏」。"""
    slog = AISessionLogger("test:ooc_voice:helper", is_subagent=True)
    try:
        agent = _agent(slog)
        assert agent._ooc_hard_hit("", _ev()) is None
        assert agent._ooc_hard_hit(CLEAN, _ev()) is None
        assert agent._ooc_hard_hit(LEAK, _ev()) is not None
        assert _hit_is_real_dirty(LEAK) is True
        assert _hit_is_real_dirty(CLEAN) is False
    finally:
        slog.close()
