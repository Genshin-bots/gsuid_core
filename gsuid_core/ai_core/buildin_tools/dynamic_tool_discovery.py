"""
动态工具发现模块

提供动态工具发现能力，允许AI根据任务需求搜索可能用到的新工具。
当AI发现自己缺乏某个能力时，可以调用此工具来发现可用的工具。
"""

from typing import Optional
from dataclasses import replace

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.rag.tools import search_tools, search_tools_by_domain


def _match_capability_agents_for_need(need: str, *, limit: int = 5) -> list[str]:
    """按 need 文本匹配已注册能力代理，返回展示行（通用，无业务特判）。"""
    from gsuid_core.ai_core.agent_node import list_nodes
    from gsuid_core.ai_core.agent_node.registry import match_capability_node

    need_s = (need or "").strip()
    if not need_s:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    # 整句最长关键词匹配
    primary = match_capability_node(need_s)
    if primary:
        from gsuid_core.ai_core.agent_node import get_node

        node = get_node(primary)
        if node is not None:
            when = (node.when_to_use or "").strip() or node.display_name
            lines.append(f"- `{node.node_id}`（{node.display_name}）：{when}")
            seen.add(node.node_id)
    # 再扫注册表弱匹配补全
    blob = need_s.lower()
    for node in list_nodes():
        if node.node_id in seen:
            continue
        if node.source == "persona" or node.node_id == "capability_evaluator":
            continue
        hay = f"{node.node_id} {node.display_name} {node.when_to_use} {' '.join(node.match_keywords)}".lower()
        hit = False
        for kw in node.match_keywords:
            k = (kw or "").strip().lower()
            if k and k in blob:
                hit = True
                break
        if not hit:
            for token in blob.replace("，", " ").split():
                if len(token) >= 2 and token in hay:
                    hit = True
                    break
        if not hit:
            continue
        when = (node.when_to_use or "").strip() or node.display_name
        lines.append(f"- `{node.node_id}`（{node.display_name}）：{when}")
        seen.add(node.node_id)
        if len(lines) >= limit:
            break
    return lines


# 不声明 capability_domain（会被 L3 按族驻留带进闲聊轮）；category 必须为 meta：
# 落入 buildin 等保底分类会让渐进式暴露门控失效、加载的工具无人暴露（实测踩坑）。
@ai_tools(category="meta")
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

        # 检索层不感知 visible_when，须与暴露层同用 prepare_tool_def 预判：隐藏工具若照报
        # "已加载"，模型按名调用必 Unknown tool 并反复重试（实测踩坑）。静默剔除，仅落日志。
        loaded_names: list[str] = []
        hidden_names: list[str] = []
        for tool in family_tools:
            run_ctx = replace(
                ctx,
                tool_name=tool.name,
                retry=0,
                max_retries=tool.max_retries if tool.max_retries is not None else 1,
            )
            try:
                tool_def = await tool.prepare_tool_def(run_ctx)
            except Exception as e:
                logger.debug(t("log.ai.find_tools_prepare_treated_unavailable_fail", p0=tool.name, e=e))
                tool_def = None
            (loaded_names if tool_def else hidden_names).append(tool.name)

        if hidden_names:
            logger.info(
                t(
                    "log.ai.find_tools_matched_excluded",
                    p0=len(hidden_names),
                    hidden_names=hidden_names,
                )
            )
        # 主人格交互轮：能力代理专属工具不得经 find_tools 回灌（与静态池剥离同口径）
        blocked = ctx.deps.blocked_tool_names
        if blocked:
            loaded_names = [n for n in loaded_names if n not in blocked]

        if not loaded_names:
            # 与"检索无命中"同文案：不向模型泄露被隐藏工具的存在，避免诱导换措辞反复检索。
            return f"⚠️ 没有找到与「{need}」相关的工具，请换个更具体的描述，或直接据现有能力作答。"

        ctx.deps.dynamic_tool_names.update(loaded_names)

        logger.info(
            t(
                "log.ai.find_tools_dynamically_requirement_load",
                p0=need[:40],
                p1=len(loaded_names),
                loaded_names=loaded_names,
            )
        )
        listing = "\n".join(f"- {name}" for name in loaded_names)
        parts = [f"✅ 已加载以下工具，下一步即可直接调用：\n{listing}"]
        # 通用：同步提示可委派的能力代理（插件注册的 node_id），不特判业务域
        agent_lines = _match_capability_agents_for_need(need)
        if agent_lines:
            parts.append(
                '若任务适合专职代理，请用 create_subagent(agent_profile="<node_id>", task=...) 委派：\n'
                + "\n".join(agent_lines)
            )
        return "\n".join(parts)

    except RuntimeError as e:
        logger.warning(t("log.ai.find_tools_feature_enabled", e=e))
        return "⚠️ 工具检索功能未启用，无法动态加载工具。"
    except Exception as e:
        logger.error(t("log.ai.find_tools_event", e=e))
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

        logger.info(t("log.ai.tooldisc_found_tools_task", p0=len(discovered_tools), p1=task[:50]))
        return "\n".join(result_parts)

    except RuntimeError as e:
        # AI功能未启用
        logger.warning(t("log.ai.tooldisc_feature_enabled", e=e))
        return "⚠️ AI工具搜索功能未启用，无法发现新工具。"
    except Exception as e:
        logger.error(t("log.ai.tooldisc_discovery", e=e))
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
        logger.error(t("log.ai.listavailabletools_get_list", e=e))
        return f"⚠️ 获取工具列表失败: {str(e)}"
