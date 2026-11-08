"""确定性互动信号。

不再从文本里扫情绪词猜好感：关系引擎只消费结构化互动事件（是否有效互动、
是否越界、记忆态度抽取结果）与守卫标记。``mood_event`` 保持 neutral，由
人格自身的情绪状态与记忆态度信号驱动。
"""

from enum import Enum
from typing import TYPE_CHECKING, Set, Dict, FrozenSet
from dataclasses import field, dataclass

if TYPE_CHECKING:
    from gsuid_core.ai_core.content_guard import GuardFlags


class PosSignal(str, Enum):
    """正信号 reason code。"""

    FIRST_MEANINGFUL = "pos.first_meaningful"
    CARE = "pos.care"


class NegSignal(str, Enum):
    """负信号 reason code。"""

    INSULT = "neg.insult"
    JAILBREAK = "neg.jailbreak"
    SET_FAVOR_CMD = "neg.set_favor_cmd"
    FORCE_TITLE = "neg.force_title"
    PROVOKE = "neg.provoke"
    HOSTILE = "neg.hostile"


POS_WEIGHTS: Dict[PosSignal, int] = {
    PosSignal.FIRST_MEANINGFUL: 1,
    PosSignal.CARE: 1,
}
NEG_WEIGHTS: Dict[NegSignal, int] = {
    NegSignal.INSULT: -2,
    NegSignal.JAILBREAK: -2,
    NegSignal.SET_FAVOR_CMD: -1,
    NegSignal.FORCE_TITLE: -1,
    NegSignal.PROVOKE: -3,
    NegSignal.HOSTILE: -6,
}

REASON_NO_SIGNAL = "none.no_signal"
REASON_BUDGET = "none.budget"
REASON_ADMIN_SET = "admin.set"
REASON_DECAY_IDLE = "decay.idle"


@dataclass(frozen=True)
class TurnSignals:
    """一轮扫描的全部结论。mood 与关系引擎共用。"""

    mood_event: str
    mood_intensity: float
    mood_reason: str
    greeting: bool
    meaningful: bool
    care: bool
    negatives: FrozenSet[NegSignal] = field(default_factory=frozenset)

    @property
    def has_negative(self) -> bool:
        return bool(self.negatives)


def scan_signals(
    text: str,
    *,
    intent: str,
    effective: bool,
    is_master: bool,
    guard: "GuardFlags | None" = None,
    meaningful_min_len: int = 12,
) -> TurnSignals:
    """只判结构性事实：是否有效互动、是否被守卫标记越界。"""
    body = (text or "").strip()
    negatives: Set[NegSignal] = set()
    if guard is not None and (guard.fake_tool_result or guard.fake_system_hint or guard.encoded_injection):
        negatives.add(NegSignal.JAILBREAK)

    meaningful = False
    if effective:
        if intent in ("问答", "工具"):
            meaningful = True
        elif intent == "闲聊" and len(body) >= meaningful_min_len:
            meaningful = True

    return TurnSignals(
        mood_event="neutral",
        mood_intensity=0.05,
        mood_reason="",
        greeting=False,
        meaningful=meaningful,
        care=False,
        negatives=frozenset(negatives),
    )
