"""Classifier 模块 —— 用户消息意图分类

在主人格收到一条消息后，用本模块的分类器先做一次轻量意图判断
（闲聊 / 工具调用 / 问答），帮助 ``gs_agent`` 决定走哪条 ``ai_mode`` 路径：

- "闲聊"  → 走 persona 直接回复，不强制装配工具池；
- "工具"  → 装配完整保底池 + 向量检索补充；
- "问答"  → 触发 ``search_knowledge`` 优先链路。

分类器是机器学习模型，单例 ``classifier_service`` 在首次调用时懒加载。
**注意**：这只是一个"加速器"——主人格 prompt 的决策树仍是权威，分类结果
只是辅助工具池装配的提示，不替代决策树。

模块组成：
- ``mode_classifier.py`` : 分类器服务 + 模型加载
"""

from gsuid_core.ai_core.classifier.mode_classifier import classifier_service

__all__ = ["classifier_service"]
