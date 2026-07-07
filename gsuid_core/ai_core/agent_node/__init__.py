"""AgentNode 统一节点层：Persona 与能力代理的同构定义。

- ``models``       : ``AgentNode`` dataclass + 交付边界 / 提示词叠加层。
- ``registry``     : 统一注册表 + ``resolve_node``（自然语言 → node_id）。
- ``tool_packs``   : 工具能力族注册 / 解析（``dynamic`` / ``task_basics`` / 域族）。
- ``persona_proj`` : persona 目录 → 只读投影节点（磁盘布局零迁移）。

运行模式不进 schema：session-mode（入口，多轮 + 记忆 + 巡检）与 task-mode
（被派活，一次循环 + 交付边界）由实例化方决定。
"""

from .models import (
    DELIVERY_BOUNDARY,
    AgentNode,
    compose_task_prompt,
    compose_plain_session_prompt,
)
from .registry import (
    get_node,
    list_nodes,
    resolve_node,
    register_agent_node,
    unregister_agent_node,
)
from .tool_packs import (
    DYNAMIC_PACK,
    TASK_BASICS_PACK,
    has_dynamic_pack,
    register_tool_pack,
    resolve_pack_tool_names,
)
from .persona_proj import get_persona_node, list_persona_nodes

__all__ = [
    "AgentNode",
    "DELIVERY_BOUNDARY",
    "compose_task_prompt",
    "compose_plain_session_prompt",
    "register_agent_node",
    "unregister_agent_node",
    "get_node",
    "list_nodes",
    "resolve_node",
    "get_persona_node",
    "list_persona_nodes",
    "DYNAMIC_PACK",
    "TASK_BASICS_PACK",
    "register_tool_pack",
    "resolve_pack_tool_names",
    "has_dynamic_pack",
]
