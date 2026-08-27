"""AgentNode 统一注册表。

内存注册表持有全部**非 persona** 节点（builtin / plugin / user，进程启动时由
``profiles.register_builtin_nodes`` + 插件启动钩子 + ``persistence.load_user_nodes``
重建）；persona 节点由 ``persona_proj`` 按目录投影、随取随刷，不占注册表写路径。
两类节点经 ``get_node`` / ``list_nodes`` 对外呈现为同一张表。
"""

import inspect
from typing import Dict, List, Optional
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .models import AgentNode
from .persona_proj import get_persona_node, list_persona_nodes

# node_id -> AgentNode（保持插入序：resolve_node 的关键词命中顺序依赖它）
_NODES: Dict[str, AgentNode] = {}

# 推断 plugin 时跳过注册栈自身帧（落到真实调用方）
_PLUGIN_INFER_SKIP_PREFIXES = (
    "gsuid_core.ai_core.agent_node",
    "gsuid_core.ai_core.capability_agents.registry",
    "gsuid_core.ai_core.capability_agents.persistence",
    "gsuid_core.ai_core.capability_agents.profiles",
    "gsuid_core.server",
)

# 插件磁盘根目录名（与 server.PLUGIN_PATH / BUILDIN_PLUGIN_PATH 一致）
_PLUGIN_DIR_MARKERS = frozenset({"plugins", "buildin_plugins"})


def _plugin_name_from_path(path: str) -> str:
    """从源码路径提取插件目录名：.../plugins/<Name>/... → Name。"""
    if not path:
        return ""
    try:
        parts = Path(path).resolve().parts
    except OSError:
        parts = Path(path).parts
    for i, part in enumerate(parts):
        if part not in _PLUGIN_DIR_MARKERS:
            continue
        if i + 1 >= len(parts):
            continue
        name = parts[i + 1]
        if name.endswith(".py"):
            name = name[:-3]
        if name and not name.startswith("_"):
            return name
    return ""


def _plugin_name_from_module(mod: str) -> str:
    """仅当模块路径含 plugins/buildin_plugins 段时取插件名；绝不把 core 当插件。"""
    if not mod:
        return ""
    parts = mod.split(".")
    for marker in ("plugins", "buildin_plugins"):
        if marker not in parts:
            continue
        i = parts.index(marker)
        if i + 1 < len(parts) and parts[i + 1]:
            return parts[i + 1]
    return ""


def _infer_registering_plugin() -> str:
    """从调用栈推断注册方插件名。

    插件常以 ``SayuStock.xxx`` 顶层包名加载（sys.path 指到 plugins/ 父级），
    模块名不含 ``plugins`` 段；必须以 **源码路径** 中的 ``plugins/<Name>`` 为准，
    禁止用 ``_get_plugin_name_from_module`` 把任意 gsuid_core 帧判成 core。
    """
    for fr in inspect.stack()[1:24]:
        mod = str(fr.frame.f_globals["__name__"]) if "__name__" in fr.frame.f_globals else ""
        if mod and any(mod == p or mod.startswith(p + ".") for p in _PLUGIN_INFER_SKIP_PREFIXES):
            continue
        # 1) 路径优先（nest 插件 / 顶层包名加载都能命中）
        name = _plugin_name_from_path(getattr(fr, "filename", "") or "")
        if name:
            return name
        # 2) 模块路径含 plugins 段时
        name = _plugin_name_from_module(mod)
        if name:
            return name
    return ""


def _fill_plugin_if_empty(node: AgentNode) -> None:
    """补全 node.plugin：builtin→core；plugin→栈/路径推断；user/persona→空。"""
    if (node.plugin or "").strip():
        return
    if node.source == "builtin":
        node.plugin = "core"
        return
    if node.source == "plugin":
        inferred = _infer_registering_plugin()
        node.plugin = inferred if inferred else "unknown"
        return
    # user / persona：无宿主插件
    node.plugin = ""


def register_agent_node(node: AgentNode) -> None:
    """注册一个节点。同 node_id 后写覆盖前写（插件可覆盖内置）。"""
    if not node.node_id:
        logger.warning(t("log.ai.agentnode_node_id_empty_ignore"))
        return
    _fill_plugin_if_empty(node)
    _NODES[node.node_id] = node
    logger.info(t("log.ai.agentnode_registered_node_source", p0=node.node_id, p1=node.display_name, p2=node.source))


def unregister_agent_node(node_id: str) -> bool:
    """移除一个非 persona 节点；返回是否真的删了一项。"""
    if node_id in _NODES:
        _NODES.pop(node_id)
        return True
    return False


