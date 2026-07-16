"""P2 修复回归测试（plans/prod_session_review §13/§14/§16/§21/§22/§23/§24）。

覆盖：资源 ID 契约 docstring、@数字→at 转换、好感度方向判据、图像理解不确定性表述、
历史连发合并的插话切块语义、模型请求墙钟收紧、跨群 Kanban 脱敏。
"""

import time
import inspect
from typing import Any, Optional

import pytest

# ─────────────────────────────────────────────
# §13 资源 ID 契约（docstring 是召回与使用说明的唯一来源）
# ─────────────────────────────────────────────


def test_send_message_by_ai_forbids_fabricated_ids() -> None:
    from gsuid_core.ai_core.buildin_tools.message_sender import send_message_by_ai

    doc = send_message_by_ai.__doc__ or ""
    assert "禁止自行构造" in doc
    assert "实际出现过的 ID" in doc


# ─────────────────────────────────────────────
# §14 @数字 → 平台真实 at（裸 ID 不落入用户可见文本）
# ─────────────────────────────────────────────


def test_at_digits_become_at_segment() -> None:
    from gsuid_core.ai_core.utils import _parse_at_segments

    segments = _parse_at_segments("好哦 @444835641 你来看")
    types = [s.type for s in segments]
    assert "at" in types
    at_seg = segments[types.index("at")]
    assert at_seg.data == "444835641" or "444835641" in str(at_seg.data)
    # 文本段里不残留裸 ID
    for s in segments:
        if s.type == "text":
            assert "444835641" not in str(s.data)


# ─────────────────────────────────────────────
# §16 好感度方向判据（源码级约束）
# ─────────────────────────────────────────────


def test_favorability_skips_silence_and_error_rounds() -> None:
    """好感度门须复用步骤 8 的结果分类，且以 last_run_sent_visible_reply 判 by_bot
    成功轮（run 返回空串，仅靠返回值判定会让正常互动永不加分，评审修复 F1）。"""
    import gsuid_core.ai_core.handle_ai as handle_ai_mod

    src = inspect.getsource(handle_ai_mod)
    idx = src.index("update_favorability(str(event.user_id)")
    gate_block = src[max(0, idx - 700) : idx]
    assert "last_run_sent_visible_reply" in gate_block
    assert "_is_error" in gate_block
    # 分类定义处必须引用协议常量而非魔法串（评审修复 E11）
    assert "ERROR_RESULT_PREFIX" in src
    assert "SILENCE_MARKERS" in src


# ─────────────────────────────────────────────
# §21 图像理解不确定性表述
# ─────────────────────────────────────────────


def test_image_understand_prompt_requires_hedging() -> None:
    import gsuid_core.ai_core.image_understand.understand as u

    src = inspect.getsource(u)
    assert "看起来像" in src
    assert "不要给出笃定的断言" in src


# ─────────────────────────────────────────────
# §22 历史连发合并：他人插话必须切块（冻结当前正确语义，防回归）
# ─────────────────────────────────────────────


def _record(user_id: str, name: str, content: str, ts: float) -> Any:
    from gsuid_core.message_history.manager import MessageRecord

    return MessageRecord(role="user", user_id=user_id, user_name=name, content=content, timestamp=ts)


def test_interleaved_speaker_breaks_merge() -> None:
    """生产场景重放：好好与秋秋交错发言，合并不得吞掉交错顺序。"""
    from gsuid_core.ai_core.history_format import format_history_for_agent

    t0 = time.time() - 600
    history = [
        _record("1904448665", "秋秋", "喝", t0),
        _record("1904448665", "秋秋", "我陪你", t0 + 5),
        _record("944722078", "好好", "昨天刚喝", t0 + 60),
        _record("1904448665", "秋秋", "没事的", t0 + 70),
        _record("944722078", "好好", "多邻国？", t0 + 80),
    ]
    text = format_history_for_agent(history)
    # 所有消息都在
    for content in ("喝", "我陪你", "昨天刚喝", "没事的", "多邻国？"):
        assert content in text, content
    # 顺序保持：昨天刚喝 在 没事的 之前，没事的 在 多邻国 之前
    assert text.index("昨天刚喝") < text.index("没事的") < text.index("多邻国？")
    # "没事的"不得被并进秋秋更早的连发块（它前面隔了好好的插话）：
    # 若被并块，其会紧跟"我陪你"出现在同一块内、且先于"昨天刚喝"。
    assert text.index("我陪你") < text.index("昨天刚喝")


