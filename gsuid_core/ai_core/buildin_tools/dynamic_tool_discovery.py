"""
动态工具发现模块

提供动态工具发现能力，允许AI根据任务需求搜索可能用到的新工具。
当AI发现自己缺乏某个能力时，可以调用此工具来发现可用的工具。
"""

from typing import Any, Optional, Sequence
from dataclasses import replace

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.rag.tools import search_tools, search_tools_by_domain
from gsuid_core.ai_core.buildin_tools.visibility import (
    check_sched_create,
    check_sched_mutate,
)
from gsuid_core.ai_core.buildin_tools.find_tools_rank import (
    RankedHit,
    tool_brief,
    agent_is_dedicated,
    build_find_tools_plan,
    classify_callable_tool,
    format_find_tools_plan,
    need_matches_node_text,
    need_matches_tool_text,
)

FIND_TOOLS_LOADED_KEY = "find_tools_last_loaded"
FIND_TOOLS_GAP_NOTE = (
    "（系统：连续检索未暴露新工具。下一动：改 need 再检索，或委派通用调研。禁止对用户讲检索过程、禁止念工具名。）"
)


def _need_matches_tool_text(need: str, retrieval_text: str, covers: list[str]) -> bool:
    """兼容旧导入；实现见 find_tools_rank.need_matches_tool_text。"""
    return need_matches_tool_text(need, retrieval_text, covers)


def _record_find_tools_round(extra: dict[str, Any], loaded: list[str]) -> bool:
    """记下本轮暴露名。返回 True 表示相对上一轮没有新名字。"""
    from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY

    had_prev = FIND_TOOLS_LOADED_KEY in extra
    prev_raw = extra[FIND_TOOLS_LOADED_KEY] if had_prev else None
    prev: set[str] = set(prev_raw) if isinstance(prev_raw, list) else set()
    extra[FIND_TOOLS_LOADED_KEY] = list(loaded)
    expose = [n for n in loaded if not n.startswith("agent:")]
    exposed_raw = extra[EXPOSED_TOOLS_EXTRA_KEY] if EXPOSED_TOOLS_EXTRA_KEY in extra else None
    if isinstance(exposed_raw, list):
        for n in expose:
            if n not in exposed_raw:
                exposed_raw.append(n)
    else:
        extra[EXPOSED_TOOLS_EXTRA_KEY] = list(expose)
    if not had_prev:
        return False
    return set(loaded) <= prev


def _node_hit(node_id: str, score: float) -> RankedHit | None:
    from gsuid_core.ai_core.agent_node import get_node

    node = get_node(node_id)
    if node is None:
        return None
    when = (node.when_to_use or "").strip() or node.display_name
    return RankedHit(name=node.node_id, label=when, score=score)


def _node_on_topic(need: str, node_id: str) -> bool:
    """专用节点入档前必须对口；语义邻居 / 短关键词不算命中。"""
    from gsuid_core.ai_core.agent_node import get_node
    from gsuid_core.ai_core.agent_node.semantic_routing import aggregate_node_covers

    node = get_node(node_id)
    if node is None:
        return False
    covers = aggregate_node_covers(node)
    return need_matches_node_text(
        need,
        when_to_use=node.when_to_use,
        display_name=node.display_name,
        keywords=tuple(node.match_keywords),
        covers=tuple(covers),
    )


def nodes_with_keyword_hit(need: str) -> list[str]:
    """match_keywords 出现在 need 里的节点（不含 persona / 评估器）。"""
    from gsuid_core.ai_core.agent_node import list_nodes

    blob = (need or "").strip().lower()
    if not blob:
        return []
    hits: list[str] = []
    for node in list_nodes():
        if node.source == "persona" or node.node_id == "capability_evaluator":
            continue
        for kw in node.match_keywords:
            k = (kw or "").strip().lower()
            if k and k in blob:
                hits.append(node.node_id)
                break
    return hits


