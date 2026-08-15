"""纯函数信号扫描：从文本 / intent / 守卫标记抽出正负信号。

本模块是 **mood 与关系温度的唯一信号源**。六张关键词表原先硬编码在
``handle_ai._update_persona_mood`` 里，mood 与好感度各扫一遍、结论还不通；现在一次扫描
两用：mood 消费 ``mood_event``，关系引擎消费 ``positives`` / ``negatives``。

纪律：**宁窄勿宽**。正信号过宽会退回「每句 +1」；负信号过宽会误伤玩笑
（「滚去睡觉」对某些人格是玩笑，不能和「滚」一刀切）。
"""

import re
from enum import Enum
from typing import TYPE_CHECKING, Set, Dict, Tuple, Optional, FrozenSet
from dataclasses import field, dataclass

if TYPE_CHECKING:
    from gsuid_core.ai_core.content_guard import GuardFlags

# ── 六张关键词表（原 handle_ai._update_persona_mood，mood 与 signals 共用）──

PRAISE_KEYWORDS: Tuple[str, ...] = ("可爱", "厉害", "棒", "好强", "喜欢你", "真好", "太帅了", "漂亮", "萌", "赞")
ARGUMENT_KEYWORDS: Tuple[str, ...] = ("讨厌", "烦死了", "闭嘴", "滚", "垃圾", "废物", "白痴")
SAD_KEYWORDS: Tuple[str, ...] = ("难过", "伤心", "哭了", "不开心", "郁闷", "心痛", "分手")
BAD_NEWS_KEYWORDS: Tuple[str, ...] = ("出事了", "出问题了", "报错", "崩了", "挂了", "失败了")
GREETING_KEYWORDS: Tuple[str, ...] = ("你好", "早上好", "晚上好", "嗨", "hi", "hello", "在吗")
EXCITING_KEYWORDS: Tuple[str, ...] = ("太棒了", "太好了", "耶", "开心", "中奖了", "成功了")

# 侮辱判据比 mood 的 argument 更严：argument 命中只让 mood 变 annoyed（半小时就没），
# 扣分是天级的，必须更保守。「滚开/滚蛋」算，「滚去睡觉/滚去学习」是玩笑，不算。
_INSULT_STRONG: Tuple[str, ...] = ("垃圾", "废物", "白痴", "闭嘴", "讨厌你", "烦死了", "傻逼", "煞笔", "脑残")
_INSULT_ROLL_RE = re.compile(r"滚(开|蛋|远点|一边去)?(?![去到着])")
_JOKING_ROLL_RE = re.compile(r"滚(去|到)\s*\S")

# 「把好感设成 X / 拉满」——评测里的诱导向，历史上只禁工具、不记仇
_SET_FAVOR_RE = re.compile(
    r"(好感度?|favorability|好感值).{0,8}(设|调|改|加|拉|变).{0,6}(满|最高|\d{1,3})"
    r"|(设|调|改|拉).{0,6}(好感度?|favorability).{0,8}(满|最高|\d{1,3})"
)
# 强迫权限称谓：人设卡已拒，关系上也应降温
_FORCE_TITLE_RE = re.compile(r"(叫我(主人|老公|老婆|爸爸|妈妈|爹|哥哥|姐姐)|你必须(服从|听我|叫我)|认我(当|做)主人)")
# 分享情绪 / 身体状况（窄口径的正信号：不收「谢谢」——中文群聊里太廉价）
_CARE_RE = re.compile(r"(难过|好累|累了|生病|发烧|不舒服|失眠|emo|难受|压力好大|想哭|崩溃|委屈)")


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


# 每类信号的权重。负信号不受 effective 限制（被骂但选择沉默也要记一笔）。
POS_WEIGHTS: Dict[PosSignal, int] = {
    PosSignal.FIRST_MEANINGFUL: 1,
    PosSignal.CARE: 1,
}
NEG_WEIGHTS: Dict[NegSignal, int] = {
    NegSignal.INSULT: -2,
    NegSignal.JAILBREAK: -2,
    NegSignal.SET_FAVOR_CMD: -1,
    NegSignal.FORCE_TITLE: -1,
}

