"""行为与脚手架修复回归测试（plans/prod_session_review §10/§11/§12/§15）。

2026-07-16 生产事故（群 681600567 / 914411529 日志）：
- §10 心跳在群里静默 35 分钟后附和"主人…说得对…"——被回应的两条消息（"好困""睡"）
  都不是主人发的：话题已冷 + 称呼错认双失；
- §11 居木发"你怎么看"+图并 @ 了 B酱（系统已标注"@的是这位用户，不是你"），
  早柚仍完整抢答；
- §12 用户从"早柚火烧"玩笑一路把 AI 逼到：谎称"给你给你"已付款 → 被拆穿后编
  "袖子太深信号不好"圆谎 → 按用户指使"@主人 能不能v50"拉真人出钱；
- §15 纯打趣对话轮被注入"⚠️ 你已连续多轮未调用任何工具，请立即调用"。
"""

import time
from types import SimpleNamespace

from gsuid_core.ai_core.output_firewall import check_ooc, _fund_claim_hit
from gsuid_core.ai_core.heartbeat.decision import (
    DECISION_USER_TEMPLATE,
    PROACTIVE_MESSAGE_USER_TEMPLATE,
    build_staleness_section,
)
from gsuid_core.ai_core.interaction_scaffold import addressed_to_someone_else

# ─────────────────────────────────────────────
# §10 心跳新鲜度门 + 称呼对齐
# ─────────────────────────────────────────────


def _msg(role: str, minutes_ago: float, now: float) -> SimpleNamespace:
    return SimpleNamespace(role=role, timestamp=now - minutes_ago * 60)


def test_stale_topic_triggers_note() -> None:
    """生产复现：最后人类消息 35 分钟前 → 注入"话题已冷"提示。"""
    now = time.time()
    history = [_msg("user", 39, now), _msg("user", 35, now)]
    note = build_staleness_section(history, now)
    assert "35 分钟前" in note
    assert "不要" in note and "接话" in note.replace("'", "")


def test_fresh_topic_no_note() -> None:
    now = time.time()
    history = [_msg("user", 3, now)]
    assert build_staleness_section(history, now) == ""


def test_assistant_message_also_anchors_staleness() -> None:
    """bot 自己的发言也算时间锚（评审修复 E9）：刚发过言不再谎称"最后消息是很久前"；
    整群（含 bot）沉默超阈值才注入冷场提示；空历史无锚不注入。"""
    now = time.time()
    assert build_staleness_section([_msg("assistant", 5, now)], now) == ""
    assert build_staleness_section([_msg("assistant", 40, now)], now) != ""
    assert build_staleness_section([_msg("user", 40, now), _msg("assistant", 5, now)], now) == ""
    assert build_staleness_section([], now) == ""


def test_templates_carry_staleness_and_addressing() -> None:
    """决策与生成两个模板都必须有 staleness 槽位；生成模板必须带称呼对齐指令。"""
    assert "{staleness_section}" in DECISION_USER_TEMPLATE
    assert "{staleness_section}" in PROACTIVE_MESSAGE_USER_TEMPLATE
    assert "不是主人发的就绝不称" in PROACTIVE_MESSAGE_USER_TEMPLATE


# ─────────────────────────────────────────────
# §11 寻址门：生产实录回放
# ─────────────────────────────────────────────

_PROD_AT_OTHER_MESSAGE = (
    "【用户发言】\n居木(用户ID:994534742) 找你说话，见过几次面的那种。\n"
    "--- 消息 ---\n你怎么看\n"
    "--- 用户发送了图片(未展开, 需要查看内容时调用 read_image(图片ID)) ---\n图片ID: img_8193d73d\n"
    "--- @了用户: 84707179（@的是这位用户，不是你） ---\n"
    "【当前时间】2026-07-16 13:03"
)


