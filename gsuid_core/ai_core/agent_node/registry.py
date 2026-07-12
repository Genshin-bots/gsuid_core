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
        logger.warning(t("🧩 [AgentNode] node_id 为空，已忽略"))
        return
    _NODES[node.node_id] = node
    logger.info(
        t("🧩 [AgentNode] 注册节点: {p0} ({p1}, source={p2})", p0=node.node_id, p1=node.display_name, p2=node.source)
    )


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


def resolve_node(hint: str, default: str = "research_agent") -> str:
    """自然语言 hint → node_id（用句柄不用 ID，原 resolve_profile 语义）。

    1. hint 就是已注册 node_id（含 persona 投影）→ 直接返回；
    2. 命中某注册表节点的 match_keywords → 返回该 node_id（按注册序首个命中）；
    3. 都不命中 → 回退 default（default 不存在时回退首个注册节点）。
    """
    h = (hint or "").strip().lower()
    if not h:
        return default if default in _NODES else next(iter(_NODES), "")
    if h in _NODES or get_persona_node(h) is not None:
        return h
    for node in _NODES.values():
        if any(kw.lower() in h for kw in node.match_keywords):
            return node.node_id
    return default if default in _NODES else next(iter(_NODES), "")