def get_node(node_id: str) -> Optional[AgentNode]:
    """按 node_id 取节点：注册表优先，未命中回落 persona 投影。"""
    if node_id in _NODES:
        return _NODES[node_id]
    return get_persona_node(node_id)


def list_nodes(include_persona: bool = False) -> List[AgentNode]:
    """列出节点。默认只列注册表（委派 / webconsole 画像页语义）；
    ``include_persona=True`` 时并入 persona 投影节点（编排全景视图）。"""
    nodes = list(_NODES.values())
    if include_persona:
        persona_nodes = list_persona_nodes()
        seen = {n.node_id for n in nodes}
        nodes.extend(n for name, n in persona_nodes.items() if name not in seen)
    return nodes


def format_capability_roster() -> str:
    """可委派短花名册：每节点一行 node_id + when_to_use。covers 不进 system。"""
    nodes: list[AgentNode] = [n for n in list_nodes() if n.source != "persona" and n.node_id != "capability_evaluator"]
    from gsuid_core.ai_core.configs.ai_config import ai_config

    cap = int(ai_config.get_config("capability_roster_max").data)
    lines: list[str] = []
    for node in nodes:
        when = (node.when_to_use or "").strip()
        if not when:
            from gsuid_core.logger import logger

            logger.warning(t("log.agent.capability_node_missing_when", node=node.node_id))
            when = "专业任务"
        prefix = f"- `{node.node_id}`："
        line = prefix + when
        if cap > 0 and len(line) > cap:
            line = prefix if len(prefix) >= cap else line[: cap - 1] + "…"
        lines.append(line)
    if not lines:
        return ""
    return (
        "（可用能力代理——须 "
        '`create_subagent(agent_profile="<node_id>", task=...)` 委派，'
        "agent_profile 只填下列 node_id，禁止自造名字：\n"
        + "\n".join(lines)
        + "\n提醒的增删改查在主会话；禁止声称没有对应工具。）"
    )


def owning_nodes_of_tools(tool_names: List[str]) -> Dict[str, List[str]]:
    """工具名 → 持有它的节点 node_id 列表（按节点 tool_names 白名单声明）。

    exclusive 剥离后 find_tools 用它回答「该去委派谁」：被剥离的工具归属哪个
    能力节点，就提示模型 create_subagent 到哪个节点，而不是谎称"没有工具"。
    """
    wanted = {n for n in tool_names if n}
    owners: Dict[str, List[str]] = {}
    if not wanted:
        return owners
    for node in _NODES.values():
        if node.source == "persona" or node.node_id == "capability_evaluator":
            continue
        hit = wanted.intersection(node.tool_names)
        for tool_name in hit:
            owners.setdefault(tool_name, []).append(node.node_id)
    return owners


def match_capability_node(hint: str) -> str:
    """自然语言 hint → node_id；无命中返回空串（不回退默认画像）。

    1. hint 就是已注册 node_id → 直接返回；
    2. 命中 match_keywords 或 when_to_use / display_name 子串 → 最长关键词优先；
    3. 都不命中 → ``""``。
    """
    h = (hint or "").strip().lower()
    if not h:
        return ""
    if h in _NODES:
        return h
    if get_persona_node(h) is not None:
        return h
    best_id = ""
    best_score = 0
    for node in _NODES.values():
        if node.source == "persona" or node.node_id == "capability_evaluator":
            continue
        for kw in node.match_keywords:
            k = (kw or "").strip().lower()
            if not k or k not in h:
                continue
            score = len(k)
            if score > best_score:
                best_score = score
                best_id = node.node_id
        # 弱匹配：when_to_use / display_name / node_id 出现在 hint 中（低于关键词分）
        weak_blob = f"{node.node_id} {node.display_name} {node.when_to_use}".lower()
        for token in h.replace("，", " ").replace(",", " ").split():
            if len(token) < 2:
                continue
            if token in weak_blob:
                score = min(len(token), 8)
                if score > best_score:
                    best_score = score
                    best_id = node.node_id
    return best_id


def resolve_node(hint: str, default: str = "") -> str:
    """自然语言 hint → node_id。非空未知不静默落到某个专职节点。

    1. hint 就是已注册 node_id（含 persona 投影）→ 直接返回；
    2. 命中 match_keywords → 最长关键词优先；
    3. 空 hint → default（若已注册）；非空未命中 → 空串。
    """
    matched = match_capability_node(hint)
    if matched:
        return matched
    h = (hint or "").strip()
    if not h:
        if default and default in _NODES:
            return default
        return ""
    return ""
