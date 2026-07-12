"""Persona 情绪状态机模块

提供 Persona 的情绪状态管理能力，让 AI 角色能够跨对话积累情绪状态：
- 被赞美后表现出好心情
- 被多次无视后表现出轻微冷淡
- 收到坏消息时表现出关切

情绪状态存储在内存中（按 session 隔离），在构建 Persona Prompt 时注入。

使用方式:
    from gsuid_core.ai_core.persona.mood import get_mood_state, update_mood

    mood = await get_mood_state(persona_name="早柚", group_id="123456")
    await update_mood(persona_name="早柚", group_id="123456", event_type="praise")
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Dict, Optional, TypedDict
from dataclasses import field, dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger


class MoodType(str, Enum):
    """情绪类型枚举"""

    NEUTRAL = "neutral"  # 中性/平静
    HAPPY = "happy"  # 开心/愉悦
    EXCITED = "excited"  # 兴奋/激动
    WARM = "warm"  # 温暖/亲切
    COLD = "cold"  # 冷淡/疏远
    CONCERNED = "concerned"  # 关切/担忧
    SAD = "sad"  # 难过/失落
    ANNOYED = "annoyed"  # 烦躁/不满


# 情绪类型 -> 中文描述映射 (Prompt-2.5: 改为第一人称内心状态)
MOOD_DESCRIPTIONS: Dict[MoodType, str] = {
    MoodType.NEUTRAL: "",  # 中性不注入，保持角色默认状态
    MoodType.HAPPY: "刚刚发生了点开心的事，心情比平时好一点",
    MoodType.EXCITED: "有件事让你觉得挺有意思的，有点停不下来",
    MoodType.WARM: "对方说的话让你感觉还不错",
    MoodType.COLD: "最近有点不想说话",
    MoodType.CONCERNED: "有点担心",
    MoodType.SAD: "有点心情不好，不想多说",
    MoodType.ANNOYED: "被烦到了，耐心有限",
}

# 情绪衰减时间常数（秒）：情绪强度每经过此时间衰减一半
MOOD_HALF_LIFE_SECONDS = 1800  # 30 分钟


@dataclass
class MoodState:
    """Persona 情绪状态

    Attributes:
        mood: 当前情绪类型
        intensity: 情绪强度 (0.0 ~ 1.0)
        trigger: 触发情绪的事件描述
        updated_at: 最后更新时间
    """

    mood: MoodType = MoodType.NEUTRAL
    intensity: float = 0.5
    trigger: str = ""
    updated_at: float = field(default_factory=time.time)

    @property
    def effective_intensity(self) -> float:
        """考虑时间衰减后的有效情绪强度

        情绪会随时间自然衰减，趋向中性。
        """
        elapsed = time.time() - self.updated_at
        decay_factor = 0.5 ** (elapsed / MOOD_HALF_LIFE_SECONDS)
        return self.intensity * decay_factor

    @property
    def description(self) -> str:
        """获取情绪状态的描述文本，用于注入 Persona Prompt"""
        effective = self.effective_intensity

        # 强度太低时视为中性
        if effective < 0.1 or self.mood == MoodType.NEUTRAL:
            return ""

        base_desc = MOOD_DESCRIPTIONS.get(self.mood, "状态正常")

        # 根据强度添加修饰
        if effective > 0.8:
            intensity_word = "非常明显地"
        elif effective > 0.5:
            intensity_word = "略微"
        elif effective > 0.2:
            intensity_word = "轻微地"
        else:
            intensity_word = "隐约"

        return f"{intensity_word}{base_desc}"

    def to_dict(self) -> "MoodStateDict":
        """转换为字典"""
        return MoodStateDict(
            mood=self.mood.value,
            intensity=self.intensity,
            trigger=self.trigger,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_dict(cls, data: "MoodStateDict") -> MoodState:
        """从字典创建"""
        return cls(
            mood=MoodType(data["mood"]),
            intensity=data["intensity"],
            trigger=data["trigger"],
            updated_at=data["updated_at"],
        )


class MoodStateDict(TypedDict):
    """MoodState 的字典表示，用于序列化/反序列化"""

    mood: str
    intensity: float
    trigger: str
    updated_at: float


# 全局情绪状态存储: {(persona_name, group_id): MoodState}
_mood_states: Dict[str, MoodState] = {}


def _make_mood_key(persona_name: str, group_id: str) -> str:
    """生成情绪状态的存储 key"""
    return f"{persona_name}@@{group_id}"


async def get_mood_state(
    persona_name: str,
    group_id: str,
) -> Optional[MoodState]:
    """获取指定 Persona 在指定群聊的情绪状态

    Args:
        persona_name: Persona 名称
        group_id: 群聊 ID

    Returns:
        MoodState 实例，如果从未设置过则返回 None
    """
    key = _make_mood_key(persona_name, group_id)
    return _mood_states.get(key)


async def get_mood_description(
    persona_name: str,
    group_id: str,
) -> str:
    """获取情绪状态的描述文本（用于注入 Prompt）

    如果情绪强度衰减到阈值以下，返回空字符串。

    Args:
        persona_name: Persona 名称
        group_id: 群聊 ID

    Returns:
        情绪描述文本，或空字符串
    """
    mood = await get_mood_state(persona_name, group_id)
    if mood is None:
        return ""
    return mood.description


async def update_mood(
    persona_name: str,
    group_id: str,
    event_type: str,
    intensity_delta: float = 0.2,
    trigger: str = "",
) -> MoodState:
    """更新 Persona 的情绪状态

    根据事件类型调整情绪。新情绪会与当前情绪混合（加权平均），
    避免情绪突变。

    Args:
        persona_name: Persona 名称
        group_id: 群聊 ID
        event_type: 事件类型，支持:
            - "praise": 被赞美 → 开心
            - "ignore": 被无视 → 冷淡
            - "bad_news": 坏消息 → 关切
            - "greeting": 友好问候 → 温暖
            - "argument": 争执 → 烦躁
            - "sad_news": 伤心事 → 难过
            - "exciting": 兴奋的事 → 兴奋
            - "neutral": 重置为中性
        intensity_delta: 情绪变化幅度 (0.0 ~ 1.0)
        trigger: 触发事件的描述

    Returns:
        更新后的 MoodState
    """
    key = _make_mood_key(persona_name, group_id)
    current = _mood_states.get(key)

    # 事件类型 -> 目标情绪映射
    event_to_mood: Dict[str, MoodType] = {
        "praise": MoodType.HAPPY,
        "ignore": MoodType.COLD,
        "bad_news": MoodType.CONCERNED,
        "greeting": MoodType.WARM,
        "argument": MoodType.ANNOYED,
        "sad_news": MoodType.SAD,
        "exciting": MoodType.EXCITED,
        "neutral": MoodType.NEUTRAL,
    }

    target_mood = event_to_mood.get(event_type, MoodType.NEUTRAL)

    if current is None:
        # 首次设置
        new_state = MoodState(
            mood=target_mood,
            intensity=min(intensity_delta, 1.0),
            trigger=trigger,
        )
    else:
        # 混合当前情绪和新事件
        effective_current = current.effective_intensity

        if target_mood == MoodType.NEUTRAL:
            # 重置为中性：强度衰减
            new_intensity = max(effective_current - intensity_delta, 0.0)
            new_state = MoodState(
                mood=MoodType.NEUTRAL if new_intensity < 0.1 else current.mood,
                intensity=new_intensity,
                trigger=trigger or current.trigger,
            )
        elif target_mood == current.mood:
            # 同类情绪叠加
            new_intensity = min(effective_current + intensity_delta * 0.5, 1.0)
            new_state = MoodState(
                mood=target_mood,
                intensity=new_intensity,
                trigger=trigger or current.trigger,
            )
        else:
            # 不同情绪：新情绪强度足够大时切换，否则混合
            if intensity_delta > effective_current:
                new_state = MoodState(
                    mood=target_mood,
                    intensity=min(intensity_delta, 1.0),
                    trigger=trigger,
                )
            else:
                # 保持当前情绪，但略微降低强度
                new_state = MoodState(
                    mood=current.mood,
                    intensity=max(effective_current - intensity_delta * 0.3, 0.0),
                    trigger=current.trigger,
                )

    _mood_states[key] = new_state

    logger.debug(
        t(
            "🎭 [Mood] {persona_name}@{group_id}: {p0} (强度: {p1:.2f}, 触发: {p2})",
            persona_name=persona_name,
            group_id=group_id,
            p0=new_state.mood.value,
            p1=new_state.intensity,
            p2=trigger or "N/A",
        )
    )

    return new_state


async def reset_mood(persona_name: str, group_id: str) -> None:
    """重置 Persona 在指定群聊的情绪状态为中性

    Args:
        persona_name: Persona 名称
        group_id: 群聊 ID
    """
    key = _make_mood_key(persona_name, group_id)
    _mood_states.pop(key, None)
    logger.debug(t("🎭 [Mood] {persona_name}@{group_id}: 情绪已重置", persona_name=persona_name, group_id=group_id))


def get_all_mood_states() -> Dict[str, MoodState]:
    """获取所有情绪状态（用于调试/监控）"""
    return dict(_mood_states)
