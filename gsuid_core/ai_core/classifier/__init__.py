"""Classifier 模块 —— 单轮路由。

``TurnRouter`` 用一次无思考低档模型调用统一决定意图、记忆模态、软触发开口与
事务委托信号；离线/单测环境退化为纯结构规则。
"""

from gsuid_core.ai_core.classifier.turn_router import (
    TurnIntent,
    MemoryIntent,
    TurnRouteDecision,
    route_turn,
    structural_route,
)
from gsuid_core.ai_core.classifier.mode_classifier import classifier_service

__all__ = [
    "MemoryIntent",
    "TurnIntent",
    "TurnRouteDecision",
    "route_turn",
    "structural_route",
    "classifier_service",
]
