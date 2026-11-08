"""MCP 服务 → 能力代理节点（AgentNode, source="mcp"）的自动画像。

每个启用了 ``register_as_ai_tools`` 的 MCP 配置对应一个 ``mcp_<config_id>`` 节点：
- 工具名取**注册表真值**（实际挂上的 ``mcp_<server>_<tool>`` 名），不可被覆盖；
- 描述/关键词按优先级取：用户覆盖层 > 中文 description > generated_profile > 英文/拼接兜底；
- 用户编辑只落覆盖层（``capability_agents.persistence`` 的 mcp/ 子目录），节点本体不落盘；
- 节点不进人格花名册（in_roster=False），但可被语义路由 / find_tools / 委派命中。
"""

import re
import hashlib
from typing import Any, List, Literal, Optional

from gsuid_core.ai_core.agent_node import TASK_BASICS_PACK, AgentNode
from gsuid_core.ai_core.mcp.config_manager import MCPConfig, mcp_config_manager
from gsuid_core.ai_core.capability_agents.persistence import (
    MCPNodeOverlay,
    load_mcp_overlay,
)

MCP_NODE_PREFIX = "mcp_"

_MCP_NODE_PROMPT = """你是 {name} 服务的专职执行代理，擅长把自然语言任务翻译成该服务的 MCP 工具调用。

工作方式：
1. 先理解主人格交给你的任务目标，必要时复述确认理解（用交付摘要，不直接发消息）。
2. 只能使用本服务提供的 MCP 工具完成任务；缺参数时用工具描述里的说明补齐，或把缺口写进交付摘要让上游决定。
3. 多次独立只读调用可用 run_code 批量汇总，减少往返。
4. 工具报错不要反复重试同一参数；换参数或如实把错误带回去。"""

# 拆词时过滤的通用动词（命中它们对路由没有信息量）
_GENERIC_VERBS = frozenset(
    {
        "get",
        "list",
        "create",
        "update",
        "delete",
        "remove",
        "search",
        "query",
        "read",
        "write",
        "set",
        "fetch",
        "add",
        "new",
        "do",
        "run",
    }
)


def mcp_node_id(config_id: str) -> str:
    return f"{MCP_NODE_PREFIX}{config_id}"


def has_cjk(text: str) -> bool:
    """是否含至少 4 个 CJK 字符（判定"中文介绍可用"的阈值）。"""
    return sum(1 for ch in text if "一" <= ch <= "鿿") >= 4


def first_line(text: str) -> str:
    return (text or "").strip().split("\n", 1)[0].strip()


def first_sentence(text: str) -> str:
    """取首个完整分句；句内逗号/冒号归一为空格，保证可直接进花名册一行。"""
    t = (text or "").strip()
    if not t:
        return ""
    m = re.search(r"[。！？!?\n]", t)
    head = t[: m.start()] if m else t
    head = re.sub(r"[，,：:；;、]+", " ", head)
    return re.sub(r"\s+", " ", head).strip()


def _description_rest(text: str) -> str:
    t = (text or "").strip()
    m = re.search(r"[。！？!?\n]", t)
    return t[m.end() :].strip() if m else ""


def effective_description(config: MCPConfig) -> str:
    """描述优先级：中文 description > generated_profile > 英文 description。"""
    desc = (config.description or "").strip()
    if has_cjk(desc):
        return desc
    profile = config.generated_profile
    if profile is not None and has_cjk(profile["description"]):
        return profile["description"].strip()
    return desc


def tools_hash(config: MCPConfig) -> str:
    """工具清单指纹：名字 + 首行描述。变化即触发重新生成。"""
    items = sorted((tool.name, first_line(tool.description)) for tool in config.tools)
    return hashlib.sha1(repr(items).encode("utf-8")).hexdigest()[:12]


def split_ident(name: str) -> List[str]:
    """把标识符（snake_case / kebab / 驼峰）拆成小写词，滤掉 <3 字符碎片。"""
    tokens = re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", name or "")
    return [t.lower() for t in tokens if len(t) >= 3]


def gen_when_to_use(config: MCPConfig) -> str:
    """自动生成 when_to_use：能力短语 + 服务名 + 工具能力列表 + 描述余量。"""
    desc = effective_description(config)
    head = first_sentence(desc)
    if not head:
        cn_name = config.market_meta.get("chinese_name", "")
        head = f"{cn_name}相关能力" if cn_name else f"调用 {config.name} 的 MCP 工具"
    text = f"{head} · {config.name} MCP"
    if config.tools:
        text += "。能力：" + "、".join(tool.name for tool in config.tools[:8])
    rest = _description_rest(desc)
    if rest:
        text += "。" + rest
    return text[:300]


def gen_match_keywords(config_id: str, config: MCPConfig) -> List[str]:
    """自动生成路由关键词：服务名/中文名/拆词/类目/生成关键词/工具名拆词。"""
    out: List[str] = []

    def push(keyword: str) -> None:
        kw = (keyword or "").strip()
        if len(kw) >= 2 and kw not in out:
            out.append(kw)

    push(config.name)
    push(config.market_meta.get("chinese_name", ""))
    for token in split_ident(config.name) + split_ident(config_id):
        push(token)
    for category in config.market_meta.get("categories", []):
        push(category)
    profile = config.generated_profile
    if profile is not None:
        for keyword in profile["keywords"]:
            push(keyword)
    for tool in config.tools:
        for token in split_ident(tool.name):
            if token not in _GENERIC_VERBS:
                push(token)
    return out[:12]


