"""
AI Core Statistics Module
AI 模块统计数据管理

提供 Token 消耗、响应延迟、意图分布、Heartbeat 决策等统计功能。
支持每日数据持久化（启动/关闭/零点重置）。
"""

from gsuid_core.ai_core.statistics.manager import StatisticsManager, statistics_manager

__all__ = ["StatisticsManager", "statistics_manager"]