async def _matched_capability_node_ids(need: str, *, limit: int = 5) -> list[str]:
    """关键词快路径 + 语义兜底 + 弱子串。返回 node_id，关键词命中排前。"""
    from gsuid_core.ai_core.agent_node import list_nodes
    from gsuid_core.ai_core.agent_node.registry import match_capability_node
    from gsuid_core.ai_core.agent_node.semantic_routing import semantic_match_nodes

    need_s = (need or "").strip()
    if not need_s:
        return []
    ids: list[str] = []
    seen: set[str] = set()

    def _push(node_id: str) -> None:
        if node_id in seen or len(ids) >= limit:
            return
        # 专用走 keyword/fold；占这里会挤掉通用档，且后面本来就会 skip。
        if agent_is_dedicated(node_id):
            return
        if _node_hit(node_id, 0.0) is None:
            return
        seen.add(node_id)
        ids.append(node_id)

    primary = match_capability_node(need_s)
    if primary:
        _push(primary)
    try:
        for node_id, _score in await semantic_match_nodes(need_s, limit=limit):
            _push(node_id)
    except Exception as e:
        logger.debug(t("log.ai.find_tools_semantic_route_fail", e=e))
    blob = need_s.lower()
    for node in list_nodes():
        if len(ids) >= limit:
            break
        if node.node_id in seen:
            continue
        if node.source == "persona" or node.node_id == "capability_evaluator":
            continue
        hit = False
        for kw in node.match_keywords:
            k = (kw or "").strip().lower()
            if k and k in blob:
                hit = True
                break
        if not hit:
            from gsuid_core.ai_core.agent_node.semantic_routing import aggregate_node_covers

            covers = aggregate_node_covers(node)
            hit = need_matches_node_text(
                need_s,
                when_to_use=node.when_to_use,
                display_name=node.display_name,
                keywords=tuple(node.match_keywords),
                covers=tuple(covers),
            )
        if hit:
            _push(node.node_id)
    return ids


def _offered_dedicated_hits(
    *,
    names: Sequence[str],
    need: str,
    exclusive: set[str],
) -> list[RankedHit]:
    """已暴露专用工具里真正对口的，并入分档；短 cover 不会在这里误伤。"""
    from gsuid_core.ai_core.register import find_tool_base

    hits: list[RankedHit] = []
    for name in names:
        tb = find_tool_base(name)
        if tb is None:
            continue
        covers = list(tb.covers)
        retrieval = tb.retrieval_text
        if not need_matches_tool_text(need, retrieval, covers):
            continue
        if (
            classify_callable_tool(
                name=tb.name,
                category=tb.category,
                plugin=tb.plugin,
                hide_from_main=tb.hide_from_main,
                exclusive=exclusive,
            )
            != "dedicated"
        ):
            continue
        hits.append(
            RankedHit(
                name=tb.name,
                label=tool_brief(covers=covers, description=tb.description),
                score=3000.0,
            )
        )
    return hits


async def visible_offered_names(ctx: RunContext[ToolContext], names: Sequence[str]) -> list[str]:
    """已加载名单去掉本步 visible_when 隐藏的（否则会诱导调用 Unknown tool）。"""
    from gsuid_core.ai_core.register import find_tool_base
    from gsuid_core.ai_core.agent_run.tools import _SCHED_CREATE_NAMES, _SCHED_MUTATE_NAMES

    out: list[str] = []
    for name in names:
        tb = find_tool_base(name)
        if tb is None:
            continue
        try:
            run_ctx = replace(ctx, tool_name=name, retry=0, max_retries=1)
        except TypeError:
            run_ctx = ctx
        try:
            tool_def = await tb.tool.prepare_tool_def(run_ctx)
        except Exception as e:
            logger.debug(t("log.ai.find_tools_prepare_treated_unavailable_fail", p0=name, e=e))
            tool_def = tb.tool
        if not tool_def:
            continue
        if name in _SCHED_CREATE_NAMES and not check_sched_create(ctx.deps)[0]:
            continue
        if name in _SCHED_MUTATE_NAMES and not check_sched_mutate(ctx.deps)[0]:
            continue
        out.append(name)
    return out


# 能力缺口登记（4.5）：find_tools 未命中时计数，供运维按「高频被求而缺失」
# 决定安装哪些插件/工具。纯进程内计数，不进用户可见通道、不做业务特判。
_CAPABILITY_GAP_COUNTS: dict[str, int] = {}


def _record_capability_gap(need: str) -> None:
    key = (need or "").strip()[:80]
    if not key:
        return
    _CAPABILITY_GAP_COUNTS[key] = _CAPABILITY_GAP_COUNTS.get(key, 0) + 1


def get_capability_gaps(limit: int = 20) -> list[tuple[str, int]]:
    """按次数降序返回 top-N 能力缺口（need, count），供 webconsole 展示。"""
    return sorted(_CAPABILITY_GAP_COUNTS.items(), key=lambda kv: kv[1], reverse=True)[:limit]