def mcp_tool_covers(config: Optional[MCPConfig], tool_description: str) -> List[str]:
    """单个 MCP 工具的检索短语：服务描述短语（≤3）+ 工具英文首行。"""
    covers: List[str] = []
    if config is not None:
        for phrase in re.split(r"[，,、；;。\n]", effective_description(config)):
            p = phrase.strip()
            if len(p) >= 3 and p not in covers:
                covers.append(p)
            if len(covers) >= 3:
                break
    line = first_line(tool_description)[:40]
    if line and line not in covers:
        covers.append(line)
    return covers


def build_mcp_node_base(config_id: str, config: MCPConfig, registered_names: List[str]) -> AgentNode:
    """不含用户覆盖层的自动生成节点。"""
    display = config.market_meta.get("chinese_name") or config.name
    return AgentNode(
        node_id=mcp_node_id(config_id),
        display_name=display,
        prompt=_MCP_NODE_PROMPT.format(name=display),
        prompt_style="plain",
        when_to_use=gen_when_to_use(config),
        match_keywords=gen_match_keywords(config_id, config),
        tool_packs=[TASK_BASICS_PACK],
        tool_names=list(registered_names),
        master_only=any(config.get_tool_required_pm(t.name) <= 1 for t in config.tools),
        source="mcp",
        plugin="",
        in_roster=False,
    )


def apply_mcp_overlay(node: AgentNode, overlay: MCPNodeOverlay) -> AgentNode:
    """把用户覆盖层合到自动生成节点上（tool_names / source / in_roster 不可覆盖）。"""
    if "display_name" in overlay:
        node.display_name = overlay["display_name"]
    if "when_to_use" in overlay:
        node.when_to_use = overlay["when_to_use"]
    if "match_keywords" in overlay:
        node.match_keywords = list(overlay["match_keywords"])
    if "prompt" in overlay:
        node.prompt = overlay["prompt"]
    if "boundary_override" in overlay:
        node.boundary_override = overlay["boundary_override"]
    if "master_only" in overlay:
        node.master_only = overlay["master_only"]
    if "tool_packs" in overlay:
        node.tool_packs = list(overlay["tool_packs"])
    return node


def build_mcp_node(config_id: str, config: MCPConfig, registered_names: List[str]) -> AgentNode:
    """自动生成 + 覆盖层合并后的最终节点。"""
    node = build_mcp_node_base(config_id, config, registered_names)
    return apply_mcp_overlay(node, load_mcp_overlay(node.node_id))


def _overlay_str_field(
    out: MCPNodeOverlay,
    key: "Literal['display_name', 'when_to_use', 'prompt', 'boundary_override']",
    base_value: str,
    provided: dict[str, Any],
) -> None:
    if key not in provided:
        return
    value = provided[key]
    if isinstance(value, str) and value != base_value:
        out[key] = value
    elif key in out:
        del out[key]


def _overlay_list_field(
    out: MCPNodeOverlay,
    key: "Literal['match_keywords', 'tool_packs']",
    base_value: List[str],
    provided: dict[str, Any],
) -> None:
    if key not in provided:
        return
    value = provided[key]
    items = [str(x) for x in value] if isinstance(value, list) else None
    if items is not None and items != list(base_value):
        out[key] = items
    elif key in out:
        del out[key]


def compute_mcp_overlay(
    current: MCPNodeOverlay,
    base: AgentNode,
    provided: dict[str, Any],
) -> MCPNodeOverlay:
    """合并覆盖层：已提供字段与自动生成基线做差，未提供字段保留旧覆盖。

    与基线一致的值从覆盖层剔除；tool_names / source / in_roster 不可覆盖。
    """
    out = MCPNodeOverlay(current)
    _overlay_str_field(out, "display_name", base.display_name, provided)
    _overlay_str_field(out, "when_to_use", base.when_to_use, provided)
    _overlay_str_field(out, "prompt", base.prompt, provided)
    _overlay_str_field(out, "boundary_override", base.boundary_override, provided)
    _overlay_list_field(out, "match_keywords", base.match_keywords, provided)
    _overlay_list_field(out, "tool_packs", base.tool_packs, provided)
    if "master_only" in provided:
        value = provided["master_only"]
        if isinstance(value, bool) and value != base.master_only:
            out["master_only"] = value
        elif "master_only" in out:
            del out["master_only"]
    return out


def mcp_desc_origin(node_id: str) -> str:
    """when_to_use 文案来源标注：user > market > generated > auto。"""
    overlay = load_mcp_overlay(node_id)
    if "when_to_use" in overlay:
        return "user"
    config_id = node_id[len(MCP_NODE_PREFIX) :] if node_id.startswith(MCP_NODE_PREFIX) else node_id
    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return "auto"
    if config.market_meta:
        return "market"
    if config.generated_profile is not None and has_cjk(config.generated_profile["description"]):
        return "generated"
    if config.description.strip():
        return "user"
    return "auto"
