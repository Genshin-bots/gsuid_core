"""Classifier 模块 —— 用户消息意图分类

在主人格收到一条消息后，用本模块的分类器先做一次轻量意图判断
（闲聊 / 工具调用 / 问答），帮助 ``gs_agent`` 决定走哪条 ``ai_mode`` 路径：

- "闲聊"  → 走 persona 直接回复，不强制装配工具池；
- "工具"  → 装配完整保底池 + 向量检索补充；
- "问答"  → 触发 ``search_cognition`` 优先链路。

分类器按句式归类，不加载机器学习库。单例是 ``classifier_service``。
主人格的决策树仍是权威，分类结果只提示工具池怎么装。

模块组成：
- ``mode_classifier.py`` : 句式归类与上文拼接
"""

from gsuid_core.ai_core.classifier.mode_classifier import classifier_service

__all__ = ["classifier_service"]