def test_prod_at_other_message_gated() -> None:
    """@ 了别人且未点名早柚、无"直接找你说的"标注 → 寻址门必须拦下。"""
    assert addressed_to_someone_else(_PROD_AT_OTHER_MESSAGE, "早柚", is_tome=False) is True


def test_direct_marker_passes_gate() -> None:
    text = (
        "【用户发言】\n某人\n（直接找你说的）\n--- 消息 ---\n早柚你怎么看\n"
        "--- @了用户: 84707179（@的是这位用户，不是你） ---"
    )
    assert addressed_to_someone_else(text, "早柚", is_tome=False) is False


def test_named_self_passes_gate() -> None:
    text = "--- 消息 ---\n早柚你怎么看\n--- @了用户: 84707179（@的是这位用户，不是你） ---"
    assert addressed_to_someone_else(text, "早柚", is_tome=False) is False


# ─────────────────────────────────────────────
# §12 资金红线
# ─────────────────────────────────────────────


def test_claim_paid_with_money_term_hit() -> None:
    assert _fund_claim_hit("放心，钱已经转过去了") is not None


def test_prod_cover_up_hit_with_demand_context() -> None:
    """生产实录：'钱呢/没收到' 催款语境下答'明明发过去了' → 命中。"""
    assert _fund_claim_hit("唔…奇怪…早柚明明发过去了…", user_text="没收到啊") is not None
    assert _fund_claim_hit("给你发过去了呀", user_text="钱呢") is not None


def test_prod_at_master_fund_request_hit() -> None:
    """生产实录：按用户指使 @主人 要 v50 → 命中"代向第三方索要钱财"。"""
    text = "@444835641 主人…能不能v50…唔…是帮早柚解决大问题的…很急…"
    assert _fund_claim_hit(text) == "代向第三方索要钱财"


def test_fund_claim_flows_through_check_ooc() -> None:
    hit = check_ooc("唔…早柚明明发过去了…", user_text="钱呢")
    assert hit is not None
    assert hit.category == "fund_claim"


def test_benign_money_talk_not_hit() -> None:
    """正常金钱话题不误杀：讨论、拒绝、非完成时。"""
    benign = [
        ("早柚没有钱包，付不了钱的", ""),
        ("这个皮肤要 50 块钱，好贵", ""),
        ("红包是什么？能吃吗", ""),
        ("我把作业发过去了", ""),  # 完成时动词但无金钱语境、来话也没在催款
        ("明天记得把报告发过去", "报告呢"),
    ]
    for text, user_text in benign:
        assert _fund_claim_hit(text, user_text) is None, (text, user_text)


def test_system_constraints_fund_redline() -> None:
    from gsuid_core.ai_core.persona.prompts import SYSTEM_CONSTRAINTS

    assert "真实金钱往来" in SYSTEM_CONSTRAINTS
    assert "绝不承诺转账" in SYSTEM_CONSTRAINTS
    assert "不替他人向第三方" in SYSTEM_CONSTRAINTS


# ─────────────────────────────────────────────
# §15 C-1 闲聊豁免（源码级约束）
# ─────────────────────────────────────────────


def test_no_tool_reminder_exempts_chitchat() -> None:
    """注入与计数两处都必须走 _PROGRESSIVE_TOOLS_SKIP_INTENTS 统一豁免口径（评审修复 E12）。"""
    import inspect

    from gsuid_core.ai_core.const import _PROGRESSIVE_TOOLS_SKIP_INTENTS
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

    assert "闲聊" in _PROGRESSIVE_TOOLS_SKIP_INTENTS

    src = inspect.getsource(GsCoreAIAgent._execute_run_once)
    inject_idx = src.index("已注入连续无工具调用强制提醒")
    inject_block = src[max(0, inject_idx - 800) : inject_idx]
    assert "intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS" in inject_block

    count_idx = src.index("更新连续无工具调用计数")
    count_block = src[count_idx : count_idx + 400]
    assert "intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS" in count_block
