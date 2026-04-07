"""System Prompt 数据模型"""

from typing import List, TypedDict


class SystemPrompt(TypedDict):
    """System Prompt 数据模型

    Attributes:
        id: 唯一标识符
        title: 标题
        desc: 描述（用于向量检索）
        content: 完整内容（作为系统提示词）
        tags: 标签列表
    """

    id: str
    title: str
    desc: str
    content: str
    tags: List[str]