# 不声明 capability_domain（会被 L3 按族驻留带进闲聊轮）；category 必须为 meta：
# 落入 buildin 等保底分类会让渐进式暴露门控失效、加载的工具无人暴露（实测踩坑）。
@ai_tools(category="meta")
async def find_tools(
    ctx: RunContext[ToolContext],
    need: str,
) -> str:
    """列表没有的能力必须先调：用一句话描述所缺能力。未调用前禁止说做不到/没装。

    每次都会检索，不因列表里已有工具而跳过。已暴露且真正对口的专用工具并入回执。
    一次返回专用能力 / 专用工具 / 通用能力 / 通用工具；有专用项时禁止改用通用项。
    专用工具下一步可直接调；专用能力用 create_subagent(agent_profile=node_id)。

    Args:
        ctx: 工具执行上下文。
        need: 所缺能力的一句话描述。

    Returns:
        分档清单；对不上当没找到。
    """
    try:
        from gsuid_core.ai_core.register import find_tool_base
        from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY
        from gsuid_core.ai_core.agent_node.registry import owning_nodes_of_tools

        exclusive = set(ctx.deps.blocked_tool_names)
        offered_raw = ctx.deps.extra[EXPOSED_TOOLS_EXTRA_KEY] if EXPOSED_TOOLS_EXTRA_KEY in ctx.deps.extra else None
        offered: list[str] = [n for n in offered_raw if isinstance(n, str)] if isinstance(offered_raw, list) else []
        offered_set = set(offered)

        # 已加载命中不短路：短 cover 会误把列表里的工具当对口，真正缺的就搜不到。
        offered_hits = _offered_dedicated_hits(names=offered, need=need, exclusive=exclusive)
        visible_offered = await visible_offered_names(ctx, [h.name for h in offered_hits])
        visible_offered_set = set(visible_offered)
        offered_hits = [h for h in offered_hits if h.name in visible_offered_set]

        family_tools = await search_tools_by_domain(
            query=need, domain_limit=3, per_domain_limit=6, exclude_names=offered_set
        )
        from gsuid_core.ai_core.rag.tools import align_seeds_to_context_plugin
        from gsuid_core.ai_core.entity_index import ALIAS_PLUGIN_EXTRA_KEY

        _alias_plugin = ""
        if ALIAS_PLUGIN_EXTRA_KEY in ctx.deps.extra:
            _raw_plugin = ctx.deps.extra[ALIAS_PLUGIN_EXTRA_KEY]
            if isinstance(_raw_plugin, str):
                _alias_plugin = _raw_plugin
        if _alias_plugin:
            family_tools = await align_seeds_to_context_plugin(family_tools, _alias_plugin, need)
        dedicated_tools: list[RankedHit] = list(offered_hits)
        generic_tools: list[RankedHit] = []
        fold_names: list[str] = []
        hidden_names: list[str] = []
        for idx, tool in enumerate(family_tools):
            tb = find_tool_base(tool.name)
            if tb is None:
                continue
            covers = list(tb.covers)
            retrieval = tb.retrieval_text
            score = float(1000 - idx)
            tier = classify_callable_tool(
                name=tb.name,
                category=tb.category,
                plugin=tb.plugin,
                hide_from_main=tb.hide_from_main,
                exclusive=exclusive,
            )
            if tier == "fold":
                # 向量已召回；词面过滤会丢掉「对比表出成图」这类 exclusive
                fold_names.append(tool.name)
                continue
            if not need_matches_tool_text(need, retrieval, covers):
                continue
            try:
                run_ctx = replace(
                    ctx,
                    tool_name=tool.name,
                    retry=0,
                    max_retries=tool.max_retries if tool.max_retries is not None else 1,
                )
            except TypeError:
                run_ctx = ctx
            try:
                tool_def = await tool.prepare_tool_def(run_ctx)
            except Exception as e:
                logger.debug(t("log.ai.find_tools_prepare_treated_unavailable_fail", p0=tool.name, e=e))
                tool_def = None
            if not tool_def:
                hidden_names.append(tool.name)
                continue
            hit = RankedHit(
                name=tool.name,
                label=tool_brief(covers=covers, description=tb.description),
                score=score,
            )
            if tier == "dedicated":
                dedicated_tools.append(hit)
            elif tool.name not in offered_set:
                generic_tools.append(hit)

        if hidden_names:
            logger.info(
                t(
                    "log.ai.find_tools_matched_excluded",
                    p0=len(hidden_names),
                    hidden_names=hidden_names,
                )
            )

        dedicated_agents: list[RankedHit] = []
        generic_agents: list[RankedHit] = []
        owner_ids: list[str] = []
        if fold_names:
            owners = owning_nodes_of_tools(fold_names)
            for ids in owners.values():
                for node_id in ids:
                    if node_id not in owner_ids:
                        owner_ids.append(node_id)
        for idx, node_id in enumerate(owner_ids):
            if not agent_is_dedicated(node_id):
                continue
            if not _node_on_topic(need, node_id):
                continue
            hit = _node_hit(node_id, float(2000 - idx))
            if hit is not None:
                dedicated_agents.append(hit)

        for idx, node_id in enumerate(nodes_with_keyword_hit(need)):
            if not agent_is_dedicated(node_id):
                continue
            if not _node_on_topic(need, node_id):
                continue
            hit = _node_hit(node_id, float(1500 - idx))
            if hit is not None:
                dedicated_agents.append(hit)

        matched_ids = await _matched_capability_node_ids(need)
        for idx, node_id in enumerate(matched_ids):
            if agent_is_dedicated(node_id):
                continue
            hit = _node_hit(node_id, float(500 - idx))
            if hit is not None:
                generic_agents.append(hit)

        plan = build_find_tools_plan(
            dedicated_agents=dedicated_agents,
            dedicated_tools=dedicated_tools,
            generic_agents=generic_agents,
            generic_tools=generic_tools,
        )
        if plan.is_empty():
            _record_capability_gap(need)
            stale_empty = _record_find_tools_round(ctx.deps.extra, [])
            miss = f"⚠️ 未检索到与「{need}」相关的专用项。可换更具体的能力描述重试，或委派通用调研。"
            return f"{miss}\n{FIND_TOOLS_GAP_NOTE}" if stale_empty else miss

        loaded_names = plan.loadable_tool_names()
        ctx.deps.dynamic_tool_names.update(loaded_names)
        stale = _record_find_tools_round(ctx.deps.extra, plan.fingerprint())
        logger.info(
            t(
                "log.ai.find_tools_dynamically_requirement_load",
                p0=need[:40],
                p1=len(loaded_names),
                loaded_names=loaded_names,
            )
        )
        msg = format_find_tools_plan(plan)
        if stale:
            return f"{msg}\n{FIND_TOOLS_GAP_NOTE}"
        return msg

    except RuntimeError as e:
        logger.warning(t("log.ai.find_tools_feature_enabled", e=e))
        return "⚠️ 工具检索功能未启用，无法动态加载工具。"
    except Exception as e:
        logger.error(t("log.ai.find_tools_event", e=e))
        return f"⚠️ 工具加载失败: {str(e)}"


