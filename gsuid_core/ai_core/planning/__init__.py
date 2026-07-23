"""Agent Mesh Kanban 任务编排层

把跨步骤、多代理协作的任务统一收敛到一棵"可持久化、可并发推进"的任务树：

- ``models``         : AIAgentTask / AIAgentTaskLog / AIAgentArtifact 三张持久化表。
- ``kanban``         : 任务树 manager——创建 / 查询 / 状态汇总 / 失败 / 重派 / 审批 / 暂停恢复。
- ``kanban_executor``: 任务树并发调度执行器（``execute_ready_tasks`` / ``kick_root``）。
- ``kanban_tools``   : 暴露给主人格 LLM 的 Kanban 工具（注册 / 重派 / 审批 / artifact 增查）。
- ``resolver``       : 自然语言任务引用解析（"那个周报任务" → 根任务），框架代管真实 ID。
- ``runtime``        : 单次执行的 PlanRunContext 绑定（任务 / 工作区 / 画像）。
- ``workspace``      : Artifact Workspace 路径守卫与登记。
- ``context``        : 每轮动态注入"当前用户的根任务摘要"，让主人可追问进度。
- ``startup``        : ``init_planning`` 注册工具 / 画像 / 评估代理 + 崩溃恢复。

约束：真实数据库 ID 绝不作为 LLM 工具参数；任务引用通过自然语言句柄 + 框架解析完成。
"""
