"""AI Session 预算限制子系统。

按 Session（群聊 / 私聊 / 群内某成员）对 AI 的 Token 消耗设置预算上限，支持
滚动 5 小时 / 天 / 周三档窗口、白名单突破限制、主人豁免等精细配置。

对外只暴露 `budget_manager` 单例与 `BudgetDecision`；数据库表与配置在各自模块内。
"""

from .manager import BudgetDecision, budget_manager

__all__ = ["budget_manager", "BudgetDecision"]