@ai_tools(category="meta")
async def capability_map(
    ctx: RunContext[ToolContext],
    scope: str = "all",
    filter: str = "",
) -> str:
    """列出我（及可委派代理）的全部能力目录（不含参数细节）。

    每行：工具名 — 覆盖场景一句话，按能力域分组。需要细节再用 find_tools 查具体工具。
    单次最多 60 行；超出请按 domain/plugin 过滤再查。

    Args:
        ctx: 工具执行上下文。
        scope: all / domain / plugin。
        filter: 按能力域或插件名过滤。
    """
    _ = ctx
    from gsuid_core.ai_core.register import get_all_tools, main_persona_roster_ok

    tools = get_all_tools()
    grouped: dict[str, list[str]] = {}
    needle = (filter or "").strip().lower()
    for _name, tb in tools.items():
        if tb is None or not main_persona_roster_ok(tb):
            continue
        domain = tb.capability_domain or tb.plugin or "其他"
        plugin = tb.plugin
        if scope == "domain" and needle and needle not in domain.lower():
            continue
        if scope == "plugin" and needle and needle not in plugin.lower():
            continue
        if scope == "all" and needle and needle not in f"{tb.name} {domain} {plugin}".lower():
            continue
        cover = ""
        if tb.covers:
            cover = tb.covers[0]
        elif tb.schema_brief:
            cover = tb.schema_brief.split("。", 1)[0]
        elif tb.description:
            cover = tb.description.split("\n", 1)[0][:40]
        grouped.setdefault(domain, []).append(f"{tb.name}：{cover}")
    if not grouped:
        return "目录为空。换 filter 或 scope 再查。"
    if scope == "all":
        lines = [f"【{domain}】 {len(rows)} 项" for domain, rows in sorted(grouped.items())]
        lines.append("展开用 scope=domain 加 filter。")
        return "\n".join(lines)
    lines: list[str] = []
    for domain in sorted(grouped):
        lines.append(f"【{domain}】")
        lines.extend(f"- {row}" for row in grouped[domain])
        if len(lines) >= 60:
            lines = lines[:60]
            lines.append("（超出 60 行，请用 scope=domain/plugin 加 filter 再查）")
            break
    return "\n".join(lines)


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
