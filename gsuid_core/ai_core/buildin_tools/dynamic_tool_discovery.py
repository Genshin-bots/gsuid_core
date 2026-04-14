"""
动态工具发现模块

提供动态工具发现能力，允许AI根据任务需求搜索可能用到的新工具。
当AI发现自己缺乏某个能力时，可以调用此工具来发现可用的工具。
"""

from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.rag.tools import search_tools


# @ai_tools(category="buildin")
async def discover_tools(
    ctx: RunContext[ToolContext],
    task: str,
    limit: int = 5,
) -> str:
    """
    动态工具发现工具

    当AI发现自己无法直接完成某个任务，需要调用特定工具时，
    可以使用此工具来发现当前可用的相关工具。

    这对于扩展AI能力边界、发现隐藏功能特别有用。
    例如：当用户询问需要数据库操作、文件处理、Web搜索、网页渲染、编写代码等能力时。

    Args:
        ctx: 工具执行上下文
        task: 任务描述，需要什么能力或想完成什么任务
        limit: 最大返回工具数量，默认5个

    Returns:
        发现的工具列表和使用建议

    Example:
        >>> result = await discover_tools(ctx, "需要读取某个文件的内容")
        >>> result = await discover_tools(ctx, "需要查询用户的好友列表")
        >>> result = await discover_tools(ctx, "需要发送消息通知用户")
    """
    try:
        # 搜索相关工具，排除self类别（避免递归调用）
        discovered_tools = await search_tools(
            query=task,
            limit=limit,
            non_category="self",
        )

        if not discovered_tools:
            return "⚠️ 没有发现与该任务相关的工具。请尝试用更具体的描述。"

        # 构建结果描述
        result_parts = ["🔧 发现以下可能有帮助的工具：\n"]

        for i, tool in enumerate(discovered_tools, 1):
            tool_name = getattr(tool, "name", str(tool))
            tool_desc = getattr(tool, "description", "无描述")
            result_parts.append(f"{i}. **{tool_name}**")
            if tool_desc and tool_desc != "无描述":
                result_parts.append(f"   描述: {tool_desc}")
            result_parts.append("")

        result_parts.append("\n提示: 如果需要使用上述工具，请调整回答，说明该任务需要调用特定工具才能完成。")

        logger.info(f"🧠 [DynamicToolDiscovery] 发现 {len(discovered_tools)} 个工具用于任务: {task[:50]}")
        return "\n".join(result_parts)

    except RuntimeError as e:
        # AI功能未启用
        logger.warning(f"🧠 [DynamicToolDiscovery] AI功能未启用: {e}")
        return "⚠️ AI工具搜索功能未启用，无法发现新工具。"
    except Exception as e:
        logger.error(f"🧠 [DynamicToolDiscovery] 工具发现失败: {e}")
        return f"⚠️ 工具发现失败: {str(e)}"


# @ai_tools(category="buildin")
async def list_available_tools(
    ctx: RunContext[ToolContext],
    category: Optional[str] = None,
) -> str:
    """
    列出可用工具

    获取当前系统中所有可用的AI工具，可以按分类查看。
    这对于了解系统能力边界很有帮助。

    Args:
        ctx: 工具执行上下文
        category: 可选，按分类筛选，如"buildin"、"common"、"default"

    Returns:
        可用工具列表

    Example:
        >>> result = await list_available_tools(ctx)
        >>> result = await list_available_tools(ctx, category="buildin")
    """
    try:
        from gsuid_core.ai_core.register import get_registered_tools

        all_tools_cag = get_registered_tools()

        if category:
            # 指定分类
            if category in all_tools_cag:
                tools_dict = all_tools_cag[category]
            else:
                return f"⚠️ 未知的工具分类: {category}，可用分类: {list(all_tools_cag.keys())}"
        else:
            # 返回所有分类
            tools_dict = {}
            for cat_tools in all_tools_cag.values():
                tools_dict.update(cat_tools)

        if not tools_dict:
            return "⚠️ 当前没有可用的工具。"

        result_parts = ["🛠️ 可用工具列表：\n"]

        if category:
            result_parts.append(f"分类: {category}\n")

        for tool_name, tool_base in tools_dict.items():
            desc = getattr(tool_base, "description", "无描述") or "无描述"
            result_parts.append(f"- **{tool_name}**: {desc}")

        result_parts.append(f"\n共 {len(tools_dict)} 个工具")

        return "\n".join(result_parts)

    except Exception as e:
        logger.error(f"🧠 [ListAvailableTools] 获取工具列表失败: {e}")
        return f"⚠️ 获取工具列表失败: {str(e)}"
