"""记忆系统提示词模块

包含所有 LLM 调用所需的提示词模板。
"""

from .summary import GROUP_SUMMARY_PROMPT
from .selection import NODE_SELECTION_PROMPT_TEMPLATE

__all__ = [
    "NODE_SELECTION_PROMPT_TEMPLATE",
    "GROUP_SUMMARY_PROMPT",
]
