"""能力代理注册 API —— 插件兼容层。

AgentNode 统一后，能力代理的真值源是 ``ai_core.agent_node``（统一注册表 +
``resolve_node``）。本模块只保留给**存量插件**的兼容入口：

- ``CapabilityAgentProfile``：旧字段名 dataclass（``profile_id`` / ``system_prompt``
  / ``max_iterations`` 等），仅供未迁移插件构造；
- ``register_capability_agent``：接受旧 dataclass 或新 ``AgentNode``，统一转注册。

新代码（含插件新版本）请直接 ``from gsuid_core.ai_core.agent_node import
AgentNode, register_agent_node``。本兼容层将在下一个大版本移除。
"""

from typing import List, Union
from dataclasses import field, dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.agent_node import TASK_BASICS_PACK, AgentNode, register_agent_node


@dataclass
class CapabilityAgentProfile:
    """【已废弃】旧版画像定义。预算字段（max_iterations / max_tokens）已抹平为
    全局配置 ``task_max_iterations`` / ``task_max_tokens``，传入值被忽略。"""

    profile_id: str
    display_name: str
    when_to_use: str
    system_prompt: str
    match_keywords: List[str]
    tool_names: List[str] = field(default_factory=list)
    tool_query: str = ""
    max_iterations: int = 20
    max_tokens: int = 35000


def _profile_to_node(profile: CapabilityAgentProfile) -> AgentNode:
    return AgentNode(
        node_id=profile.profile_id,
        display_name=profile.display_name,
        prompt=profile.system_prompt,
        prompt_style="plain",
        when_to_use=profile.when_to_use,
        match_keywords=list(profile.match_keywords),
        tool_packs=[TASK_BASICS_PACK],
        tool_names=list(profile.tool_names),
        tool_query=profile.tool_query,
        source="plugin",
    )


def register_capability_agent(profile: Union[CapabilityAgentProfile, AgentNode]) -> None:
    """注册一个能力代理（兼容入口）。旧 dataclass 自动转换为 AgentNode。"""
    if isinstance(profile, AgentNode):
        register_agent_node(profile)
        return
    logger.warning(
        t(
            "🤖 [CapabilityAgent] CapabilityAgentProfile 已废弃（{p0}），请迁移到"
            " agent_node.AgentNode + register_agent_node（预算字段已被忽略）",
            p0=profile.profile_id,
        )
    )
    register_agent_node(_profile_to_node(profile))
