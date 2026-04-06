"""
AI日期工具模块

提供获取当前日期和时间的工具函数。
"""

from typing import Optional
from datetime import datetime

from gsuid_core.logger import logger
from gsuid_core.ai_core.register import ai_tools


@ai_tools()
async def get_current_date(
    format: Optional[str] = None,
) -> str:
    """
    获取当前日期和时间

    返回当前的日期、时间、星期等信息。可用于回答用户关于时间的问题。

    Args:
        ctx: 工具执行上下文
        format: 可选，日期格式字符串，默认为"%Y年%m月%d日 %H:%M:%S"

    Returns:
        当前日期时间字符串，包含日期、时间、星期信息

    Example:
        >>> date = await get_current_date(ctx)
        >>> date = await get_current_date(ctx, format="%Y-%m-%d")
    """
    try:
        now = datetime.now()

        # 默认格式
        date_format = format or "%Y年%m月%d日 %H:%M:%S"

        # 格式化日期时间
        formatted_date = now.strftime(date_format)

        # 获取星期（中文）
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday = weekdays[now.weekday()]

        # 构建结果字符串
        if not format:
            result = f"当前时间：{formatted_date} ({weekday})"
        else:
            result = formatted_date

        logger.info(f"🧠 [BuildinTools] 获取当前日期: {result}")
        return result

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 获取日期失败: {e}")
        return f"获取日期失败：{str(e)}"
