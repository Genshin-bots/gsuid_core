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

FIND_TOOLS_LOADED_KEY = "find_tools_last_loaded"
FIND_TOOLS_GAP_NOTE = (
    "（系统：连续检索未暴露新工具。可用 capability_map 查看全目录后再决定；"
    "用角色短句说明做不到；禁止再 find_tools；禁止念工具名或叙述装载过程。）"
)


def _need_matches_tool_text(need: str, retrieval_text: str, covers: list[str]) -> bool:
    """单向命中：need 出现在 retrieval，或 cover 出现在 need。禁止互为子串双向。"""
    n = (need or "").strip().lower()
    hay = (retrieval_text or "").strip().lower()
    if not n:
        return False
    if hay and n in hay:
        return True
    for c in covers:
        cl = (c or "").strip().lower()
        if cl and cl in n:
            return True
    tokens = [t for t in n.replace("，", " ").replace(",", " ").split() if len(t) >= 2]
    if not tokens or not hay:
        return False
    hits = sum(1 for t in tokens if t in hay)
    return hits >= max(1, (len(tokens) + 1) // 2)


def _record_find_tools_round(extra: dict[str, Any], loaded: list[str]) -> bool:
    """记下本轮暴露名。返回 True 表示相对上一轮没有新名字。"""
    from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY

    had_prev = FIND_TOOLS_LOADED_KEY in extra
    prev_raw = extra[FIND_TOOLS_LOADED_KEY] if had_prev else None
    prev: set[str] = set(prev_raw) if isinstance(prev_raw, list) else set()
    extra[FIND_TOOLS_LOADED_KEY] = list(loaded)
    exposed_raw = extra[EXPOSED_TOOLS_EXTRA_KEY] if EXPOSED_TOOLS_EXTRA_KEY in extra else None
    if isinstance(exposed_raw, list):
        for n in loaded:
            if n not in exposed_raw:
                exposed_raw.append(n)
    else:
        extra[EXPOSED_TOOLS_EXTRA_KEY] = list(loaded)
    if not had_prev:
        return False
    return set(loaded) <= prev


def _format_node_line(node_id: str) -> Optional[str]:
    """节点展示行：`node_id`（显示名）：when_to_use；节点不存在返回 None。"""
    from gsuid_core.ai_core.agent_node import get_node

    node = get_node(node_id)
    if node is None:
        return None
    when = (node.when_to_use or "").strip() or node.display_name
    return f"- `{node.node_id}`（{node.display_name}）：{when}"


async def _capability_agent_lines(need: str, *, limit: int = 5) -> list[str]:
    """按 need 匹配可委派能力代理，返回展示行（关键词快路径 + 语义兜底）。

    关键词表是枚举式的、必有洞；语义匹配（节点检索空间）兜住枚举之外的表述。
    两路合并去重，关键词命中排前。
    """
    from gsuid_core.ai_core.agent_node import list_nodes
    from gsuid_core.ai_core.agent_node.registry import match_capability_node
    from gsuid_core.ai_core.agent_node.semantic_routing import semantic_match_nodes

    need_s = (need or "").strip()
    if not need_s:
        return []
    lines: list[str] = []
    seen: set[str] = set()

    def _push(node_id: str) -> None:
        if node_id in seen or len(lines) >= limit:
            return
        line = _format_node_line(node_id)
        if line is None:
            return
        seen.add(node_id)
        lines.append(line)

    # 1) 关键词快路径：整句最长关键词命中的主节点
    primary = match_capability_node(need_s)
    if primary:
        _push(primary)
    # 2) 语义兜底：关键词没覆盖的说法（跨领域新词）由向量空间接住
    try:
        for node_id, _score in await semantic_match_nodes(need_s, limit=limit):
            _push(node_id)
    except Exception as e:
        logger.debug(t("log.ai.find_tools_semantic_route_fail", e=e))
    # 3) 注册表弱匹配补全（保留原有 token 子串逻辑，覆盖节点自述里的词）
    blob = need_s.lower()
    for node in list_nodes():
        if len(lines) >= limit:
            break
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
        if hit:
            _push(node.node_id)
    return lines


def _delegation_directive(lines: list[str]) -> str:
    """把候选节点行组装成委派指引文本。"""
    return '请用 create_subagent(agent_profile="<node_id>", task=...) 委派给下列能力代理：\n' + "\n".join(lines)


def _format_already_loaded(names: Sequence[str]) -> str:
    listing = "\n".join(f"- {name}" for name in names)
    return f"✅ 当前列表已有对应能力族工具，直接调用，不要委派：\n{listing}"


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
    """当前列表没有的能力，用一句话描述后调用。命中工具下一步可直接调。

    Args:
        ctx: 工具执行上下文。
        need: 所缺能力的一句话描述。

    Returns:
        委派指令或加载清单；对不上当没找到。
    """
    try:
        from gsuid_core.ai_core.register import find_tool_base
        from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY

        offered_raw = ctx.deps.extra[EXPOSED_TOOLS_EXTRA_KEY] if EXPOSED_TOOLS_EXTRA_KEY in ctx.deps.extra else None
        offered: list[str] = [n for n in offered_raw if isinstance(n, str)] if isinstance(offered_raw, list) else []
        if offered:
            loaded_hits: list[str] = []
            for name in offered:
                tb = find_tool_base(name)
                covers = list(tb.covers) if tb is not None else []
                retrieval = tb.retrieval_text if tb is not None else name
                if _need_matches_tool_text(need, retrieval, covers) and name not in loaded_hits:
                    loaded_hits.append(name)
            loaded_hits = await visible_offered_names(ctx, loaded_hits)
            if loaded_hits:
                stale_l = _record_find_tools_round(ctx.deps.extra, loaded_hits)
                msg_l = _format_already_loaded(loaded_hits)
                return f"{msg_l}\n{FIND_TOOLS_GAP_NOTE}" if stale_l else msg_l

        node_lines = await _capability_agent_lines(need)
        if node_lines:
            stale_n = _record_find_tools_round(ctx.deps.extra, [])
            msg = _delegation_directive(node_lines)
            return f"{msg}\n{FIND_TOOLS_GAP_NOTE}" if stale_n else msg

        family_tools = await search_tools_by_domain(query=need, domain_limit=3, per_domain_limit=6)
        related: list[Any] = []
        for tool in family_tools:
            tb = find_tool_base(tool.name)
            covers = list(tb.covers) if tb is not None else []
            retrieval = tb.retrieval_text if tb is not None else tool.name
            if _need_matches_tool_text(need, retrieval, covers):
                related.append(tool)
        family_tools = related
        if not family_tools:
            _record_capability_gap(need)
            stale = _record_find_tools_round(ctx.deps.extra, [])
            miss = (
                f"⚠️ 未检索到与「{need}」相关的工具。可换更具体的能力描述重试一次；"
                "若确实没有该能力，如实说明做不到，禁止编造。"
            )
            return f"{miss}\n{FIND_TOOLS_GAP_NOTE}" if stale else miss

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
        blocked_hit_names = [n for n in loaded_names if n in blocked] if blocked else []
        if blocked:
            loaded_names = [n for n in loaded_names if n not in blocked]

        if not loaded_names:
            _record_capability_gap(need)
            stale_empty = _record_find_tools_round(ctx.deps.extra, [])
            # 命中但全被 exclusive 剥离：工具真实存在、归能力代理专属——明确指路委派，
            # 不再谎称"没有找到"（旧同文案把模型推向 web_search 顶替，见 2026-08-11 归因）。
            if blocked_hit_names:
                from gsuid_core.ai_core.agent_node.registry import match_capability_node, owning_nodes_of_tools

                owners = owning_nodes_of_tools(blocked_hit_names)
                owner_ids: list[str] = []
                for ids in owners.values():
                    for node_id in ids:
                        if node_id not in owner_ids:
                            owner_ids.append(node_id)
                recognized = [nid for nid in owner_ids if match_capability_node(need) == nid]
                lines = [line for line in map(_format_node_line, recognized) if line]
                if lines:
                    locked = (
                        "🔒 该类工具为能力代理专属，不在主人格手里直接装配（这是设计，不是缺失）。\n"
                        + _delegation_directive(lines)
                    )
                    return f"{locked}\n{FIND_TOOLS_GAP_NOTE}" if stale_empty else locked
            # 全被 visible_when 隐藏：维持不泄露隐藏工具存在，但给出语义委派兜底。
            agent_lines = await _capability_agent_lines(need)
            if agent_lines:
                hidden = "🔎 未检索到当前场景可直接加载的工具，但该能力可能由能力代理持有。\n" + _delegation_directive(
                    agent_lines
                )
                return f"{hidden}\n{FIND_TOOLS_GAP_NOTE}" if stale_empty else hidden
            from gsuid_core.ai_core.register import format_capability_family_overview

            fam = format_capability_family_overview(max_families=3, max_chars=400)
            miss2 = (
                f"⚠️ 未检索到与「{need}」相关的工具。可换更具体的能力描述重试一次；"
                "若确实没有该能力，如实说明做不到，禁止编造。"
            )
            if fam:
                miss2 = f"{miss2}\n{fam}"
            return f"{miss2}\n{FIND_TOOLS_GAP_NOTE}" if stale_empty else miss2

        ctx.deps.dynamic_tool_names.update(loaded_names)
        stale = _record_find_tools_round(ctx.deps.extra, loaded_names)

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
        parts.append("若对不上需求，可换一句描述再 find_tools。")
        if stale:
            parts.append(FIND_TOOLS_GAP_NOTE)
        return "\n".join(parts)

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
