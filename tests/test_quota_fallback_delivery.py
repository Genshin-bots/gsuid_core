"""配额兜底的**下发层**行为锁（``turn_pipeline.deliver_run_result``）。

``test_quota_guard.py`` 只锁了 ``QuotaBreaker`` 本身（记账 / 窗口 / 分闸）。本文件锁的是
下游接线——那才是用户看得见的部分，且此前零覆盖：

- 配额类失败：同一会话在抑制窗内**只发第一句**，其余静默；
- 抑制**不**外溢到别的会话，也不吞掉私聊主人的详情报告；
- 超时等偶发失败**不**抑制（用户重问就该重答）；
- 任一成功轮必须 ``reset`` 去重键，否则后续真实失败也被静默漏发。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

import gsuid_core.ai_core.turn_pipeline as tp
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.const import ERROR_QUOTA_EXHAUSTED
from gsuid_core.ai_core.utils import ERROR_RESULT_PREFIX
from gsuid_core.ai_core.quota_guard import quota_breaker


@pytest.fixture(autouse=True)
def _clean_breaker() -> Any:
    """熔断器是进程级单例；每个用例前后清空，避免互相污染。"""
    quota_breaker.clear()
    yield
    quota_breaker.clear()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """把 ``send_chat_result`` / 主人通知换成计数器，返回调用记录。"""
    sent: List[str] = []
    master: List[Dict[str, str]] = []

    async def _fake_send(_bot: Bot, text: str, **_kw: object) -> None:
        sent.append(text)

    async def _fake_master(**kw: object) -> None:
        master.append({k: str(v) for k, v in kw.items()})

    monkeypatch.setattr(tp, "send_chat_result", _fake_send)
    monkeypatch.setattr(tp, "notify_master_of_agent_error", _fake_master)
    return {"sent": sent, "master": master}


def _deliver(group_id: str, result_text: str, *, is_error: bool, chat_result: str = "") -> None:
    # 真 Event 的 session_id 由 group_id 派生，天然做到「按会话」隔离
    ev = Event(group_id=group_id, user_type="group")
    asyncio.run(
        tp.deliver_run_result(
            Bot.__new__(Bot),
            ev,
            chat_result or result_text,
            result_text=result_text,
            is_silence=False,
            is_error=is_error,
            intent="chat",
        )
    )


_QUOTA_TEXT = f"{ERROR_RESULT_PREFIX}: {ERROR_QUOTA_EXHAUSTED}"


# ── 一、配额类重复失败：同会话只放行第一句 ────────────────────────────────


def test_quota_fallback_sent_once_then_suppressed(wired: Dict[str, Any]) -> None:
    """连着两轮同样打满：用户只看到一句，不会在群里连喷。"""
    _deliver("s1", _QUOTA_TEXT, is_error=True)
    _deliver("s1", _QUOTA_TEXT, is_error=True)
    _deliver("s1", _QUOTA_TEXT, is_error=True)
    assert len(wired["sent"]) == 1, f"配额兜底应只发一句，实际 {len(wired['sent'])} 句"


def test_quota_suppression_does_not_leak_across_sessions(wired: Dict[str, Any]) -> None:
    """一个会话被抑制，不妨碍别的会话照常收到兜底。"""
    _deliver("s1", _QUOTA_TEXT, is_error=True)
    _deliver("s2", _QUOTA_TEXT, is_error=True)
    assert len(wired["sent"]) == 2


def test_master_still_gets_every_quota_report(wired: Dict[str, Any]) -> None:
    """抑制只针对用户可见兜底；详情必须照常私聊主人，否则线上无从排查。"""
    _deliver("s1", _QUOTA_TEXT, is_error=True)
    _deliver("s1", _QUOTA_TEXT, is_error=True)
    assert len(wired["master"]) == 2, f"主人应收到 2 份详情，实际 {len(wired['master'])}"
    assert all(m["error_type"] == "套餐用量打满" for m in wired["master"])


def test_timeout_failure_is_not_suppressed(wired: Dict[str, Any]) -> None:
    """偶发失败不压：用户重问就应当重答。"""
    from gsuid_core.ai_core.utils import ERROR_TIMEOUT_TEXT

    timeout_text = f"{ERROR_RESULT_PREFIX}: {ERROR_TIMEOUT_TEXT}"
    _deliver("s1", timeout_text, is_error=True)
    _deliver("s1", timeout_text, is_error=True)
    assert len(wired["sent"]) == 2


# ── 二、成功轮必须清掉去重键 ─────────────────────────────────────────────


def test_success_after_quota_failure_resets_suppression(wired: Dict[str, Any]) -> None:
    """配额恢复后的一次真实失败必须能出声，不能被上一轮的去重键永久静默。"""
    _deliver("s1", _QUOTA_TEXT, is_error=True)
    # 恢复：正常回复一轮（应走 reset 分支）
    _deliver("s1", "今天天气不错", is_error=False, chat_result="今天天气不错")
    # 再次失败（换成超时，避免仍然命中配额分支）——必须能发出来
    _deliver("s1", f"{ERROR_RESULT_PREFIX}: 网络开小差了", is_error=True)
    assert len(wired["sent"]) == 3, f"恢复后的失败被静默了，实际只发 {len(wired['sent'])} 句"


def test_success_alone_sends_once(wired: Dict[str, Any]) -> None:
    """正常轮不受抑制逻辑影响，且不误触 reset 之外的路径。"""
    _deliver("s1", "在的在的", is_error=False, chat_result="在的在的")
    assert wired["sent"] == ["在的在的"]
    assert wired["master"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
