"""
动态工具发现模块

提供动态工具发现能力，允许AI根据任务需求搜索可能用到的新工具。
当AI发现自己缺乏某个能力时，可以调用此工具来发现可用的工具。
"""

from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.rag.tools import search_tools, search_tools_by_domain


# 不声明 capability_domain：find_tools 是单例 meta 工具，无能力族语义；声明了反而会被
# L3 会话驻留按族带进随后数轮（含闲聊），破坏"闲聊轮零开销"。它的装配完全由意图门控制。
@ai_tools(category="buildin")
async def find_tools(
    ctx: RunContext[ToolContext],
    need: str,
) -> str:
    """按需加载完成任务所缺的工具（渐进式工具暴露）。

    当你发现当前可用工具里**没有**能完成用户需求的工具时，用一句话描述你需要的能力，
    调用本工具。命中的相关工具会在**下一步**变为可直接调用——不要在本步假装调用它们，
    先调用本工具把它们加载进来，再在后续步骤正式调用。

    适用场景示例：
    - 用户的追问语义太短、当前工具列表里找不到合适工具时（如澄清后回了个地名/时间）；
    - 需要某类专门能力（查询某游戏数据、渲染图片、读写文件、查数据库等）但工具不在列。

    Args:
        ctx: 工具执行上下文。
        need: 你需要的能力的自然语言描述，越具体越好（如"查询某城市的实时天气"）。

    Returns:
        本次加载到的工具清单；这些工具下一步即可调用。
    """
    try:
        # Phase 3a 两段式·domain 粒度检索：先语义召回（含 Reranker 精排），再聚合到
        # capability_domain 整族纳入，保证"能创建就能改/删"，加载到的工具语义连贯而非零散单点。
        family_tools = await search_tools_by_domain(query=need, domain_limit=3, per_domain_limit=6)
        if not family_tools:
            return f"⚠️ 没有找到与「{need}」相关的工具，请换个更具体的描述，或直接据现有能力作答。"

        loaded_names = [t.name for t in family_tools]
        ctx.deps.dynamic_tool_names.update(loaded_names)

        logger.info(f"🧠 [find_tools] 为需求「{need[:40]}」动态加载 {len(loaded_names)} 个工具: {loaded_names}")
        listing = "\n".join(f"- {name}" for name in loaded_names)
        return f"✅ 已加载以下工具，下一步即可直接调用：\n{listing}"

    except RuntimeError as e:
        logger.warning(f"🧠 [find_tools] AI功能未启用: {e}")
        return "⚠️ 工具检索功能未启用，无法动态加载工具。"
    except Exception as e:
        logger.error(f"🧠 [find_tools] 工具加载失败: {e}")
        return f"⚠️ 工具加载失败: {str(e)}"


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
