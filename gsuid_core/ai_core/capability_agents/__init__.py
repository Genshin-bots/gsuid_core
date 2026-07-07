"""能力代理层（Capability Agents · AgentNode task-mode）。

AgentNode 统一后，本包 = "task-mode 实例化 + 内置节点定义 + 用户节点持久化"：
节点真值源在 ``ai_core.agent_node``（统一注册表，persona 与能力代理同构）。

## 模块组成

- ``registry``    : **插件兼容层**——旧 ``CapabilityAgentProfile`` dataclass +
                    ``register_capability_agent``（转注册到 agent_node）。
- ``profiles``    : 框架内置节点（research_agent / code_agent / internal_reporter /
                    memory_curator / scheduler_assistant / plugin_developer_agent）。
- ``evaluator``   : 框架内部 ``capability_evaluator`` 节点 + 评估缓存。
- ``runner``      : ``run_capability_agent()``——task-mode 实例化：身份核 +
                    交付边界叠加、packs+白名单装配、全局任务档预算、跑一次任务。
- ``persistence`` : webconsole 用户自建节点落盘 / 加载（v1 旧画像自动迁移）。

## 架构

Hub-and-spoke（星型）：入口节点（persona）编排 + 多个专职执行节点。执行节点
task-mode 下由 ``compose_task_prompt`` 强制叠加交付边界——只向主人格交付、
不直接对用户说话；下行播报由 ``kanban_executor._persona_relay`` 转译。

跨天持久化、崩溃恢复由 ``ai_core.planning`` 三表承担，不引入点对点消息总线。
"""

from .runner import run_capability_agent
from .profiles import register_builtin_profiles
from .registry import CapabilityAgentProfile, register_capability_agent
from .persistence import (
    AgentNodeDTO,
    is_user_profile,
    save_user_profile,
    get_profile_as_dto,
    get_profile_source,
    load_user_profiles,
    delete_user_profile,
    export_all_profiles_as_dto,
)

__all__ = [
    # 插件兼容层（下个大版本移除；新代码用 ai_core.agent_node）
    "CapabilityAgentProfile",
    "register_capability_agent",
    "register_builtin_profiles",
    "run_capability_agent",
    # 持久化 / webconsole 后端依赖
    "AgentNodeDTO",
    "save_user_profile",
    "load_user_profiles",
    "delete_user_profile",
    "is_user_profile",
    "get_profile_source",
    "get_profile_as_dto",
    "export_all_profiles_as_dto",
]
