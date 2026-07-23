"""意图分类：同用户上下文拼接 + 省略式跟进结构升级。"""

import asyncio

from gsuid_core.ai_core.classifier.mode_classifier import (
    IntentService,
    _is_ellipsis_followup,
    collect_prior_user_turns,
)


class _Rec:
    def __init__(self, role: str, content: str, user_id: str = "u1") -> None:
        self.role = role
        self.content = content
        self.user_id = user_id


def test_ellipsis_followup_shapes() -> None:
    assert _is_ellipsis_followup("然后呢")
    assert _is_ellipsis_followup("结果呢？")
    assert _is_ellipsis_followup("改成明天")
    assert not _is_ellipsis_followup("今天天气真好想出去玩")
    assert not _is_ellipsis_followup("帮我看看面板数据")
    # 纯确认/附和不是「未完成动作」省略跟进
    assert not _is_ellipsis_followup("是吗")
    assert not _is_ellipsis_followup("可以吗？")
    assert not _is_ellipsis_followup("好吗")


def test_collect_prior_filters_other_users() -> None:
    recs = [
        _Rec("user", "看下面板", "u1"),
        _Rec("assistant", "唔…", "bot"),
        _Rec("user", "然后呢", "u2"),
        _Rec("user", "还有呢", "u1"),
    ]
    prior = collect_prior_user_turns(recs, "u1", max_turns=4)
    assert prior == ["看下面板", "还有呢"]


def test_ellipsis_after_tools_structural_upgrade() -> None:
    """上轮真用过工具时，「然后呢」必须升级为工具，不能单句闲聊。"""
    svc = IntentService()

    async def _run() -> None:
        res = await svc.predict_async(
            "然后呢",
            prior_user_turns=["帮我查一下面板数据"],
            prev_turn_used_tools=True,
        )
        assert res["intent"] == "工具", res
        # ContextPrimary（拼接上文）或 Structural（上轮工具）均可；不得是裸 Model 单句闲聊
        assert any(k in res["reason"] for k in ("ContextPrimary", "ContextJoin", "Structural", "ellipsis")), res

    asyncio.run(_run())


def test_ellipsis_after_toolish_priors_without_tool_flag() -> None:
    """最终 ModelResponse 可能是纯文本：靠上文用户句是否工具向来升级。"""
    svc = IntentService()

    async def _run() -> None:
        res = await svc.predict_async(
            "然后呢",
            prior_user_turns=["帮我查一下面板数据"],
            prev_turn_used_tools=False,
        )
        assert res["intent"] == "工具", res
        assert "Context" in res["reason"] or "Structural" in res["reason"] or "prior" in res["reason"].lower(), res

    asyncio.run(_run())


def test_pure_greeting_stays_chat() -> None:
    """真寒暄不因空上下文被抬成工具。"""
    svc = IntentService()

    async def _run() -> None:
        res = await svc.predict_async("你好", prior_user_turns=[], prev_turn_used_tools=False)
        assert res["intent"] == "闲聊", res

    asyncio.run(_run())
