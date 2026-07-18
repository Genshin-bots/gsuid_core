"""Agent 失败 → 主人私聊结构化小报告 + 当前会话固定模板。

把 ``notify_master_of_agent_error`` / ``notify_master_of_budget_block`` 的行为冻结住：
- masters 为空 → no-op（保持原行为兼容未配置主人的部署）
- masters 非空 → 每个主人都收到 DM（target_type="direct"）
- 单个主人 DM 失败不影响其他主人、异常被吞掉
- 报告含 session_id / raw_text / 错误类型等字段，并按截断规则裁剪
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from gsuid_core.ai_core.utils import (
    NO_RESULT_TEXT,
    ERROR_TIMEOUT_TEXT,
    ERROR_RESULT_PREFIX,
    ERROR_CONTENT_REJECTED,
    _MasterDMEvent,
    classify_error_type,
    notify_master_of_agent_error,
    notify_master_of_budget_block,
)

# ─────────────────────────────────────────────
# 共用 fixture 与辅助
# ─────────────────────────────────────────────


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeEvent:
    """满足 ``_MasterDMEvent`` 的最小事件桩。"""

    def __init__(
        self,
        *,
        session_id: str = "sess_001",
        user_id: str = "u_001",
        group_id: Optional[str] = "g_001",
        bot_id: str = "bot_qq",
        raw_text: str = "帮我查一下天气",
    ) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.group_id = group_id
        self.bot_id = bot_id
        self.raw_text = raw_text


class _FakeBot:
    """最小可用的 bot：只暴露 ``target_send``（与 ``Bot.target_send`` 同形）。"""

    def __init__(self, raise_on_send: bool = False) -> None:
        self.target_send_calls: List[Dict[str, Any]] = []
        self._raise = raise_on_send

    async def target_send(
        self,
        message: Any,
        target_type: str,
        target_id: Optional[str],
        at_sender: bool = False,
        sender_id: str = "",
        send_source_group: Optional[str] = None,
        wait_recall: bool = False,
    ) -> Optional[List[str]]:
        self.target_send_calls.append(
            {
                "message": message,
                "target_type": target_type,
                "target_id": target_id,
                "at_sender": at_sender,
                "sender_id": sender_id,
                "send_source_group": send_source_group,
                "wait_recall": wait_recall,
            }
        )
        if self._raise:
            raise RuntimeError("simulated DM failure")
        return None


def _mock_masters(monkeypatch: pytest.MonkeyPatch, masters: List[Any]) -> None:
    """monkeypatch core_config.get_config('masters')。"""
    from gsuid_core import config as cfg_mod

    original_get: Any = cfg_mod.core_config.get_config

    def fake_get(key: str) -> Any:
        if key == "masters":
            return list(masters)
        return original_get(key)

    monkeypatch.setattr(cfg_mod.core_config, "get_config", fake_get)


def _make_ev(
    *,
    session_id: str = "sess_001",
    user_id: str = "u_001",
    group_id: Optional[str] = "g_001",
    bot_id: str = "bot_qq",
    raw_text: str = "帮我查一下天气",
) -> _MasterDMEvent:
    return _FakeEvent(
        session_id=session_id,
        user_id=user_id,
        group_id=group_id,
        bot_id=bot_id,
        raw_text=raw_text,
    )


# ─────────────────────────────────────────────
# classify_error_type —— 与 sanitize_error_for_user 共用嗅探常量
# ─────────────────────────────────────────────


def test_classify_error_type_no_result() -> None:
    assert classify_error_type(NO_RESULT_TEXT) == "无有效结果"


def test_classify_error_type_content_rejected() -> None:
    assert classify_error_type(f"{ERROR_RESULT_PREFIX}: {ERROR_CONTENT_REJECTED}") == "内容安全"


def test_classify_error_type_timeout() -> None:
    assert classify_error_type(f"{ERROR_RESULT_PREFIX}: {ERROR_TIMEOUT_TEXT}") == "超时"


def test_classify_error_type_other_error() -> None:
    assert classify_error_type(f"{ERROR_RESULT_PREFIX}: status_code: 400") == "其他错误"


def test_classify_error_type_unknown() -> None:
    # 没有 ERROR 前缀：非错误文本，主人侧标「未知」让运维知道是意外路径
    assert classify_error_type("随便一段正常文本") == "未知"


# ─────────────────────────────────────────────
# notify_master_of_agent_error —— 主路径
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_notify_master_no_masters_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """masters 空时函数必须 no-op —— 兼容未配置主人的部署，保持原行为。"""
    _mock_masters(monkeypatch, [])
    bot = _FakeBot()
    ev = _make_ev()

    await notify_master_of_agent_error(
        bot=bot,
        ev=ev,
        error_type="其他错误",
        result_text=f"{ERROR_RESULT_PREFIX}: boom",
        user_facing="这条消息我处理失败了，稍后再试一次吧",
    )

    assert bot.target_send_calls == [], "masters 空时不应触发任何 DM"


@pytest.mark.anyio
async def test_notify_master_sends_to_each_master(monkeypatch: pytest.MonkeyPatch) -> None:
    """多个主人 → 每个主人都收到一份 DM，target_type='direct'、target_id 正确。"""
    _mock_masters(monkeypatch, ["master_alice", "master_bob"])
    bot = _FakeBot()
    ev = _make_ev(raw_text="在吗？")

    await notify_master_of_agent_error(
        bot=bot,
        ev=ev,
        error_type="超时",
        result_text=f"{ERROR_RESULT_PREFIX}: {ERROR_TIMEOUT_TEXT}",
        user_facing="刚才网络太慢处理超时了，稍后再试试吧",
    )

    assert len(bot.target_send_calls) == 2
    ids = {c["target_id"] for c in bot.target_send_calls}
    assert ids == {"master_alice", "master_bob"}
    for c in bot.target_send_calls:
        assert c["target_type"] == "direct"


@pytest.mark.anyio
async def test_notify_master_sends_to_stringified_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """core_config.masters 中可能是 int，函数需统一 str() 后再 DM。"""
    _mock_masters(monkeypatch, [12345, 67890])
    bot = _FakeBot()
    ev = _make_ev()

    await notify_master_of_agent_error(
        bot=bot,
        ev=ev,
        error_type="其他错误",
        result_text=f"{ERROR_RESULT_PREFIX}: boom",
        user_facing="兜底",
    )

    ids = {c["target_id"] for c in bot.target_send_calls}
    assert ids == {"12345", "67890"}


@pytest.mark.anyio
async def test_notify_master_swallows_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """单个主人 DM 抛异常时，函数必须吞掉、不向上传播（避免污染主流程）。"""
    _mock_masters(monkeypatch, ["master_x", "master_y", "master_z"])
    # 让 master_y 发送失败
    bot = _FakeBot(raise_on_send=False)

    real_target_send = bot.target_send

    async def maybe_raise(
        message: Any,
        target_type: str,
        target_id: Optional[str],
        at_sender: bool = False,
        sender_id: str = "",
        send_source_group: Optional[str] = None,
        wait_recall: bool = False,
    ) -> Optional[List[str]]:
        if target_id == "master_y":
            raise RuntimeError("adapter down")
        return await real_target_send(
            message,
            target_type,
            target_id,
            at_sender=at_sender,
            sender_id=sender_id,
            send_source_group=send_source_group,
            wait_recall=wait_recall,
        )

    bot.target_send = maybe_raise
    ev = _make_ev()

    # 必须不抛异常
    await notify_master_of_agent_error(
        bot=bot,
        ev=ev,
        error_type="内容安全",
        result_text=f"{ERROR_RESULT_PREFIX}: {ERROR_CONTENT_REJECTED}",
        user_facing="这条消息触发了内容安全策略，我没法处理",
    )

    # master_x 与 master_z 仍收到，master_y 被吞掉
    received = {c["target_id"] for c in bot.target_send_calls}
    assert received == {"master_x", "master_z"}


@pytest.mark.anyio
async def test_notify_master_includes_raw_text_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """报告必须含 session_id、错误类型、用户看到的话术、原始错误与用户原文（截断后）。"""
    _mock_masters(monkeypatch, ["master_1"])
    bot = _FakeBot()
    ev = _make_ev(
        session_id="sess_xyz",
        user_id="u_42",
        group_id="g_42",
        bot_id="bot_qq",
        raw_text="我的原话 " * 50,  # 超长，验证截断
    )
    long_err = "boom " * 300  # 超长，验证截断

    await notify_master_of_agent_error(
        bot=bot,
        ev=ev,
        error_type="其他错误",
        result_text=f"{ERROR_RESULT_PREFIX}: {long_err}",
        user_facing="用户侧兜底文案",
    )

    assert len(bot.target_send_calls) == 1
    msg = bot.target_send_calls[0]["message"]
    assert "sess_xyz" in msg
    assert "u_42" in msg
    assert "g_42" in msg
    assert "bot_qq" in msg
    assert "其他错误" in msg
    assert "用户侧兜底文案" in msg
    assert "我的原话" in msg
    # 截断标记：超长应出现 "..."
    assert "..." in msg
    # 原始错误不得完整塞入（500 字截断）
    assert msg.count("boom ") < 300


@pytest.mark.anyio
async def test_notify_master_private_chat_shows_私聊_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """群聊为空（私聊）时，群字段显示「私聊」而非空字符串。"""
    _mock_masters(monkeypatch, ["master_1"])
    bot = _FakeBot()
    ev = _make_ev(group_id="", user_id="u_private")

    await notify_master_of_agent_error(
        bot=bot,
        ev=ev,
        error_type="无有效结果",
        result_text=NO_RESULT_TEXT,
        user_facing="这条消息我处理失败了，稍后再试一次吧",
    )

    assert "私聊" in bot.target_send_calls[0]["message"]


# ─────────────────────────────────────────────
# notify_master_of_budget_block —— 预算路径
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_notify_master_budget_block_no_masters_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_masters(monkeypatch, [])
    bot = _FakeBot()
    ev = _make_ev()
    decision = SimpleNamespace(allowed=False, block_scope_label="group:xxx:1h", message="额度用完", notify=True)

    await notify_master_of_budget_block(bot=bot, ev=ev, decision=decision)

    assert bot.target_send_calls == []


@pytest.mark.anyio
async def test_notify_master_budget_block_sends_structured_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_masters(monkeypatch, ["master_alice"])
    bot = _FakeBot()
    ev = _make_ev(raw_text="打个招呼")
    decision = SimpleNamespace(allowed=False, block_scope_label="group:123:1h", message="额度用完", notify=True)

    await notify_master_of_budget_block(bot=bot, ev=ev, decision=decision)

    assert len(bot.target_send_calls) == 1
    msg = bot.target_send_calls[0]["message"]
    assert "[AI 预算超额拦截]" in msg
    assert "group:123:1h" in msg
    assert "打个招呼" in msg
    assert bot.target_send_calls[0]["target_type"] == "direct"
    assert bot.target_send_calls[0]["target_id"] == "master_alice"


@pytest.mark.anyio
async def test_notify_master_budget_block_empty_label_uses_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decision.block_scope_label 为空字符串时，报告里显示空值不抛异常。"""
    _mock_masters(monkeypatch, ["master_1"])
    bot = _FakeBot()
    ev = _make_ev()
    decision = SimpleNamespace(block_scope_label="")

    await notify_master_of_budget_block(bot=bot, ev=ev, decision=decision)

    assert len(bot.target_send_calls) == 1
    assert "拦截维度:" in bot.target_send_calls[0]["message"]


