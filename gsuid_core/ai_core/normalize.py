"""命令层别名归一化（非 LLM 强匹配 fallback）

> R1（plans/agent_design_review.md）：本模块原 `normalize_query` 是核心 AI
> 链路中的 dead code（全代码库无调用点），朴素字符串替换也不适合 LLM 语义链路。
> AI 记忆 / 检索的别名消歧已改由 C2 动态实体链接承担。本模块据此降级为
> **传统命令解析的非 LLM fallback**——0 成本、强匹配，仅供命令前缀归一化使用，
> 不参与任何 LLM 推理 / 记忆 / 检索链路。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.register import get_aliases_for_scope


def command_alias_normalizer(text: str, scope: str = "global") -> str:
    """把文本中出现的已注册别名替换为正式名称（命令层强匹配）。

    仅用于传统命令解析的 fallback，**不得用于 LLM 记忆 / 检索链路**——
    语义消歧应交由 C2 动态实体链接按上下文判断，而非字符串替换。

    Args:
        text:  待归一化文本
        scope: 别名作用域，默认 "global"
    """
    aliases = get_aliases_for_scope(scope)
    if not aliases:
        return text
    for alias, formals in aliases.items():
        if alias and formals and alias in text:
            text = text.replace(alias, formals[0])
    return text


def normalize_query(text: str) -> str:
    """向后兼容的旧函数名（已无调用点，保留以防外部引用）。

    新代码请改用 `command_alias_normalizer`。
    """
    logger.trace(t("🧠 [Normalize] normalize_query 已降级为命令层 fallback，建议改用 command_alias_normalizer"))
    return command_alias_normalizer(text)