def test_same_speaker_burst_merged() -> None:
    """无人插话的连发仍然合并为一个发言块（只出现一次说话人头行）。"""
    from gsuid_core.ai_core.history_format import format_history_for_agent

    t0 = time.time() - 600
    history = [
        _record("944722078", "好好", "多邻国？", t0),
        _record("944722078", "好好", "算了，明天晚上点个汉堡", t0 + 10),
        _record("944722078", "好好", "今天先不喝", t0 + 20),
    ]
    text = format_history_for_agent(history)
    assert text.count("944722078(好好)") == 1


# ─────────────────────────────────────────────
# §23 模型请求墙钟（源码级约束）
# ─────────────────────────────────────────────


def test_openai_client_timeout_tightened() -> None:
    from gsuid_core.ai_core.configs import models

    src = inspect.getsource(models.get_openai_model_by_name)
    assert "max_retries=1" in src
    assert "MODEL_REQUEST_TIMEOUT" in src
    # §23 墙钟常量本体 + 三工厂统一走共享池/超时（评审修复 F8/E13）
    assert models.MODEL_REQUEST_TIMEOUT.read == 180.0
    assert "_shared_model_http_client" in inspect.getsource(models.get_anthropic_chat_model_by_name)
    assert "_shared_model_http_client" in inspect.getsource(models.get_gemini_model_by_name)


# ─────────────────────────────────────────────
# §24 跨群 Kanban 脱敏
# ─────────────────────────────────────────────


class _FakeTask:
    def __init__(self, ordinal: int, name: str, group_id: Optional[str]) -> None:
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
async def test_other_group_tasks_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.ai_core.planning.context as ctx_mod

    async def fake_list_for_owner(user_id: str, only_active: bool = True, root_only: bool = True) -> list:
        return [
            _FakeTask(36, "群666249732 AI模拟盘 周期托管", "666249732"),
            _FakeTask(37, "本群翻译任务", "681600567"),
        ]

    monkeypatch.setattr(ctx_mod.AIAgentTask, "list_for_owner", fake_list_for_owner)

    async def fake_get_task_tree(task_id: str) -> tuple:
        return None, []

    monkeypatch.setattr(ctx_mod.kanban_manager, "get_task_tree", fake_get_task_tree)

    text = await ctx_mod.build_task_context("444835641", current_group_id="681600567")
    # 本群任务展开，他群任务只留脱敏计数
    assert "本群翻译任务" in text
    assert "666249732" not in text
    assert "模拟盘" not in text
    assert "1 个任务在其他会话" in text


@pytest.mark.anyio
async def test_no_group_id_keeps_full_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """私聊（current_group_id=None）保持完整视图（用户本人看自己的任务没有泄露问题）。"""
    import gsuid_core.ai_core.planning.context as ctx_mod

    async def fake_list_for_owner(user_id: str, only_active: bool = True, root_only: bool = True) -> list:
        return [_FakeTask(36, "群666249732 AI模拟盘 周期托管", "666249732")]

    monkeypatch.setattr(ctx_mod.AIAgentTask, "list_for_owner", fake_list_for_owner)

    async def fake_get_task_tree(task_id: str) -> tuple:
        return None, []

    monkeypatch.setattr(ctx_mod.kanban_manager, "get_task_tree", fake_get_task_tree)

    text = await ctx_mod.build_task_context("444835641", current_group_id=None)
    assert "模拟盘" in text
