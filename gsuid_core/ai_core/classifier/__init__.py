"""
Classifier 模块

意图分类模块，使用机器学习模型对用户输入进行意图分类。
支持的意图类型：闲聊、工具、问答。
"""

from gsuid_core.ai_core.classifier.mode_classifier import classifier_service

__all__ = ["classifier_service"]