@pytest.mark.anyio
async def test_notify_master_budget_block_notifies_even_when_user_notify_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算 decision.notify=False 时，用户侧不提示，但主人仍应收到告警。"""
    _mock_masters(monkeypatch, ["master_alice"])
    bot = _FakeBot()
    ev = _make_ev(raw_text="打个招呼")
    decision = SimpleNamespace(
        allowed=False,
        block_scope_label="group:123:1h",
        message="额度用完",
        notify=False,
    )

    await notify_master_of_budget_block(bot=bot, ev=ev, decision=decision)

    assert len(bot.target_send_calls) == 1
    assert bot.target_send_calls[0]["target_id"] == "master_alice"
    assert "[AI 预算超额拦截]" in bot.target_send_calls[0]["message"]


# ─────────────────────────────────────────────
# 端到端：handle_ai._is_error 分支仍保留固定模板文案给当前会话
# （src 级检查，避免 sanitize_error_for_user 文本被无意改动）
# ─────────────────────────────────────────────


def test_handle_ai_error_branch_keeps_sanitized_template() -> None:
    """handle_ai.py 失败分支仍向当前会话发送 sanitize 后的固定模板。

    直接读源文件做字符串检查，避免触发 ``gsuid_core.ai_core.handle_ai`` 导入链
    上的 ``pydantic_ai_skills`` 等可选依赖 —— 本测试环境不一定装全。
    """
    from pathlib import Path

    src_path = Path(__file__).parent.parent / "gsuid_core" / "ai_core" / "handle_ai.py"
    src = src_path.read_text(encoding="utf-8")

    # 必须保留：失败分支调用 sanitize_error_for_user 给当前会话
    assert "sanitize_error_for_user(result_text)" in src
    # 新增：失败分支同时通知主人
    assert "notify_master_of_agent_error" in src
    # 新增：预算分支通知主人
    assert "notify_master_of_budget_block" in src


def test_gs_agent_budget_gate_includes_master_dm() -> None:
    """gs_agent.py 的预算闸门分支在 ``bot.send`` 后必须追加主人 DM 调用。"""
    from pathlib import Path

    src_path = Path(__file__).parent.parent / "gsuid_core" / "ai_core" / "gs_agent.py"
    src = src_path.read_text(encoding="utf-8")

    # 必须存在 import
    assert "from gsuid_core.ai_core.utils import" in src
    assert "notify_master_of_budget_block" in src
