"""能力代理层（Capability Agents）

把「执行」从「人格表达」剥离：复杂任务由**无人格**的专职能力代理推进，
主人格只负责识别派发、查进度、把代理产物用人格口吻转告主人。能力代理与
主人对话之间隔了一层主人格转译——这是框架的核心约束之一，prompt 与工具
配置都围绕它来。

## 模块组成

- ``registry``    : ``CapabilityAgentProfile`` dataclass + 全局注册表 +
                    ``resolve_profile``（自然语言关键词 → profile_id）。
- ``profiles``    : 框架内置画像（v3：``research_agent`` / ``code_agent`` /
                    ``internal_reporter`` / ``memory_curator`` /
                    ``scheduler_assistant``）。
- ``evaluator``   : 框架内部 ``capability_evaluator`` 画像 + 评估缓存；
                    服务于 ``evaluate_agent_mesh_capability`` LLM 工具。
- ``runner``      : ``run_capability_agent()``——按画像装配工具、实例化无人格
                    Agent、跑一次任务、返回纯文本结果。
- ``persistence`` : 用户在 webconsole 自定义的画像落盘 / 加载 + 标识画像来源
                    （``builtin`` / ``user`` / ``plugin`` / ``missing``）。

## 架构

Hub-and-spoke（星型）：一个人格编排层 + 多个专职执行者：

```
                       ┌─────────────────────────┐
                       │ 主人格 (Persona)         │
                       │   - 与主人对话           │
                       │   - 任务编排 / 转译播报  │
                       └────────┬────────────────┘
                                │ run_capability_agent(profile_id, task)
                                │ 或 Kanban 调度 _run_one_task_node
                                ▼
       ┌─────────────────────────────────────────────────────────┐
       │ Capability Agents (无人格)                                │
       │   research_agent · code_agent · internal_reporter        │
       │   memory_curator · scheduler_assistant · 插件业务画像     │
       │   交付：artifact_put + 函数返回值 → 主人格转译后下发      │
       └─────────────────────────────────────────────────────────┘
```

跨天持久化、崩溃恢复由 ``ai_core.planning`` 三表（``AIAgentTask`` /
``AIAgentTaskLog`` / ``AIAgentArtifact``）承担，**不**引入点对点消息总线。

## 关键约束

1. 能力代理 ``system_prompt`` 来自画像，**不携带角色人格**——它们对结果负责，
   不寒暄、不抱怨、不演角色。
2. 能力代理**禁止**调用 ``send_message_by_ai`` / ``send_meme`` 等"直接下发到
   主人"的工具——这些工具仅供主人格使用。代理只把结果交回；下行播报由
   ``kanban_executor._persona_relay`` 用主人格口吻包一层后发出。
3. 同 ``profile_id`` 由后写覆盖前写——插件可用相同 id 重写覆盖内置画像。
4. 业务画像（``stock_agent`` / ``weather_agent`` 等）不在本模块内置，由对应
   插件在自身启动钩子 ``register_capability_agent(...)`` 注册。
"""

from .runner import run_capability_agent
from .profiles import register_builtin_profiles
from .registry import (
    CapabilityAgentProfile,
    get_profile,
    list_profiles,
    resolve_profile,
    register_capability_agent,
    unregister_capability_agent,
)
from .persistence import (
    CapabilityAgentDTO,
    is_user_profile,
    save_user_profile,
    get_profile_as_dto,
    get_profile_source,
    load_user_profiles,
    delete_user_profile,
    mark_as_user_profile,
    export_all_profiles_as_dto,
)

__all__ = [
    "CapabilityAgentProfile",
    "register_capability_agent",
    "unregister_capability_agent",
    "register_builtin_profiles",
    "resolve_profile",
    "get_profile",
    "list_profiles",
    "run_capability_agent",
    # 持久化 / webconsole 后端依赖
    "CapabilityAgentDTO",
    "save_user_profile",
    "load_user_profiles",
    "delete_user_profile",
    "is_user_profile",
    "mark_as_user_profile",
    "get_profile_source",
    "get_profile_as_dto",
    "export_all_profiles_as_dto",
]
