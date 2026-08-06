"""AgentNode 统一注册表。

内存注册表持有全部**非 persona** 节点（builtin / plugin / user，进程启动时由
``profiles.register_builtin_nodes`` + 插件启动钩子 + ``persistence.load_user_nodes``
重建）；persona 节点由 ``persona_proj`` 按目录投影、随取随刷，不占注册表写路径。
两类节点经 ``get_node`` / ``list_nodes`` 对外呈现为同一张表。
"""

from typing import Dict, List, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .models import AgentNode
from .persona_proj import get_persona_node, list_persona_nodes

# node_id -> AgentNode（保持插入序：resolve_node 的关键词命中顺序依赖它）
_NODES: Dict[str, AgentNode] = {}


def register_agent_node(node: AgentNode) -> None:
    """注册一个节点。同 node_id 后写覆盖前写（插件可覆盖内置）。"""
    if not node.node_id:
        logger.warning(t("log.ai.agentnode_node_id_empty_ignore"))
        return
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
    """可委派能力代理清单（供 system_prompt 固化，避免每轮 user 侧重复注入）。"""
    lines: list[str] = []
    for node in list_nodes():
        if node.source == "persona" or node.node_id == "capability_evaluator":
            continue
        when = (node.when_to_use or "").strip() or "专业任务"
        lines.append(f"- `{node.node_id}`（{node.display_name}）：{when}")
    if not lines:
        return ""
    return (
        "（可用能力代理——B 类组合/分析/推荐任务必须 "
        '`create_subagent(agent_profile="<node_id>", task=...)` 委派，'
        "agent_profile 只填下列 node_id，禁止自造名字：\n" + "\n".join(lines) + "）"
    )


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


def resolve_node(hint: str, default: str = "research_agent") -> str:
    """自然语言 hint → node_id（用句柄不用 ID，原 resolve_profile 语义）。

    1. hint 就是已注册 node_id（含 persona 投影）→ 直接返回；
    2. 命中 match_keywords → 取**最长关键词**命中的节点（更具体优先；
       同分时保留注册序更靠前的节点，与旧「首个命中」一致）；
    3. 都不命中 → 回退 default（default 不存在时回退首个注册节点）。

    例：``分析并出对比表`` 同时命中 research「分析」与 render「对比表」→
    因「对比表」更长，选 ``render_agent``。
    """
    matched = match_capability_node(hint)
    if matched:
        return matched
    h = (hint or "").strip().lower()
    if not h:
        return default if default in _NODES else next(iter(_NODES), "")
    return default if default in _NODES else next(iter(_NODES), "")