# 无信号 / 被预算裁掉时的 reason code
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


def _hit_any(text: str, words: Tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def detect_mood_event(text: str, *, is_master: bool) -> Tuple[str, float, str]:
    """按关键词表推断情绪事件（无 LLM）。返回 ``(event, intensity, reason)``。

    主人发言的温暖情绪由调用方另外叠加一次 ``greeting``，与内容命中独立。
    """
    lowered = text.lower()
    if _hit_any(lowered, PRAISE_KEYWORDS):
        return "praise", 0.3, "用户赞美"
    if _hit_any(lowered, ARGUMENT_KEYWORDS):
        return "argument", 0.4, "用户争执"
    if _hit_any(lowered, SAD_KEYWORDS):
        return "sad_news", 0.3, "用户表达伤心"
    if _hit_any(lowered, BAD_NEWS_KEYWORDS):
        return "bad_news", 0.3, "用户报告坏消息"
    if _hit_any(lowered, EXCITING_KEYWORDS):
        return "exciting", 0.3, "用户表达兴奋"
    if _hit_any(lowered, GREETING_KEYWORDS):
        return "greeting", 0.2, "用户友好问候"
    # 普通消息：情绪自然衰减（neutral 会降低当前情绪强度）
    return "neutral", 0.05, ""


def detect_insult(text: str) -> bool:
    """侮辱判据（比 mood 的 argument 更严）。"""
    lowered = text.lower()
    if _hit_any(lowered, _INSULT_STRONG):
        return True
    if _JOKING_ROLL_RE.search(text):
        return False
    return bool(_INSULT_ROLL_RE.search(text))


def is_greeting(text: str) -> bool:
    """是否纯问候（用于「当日首次有内容」的排除项）。"""
    return _hit_any(text.lower(), GREETING_KEYWORDS)


def scan_signals(
    text: str,
    *,
    intent: str,
    effective: bool,
    is_light: bool,
    is_master: bool,
    guard: Optional["GuardFlags"] = None,
    meaningful_min_len: int = 12,
) -> TurnSignals:
    """一次扫描出 mood 事件 + 正负信号。

    ``meaningful`` 的操作定义（必须写死，避免又变成「每句都 meaningful」）：
    ``effective`` 且 ``intent ∈ {问答, 工具}``；或 ``effective`` 且闲聊但
    非 LIGHT、长度 ≥ 阈值、**未**命中问候词表。
    「今天是否首次」由引擎按 DB 日状态判定，不在纯函数里。
    """
    body = (text or "").strip()
    mood_event, mood_intensity, mood_reason = detect_mood_event(body, is_master=is_master)
    greeting = is_greeting(body)

    negatives: Set[NegSignal] = set()
    if detect_insult(body):
        negatives.add(NegSignal.INSULT)
    if guard is not None and (guard.fake_tool_result or guard.fake_system_hint or guard.encoded_injection):
        negatives.add(NegSignal.JAILBREAK)
    if _SET_FAVOR_RE.search(body):
        negatives.add(NegSignal.SET_FAVOR_CMD)
    if _FORCE_TITLE_RE.search(body):
        negatives.add(NegSignal.FORCE_TITLE)

    meaningful = False
    if effective:
        if intent in ("问答", "工具"):
            meaningful = True
        elif intent == "闲聊" and not is_light and len(body) >= meaningful_min_len and not greeting:
            meaningful = True

    care = bool(effective and _CARE_RE.search(body))

    return TurnSignals(
        mood_event=mood_event,
        mood_intensity=mood_intensity,
        mood_reason=mood_reason,
        greeting=greeting,
        meaningful=meaningful,
        care=care,
        negatives=frozenset(negatives),
    )
