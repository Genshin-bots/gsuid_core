"""单句意图的薄兼容层。

语义路由已收敛到 ``classifier/turn_router.py`` 的 ``TurnRouter``；本模块只保留
``IntentService`` 的旧调用形状，内部走确定性结构退化，不再包含任何业务词表
或语义正则。
"""

from typing import Literal, Protocol, Sequence, TypedDict

from gsuid_core.ai_core.classifier.turn_router import (
    TurnRouteDecision,
    structural_route,
    collect_prior_user_turns as _collect_prior_user_turns,
)


class _UserTurnRecord(Protocol):
    role: Literal["user", "assistant", "system"]
    user_id: str
    content: str


def collect_prior_user_turns(
    records: Sequence[_UserTurnRecord],
    current_user_id: str,
    *,
    max_turns: int = 4,
) -> list[str]:
    return _collect_prior_user_turns(records, current_user_id, max_turns=max_turns)


class IntentHit(TypedDict):
    text: str
    intent: str
    conf: float
    reason: str


def _hit(text: str, decision: TurnRouteDecision) -> IntentHit:
    return {
        "text": text,
        "intent": decision.intent,
        "conf": 0.6,
        "reason": "structural_fallback",
    }


class IntentService:
    """旧接口：只做结构退化，真实路由由 ``TurnRouter`` 在 CLASSIFY 点位完成。"""

    def _sync_predict(
        self,
        text: str,
        *,
        prev_turn_used_tools: bool = False,
    ) -> IntentHit:
        return _hit(text, structural_route(text, prev_turn_used_tools=prev_turn_used_tools))

    async def predict_async(
        self,
        text: str,
        *,
        prior_user_turns: list[str] | None = None,
        prev_turn_used_tools: bool = False,
    ) -> IntentHit:
        return self._sync_predict(text, prev_turn_used_tools=prev_turn_used_tools)


classifier_service = IntentService()
