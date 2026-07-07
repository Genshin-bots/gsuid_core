"""工具能力族（tool packs）注册与解析。

一个 pack = 一组可整体挂载到 AgentNode 的工具。三类来源：

- ``dynamic``：**标记族**——运行时五层自动装配（保底池 + 状态池 + 驻留池 +
  语境池 + 向量检索 + find_tools 渐进暴露），由 gs_agent 逐轮执行，不在此解析。
- 静态族：``register_tool_pack`` 显式注册的名单（框架内置 ``task_basics``）。
- capability_domain 族：pack 名命中某 ``@ai_tools(capability_domain=...)`` 时
  整族解析（如 "定时任务"）。
"""

from typing import Dict, List

from gsuid_core.logger import logger

DYNAMIC_PACK = "dynamic"
TASK_BASICS_PACK = "task_basics"

# 任务基础族：能力代理 task-mode 的常备工具（原 runner._ALWAYS_TOOLS）。
# 刻意不含破坏性的 record_delete——需要删行的节点在 tool_names 显式声明。
_TASK_BASICS_TOOLS: List[str] = [
    "artifact_put",
    "artifact_get",
    "artifact_list",
    "state_set",
    "state_get",
    "state_append",
    "state_list",
    "record_put",
    "record_get",
    "record_list",
    "record_append",
    "record_update",
    "record_summary",
    "search_knowledge",
    "web_search_tool",
    "web_fetch_tool",
]

_STATIC_PACKS: Dict[str, List[str]] = {TASK_BASICS_PACK: list(_TASK_BASICS_TOOLS)}


def register_tool_pack(pack_name: str, tool_names: List[str]) -> None:
    """注册 / 覆盖一个静态能力族（插件可注册自己的族，同名后写覆盖）。"""
    if not pack_name or pack_name == DYNAMIC_PACK:
        logger.warning(f"🧩 [ToolPack] 非法能力族名: {pack_name!r}，已忽略")
        return
    _STATIC_PACKS[pack_name] = list(tool_names)
    logger.info(f"🧩 [ToolPack] 注册能力族: {pack_name}（{len(tool_names)} 个工具）")


def has_dynamic_pack(packs: List[str]) -> bool:
    """节点是否声明了 ``dynamic``（五层自动装配）能力族。"""
    return DYNAMIC_PACK in packs


def resolve_pack_tool_names(packs: List[str]) -> List[str]:
    """把静态能力族解析为去重后的工具名列表（``dynamic`` 跳过，由 gs_agent 处理）。

    解析顺序：静态注册族 > capability_domain 族；未命中的 pack 记 warning 并跳过。
    """
    from gsuid_core.ai_core.register import get_tools_by_capability_domain

    names: List[str] = []
    for pack in packs:
        if pack == DYNAMIC_PACK:
            continue
        if pack in _STATIC_PACKS:
            names.extend(_STATIC_PACKS[pack])
            continue
        domain_tools = get_tools_by_capability_domain(pack)
        if domain_tools:
            names.extend(tb.name for tb in domain_tools)
            continue
        logger.warning(f"🧩 [ToolPack] 未知能力族: {pack!r}（既非静态族也非 capability_domain）")
    return list(dict.fromkeys(names))
