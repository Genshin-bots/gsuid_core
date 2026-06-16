"""嵌入模态类型定义

定义嵌入模型支持的模态枚举，以及把配置里的字符串列表解析成模态集合的工具函数。
"""

from enum import Enum
from typing import Union


class EmbeddingModality(str, Enum):
    """嵌入模型支持的模态类型

    用户在嵌入模型配置中声明所用模型支持哪些模态（持久化进配置），
    检索/入库管线据此决定能否对某类内容直接做向量嵌入。
    """

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"

    @classmethod
    def from_str(cls, value: str) -> "Union[EmbeddingModality, None]":
        normalized = value.strip().lower()
        for modality in cls:
            if modality.value == normalized:
                return modality
        return None


def parse_modalities(values: list[str]) -> set[EmbeddingModality]:
    """把配置里的字符串列表解析成模态集合。

    始终包含 TEXT（任何嵌入模型都至少支持文本）；无法识别的取值被忽略。
    """
    result: set[EmbeddingModality] = {EmbeddingModality.TEXT}
    for value in values:
        modality = EmbeddingModality.from_str(value)
        if modality is not None:
            result.add(modality)
    return result
