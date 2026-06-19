# 八、主动发言与任务编排

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[七、工具注册表与 Agent 装配](./07-tool-registry-and-agent.md) · **下一章**：[九、记忆系统](./09-memory-system.md)

本章讲三条"不由用户当前消息触发"的链路：Heartbeat 定时巡检（AI 主动说话）、Scheduled Task
定时任务（用户预约）、Kanban 长任务编排（多步多代理协作）。

## 8.1 Heartbeat 定时巡检（`ai_core/heartbeat/`）

`ai_mode` 含"定时巡检"时启用，AI 每 `inspect_interval` 分钟主动巡检群聊决定要不要说话。

```
heartbeat/
├── inspector.py     # HeartbeatInspector 巡检器
├── decision.py      # LLM 决策（含 run_reactive_gate 续聊沉默门复用）
└── dispatcher.py    # 主动消息统一网关（防撞车 + 任务结果合并进 Heartbeat 语境）
```

```python
class HeartbeatInspector:
    _scheduled_jobs: dict[str, str]   # persona_name -> job_id
    def start_for_persona(self, persona_name) -> bool: ...
    def stop_for_persona(self, persona_name) -> bool: ...
    def start_all(self) -> bool: ...
```

每 persona 注册一个 APScheduler interval job：`ai_heartbeat_inspector_{persona_name}`。

### 巡检流程（两阶段：前置规则过滤 → LLM 决策）

```
定时触发 → _inspect_all_sessions_for_persona(persona_name)
  ├── 取 persona 的 scope / target_groups
  ├── 遍历活跃会话 _should_inspect_session()
  │     scope=disabled→不巡检 / global→全巡检 / specific→仅 target_groups
  └── _pre_check_session() 前置轻量规则过滤（零 LLM）
        ├── 无历史 → 跳过
        ├── 最后消息来自 AI → 跳过
        ├── 群已 1 小时不活跃 → 跳过
        └── AI 最近 5 条已发言（防刷屏）→ 跳过
      通过 → _inspect_session_with_semaphore（Semaphore(5) 控并发）
        └── run_heartbeat 两阶段：
              阶段一 决策（DECISION_PROMPT）→ {should_speak, mood, context_hook}
              阶段二 生成发言（PROACTIVE_MESSAGE_PROMPT，仅 should_speak=True）
            → _send_proactive_message（metadata={"proactive": True}）
```

```python
MAX_CONCURRENT_LLM_CALLS = 5    # 信号量
INACTIVE_THRESHOLD_HOURS = 1    # 冷场阈值
```

> ⚠️ **历史缺陷 D-2（必须保留的防护）**：原代码遍历所有活跃会话**串行**对每个会话调 LLM。
> 100 个群 + 5 分钟间隔 = 每 5 分钟瞬间 100 次并发 LLM 请求，触发 Rate Limit + Token 破产。
> 修复 = 前置规则过滤（绝大多数会话不进 LLM）+ Semaphore(5) + 300s 超时保护。**改巡检逻辑
> 时不要删掉前置过滤**。
>
> **防刷屏**：主动发言带 `metadata={"proactive": True}`；`_has_recent_ai_response()` 查最近 5
> 条，AI 已主动开过口就不再发。

> 取 `_Bot` 走 `gss.active_bot[event.WS_BOT_ID]` 三级查找（历史缺陷 D-5，见 [§05](./05-bot-classes.md)）。

## 8.2 Scheduled Task 定时任务（`ai_core/scheduled_task/`）

主 Agent 预约未来某时刻执行的任务（"明早 6 点叫我起床""每半小时查股价"）。到点由 APScheduler
唤醒，**加载当时的 persona/session 用一致语气执行**。

```
buildin_tools/scheduler.py   # 独立 AI 工具：add_once_task / add_interval_task / list / query / modify / cancel / pause / resume
scheduled_task/
├── models.py     # AIScheduledTask 数据模型
├── executor.py   # execute_scheduled_task + reload_pending_tasks + cleanup_completed_tasks
├── scheduler.py  # APScheduler 注册辅助
└── startup.py    # @on_core_start / @on_core_shutdown
```

任务类型：`once`（一次性，执行后→`executed`）/ `interval`（循环，到 `max_executions` 后→`executed`）。

### 工具与 category 的微妙设计

| 工具 | category | 原因 |
|------|----------|------|
| `add_once_task` / `add_interval_task` | `self`（保底常驻） | **创建**入口触发高度口语化（"每天下午三点半推送新闻"），向量检索难命中，必须常驻 |
| `list/query/modify/cancel/pause/resume_scheduled_task` | `common`（按需检索） | **管理**类，用户显式提需求时才需要 |

### 安全限制（防滥用，强制）

```python
MAX_PENDING_TASKS_PER_USER = 20   # 单用户最多 20 个待执行
MAX_EXECUTION_LIMIT = 10          # 循环任务最多 10 次（即便用户要"无限循环"也强制 10）
MIN_INTERVAL_SECONDS = 300        # 循环最小间隔 5 分钟
```

### 执行器与重启恢复

`execute_scheduled_task(task_id)`：读 DB → 建 Event → `get_ai_session(ev)` 加载 persona →
`session.run()` 执行 → 按类型更新状态（循环则 +`current_executions`、重算 `next_run_time`、
重注册）→ 记 `record_trigger("scheduled")` → 推送结果。

`reload_pending_tasks()`（启动时）：查所有 `pending`，一次性任务过期则立即执行/未过期重注册；
循环任务按 `next_run_time` 处理。状态机：`pending ⇄ paused`，`→ cancelled/executed`。

> 执行器开头同样要查 AI 总开关（D-21）。

## 8.3 Kanban 长任务编排（`ai_core/planning/`）

把跨步骤、多代理协作的任务做成**数据库持久化任务树**，取代历史上的"假持久化 PersistentAgent"
（agent_mesh）与"单代理跨天串行步骤长任务"（C5）。

**三张表**：

- `AIAgentTask`（`aiagenttask`）：任务节点——根 + 子任务**共表**，`node_kind="root|subtask"` 区分；
  含 `ordinal`(用户可见短序号)/`goal`/`status`/`parent_task_id`/`root_task_id`/
  `dependency_task_ids`/`agent_profile`/`failure_reason`/`respawn_count`/`input_artifact_ids`/
  `output_artifact_id`/`failure_policy`/`workspace_policy`/`broadcast_targets`/`review_notes`。
- `AIAgentTaskLog`（`aiagenttasklog`）：事件流（`plan_created`/`step_started`/`step_done`/
  `step_failed`/`decision`/`approval`/`workspace_violation`），崩溃恢复/审计依据。
- `AIAgentArtifact`（`aiagentartifact`）：节点产出登记——`payload_inline` ≤4KB / 超过走
  `payload_path` 落盘；按 `root_task_id` 严格隔离跨树读取。

### 约束：真实 ID 绝不暴露给 LLM

LLM 工具参数无 `task_id`/`root_task_id`。写原语作用于 `runtime.py` 的 `contextvars` 绑定的
current_task；引用类工具用自然语言句柄（`resolver.resolve_task_ref` 解析"任务#3"/"炒股那个"/
"运行中的"），子任务句柄 `"<root_ref>#sub<N>"`，artifact 用显式 `res_xxx` 句柄。

### 生命周期（事件驱动 · 无定时器）

```
1. 能力评估：主人格调 evaluate_agent_mesh_capability，capability_evaluator 判现有画像能否覆盖
2. 建树：covered=true 时 register_kanban_task 创建根 + N 子任务，立刻 kick_root 一次
3. 并发派活：kanban_executor.execute_ready_tasks 扫树拿可跑节点，asyncio.gather 并发 run_capability_agent
4. 状态推进：节点完/败 → 子任务落终态 + refresh_root_status 汇总根 → 递归 _schedule_continuation（最多 4 层）
5. 追问溯源：每个能力代理 artifact_put 登记产出（没登记则执行器用 raw_result 兜底写 output artifact）
6. 失败处理：默认 notify_persona 策略，重派达 3 次自动转 waiting_approval
7. 崩溃恢复：启动期 recover_zombie_subtasks 复活心跳过期的 running 子任务 + 统一 kick_root
```

每轮对话由 `planning.context.build_task_context` 注入活跃**根任务**摘要（不含子任务，避免顶层
概览被污染）。

> **没有定时器**：Kanban 纯事件驱动。需要"明天 6 点触发""每天复盘"等时间条件，用
> `add_once_task`/`add_interval_task` 在那个时刻把主人格唤醒，再视情况 `register_kanban_task`；
> **不要把时间塞进子任务字段**。

### 能力代理（执行/表达分离）`ai_core/capability_agents/`

调度器派活时**绕过主人格会话**——人格若被设定为"懒惰、回避分析"会让严肃执行抵制空转、人格
漂移。`run_capability_agent` 按子任务 `agent_profile` 唤醒**无人格能力代理**（不拒绝、不漂移），
结果经 `_persona_relay` 用人格口吻转译后通知主人。

内置 6 画像：`research_agent` / `code_agent` / `aigc_creator` / `data_analyst` /
`memory_curator` / `scheduler_assistant` + 内部 `capability_evaluator`。业务画像（如
`finance_agent`）由插件注册（`source` 三态 builtin/plugin/user，用户画像落
`data/ai_core/capability_agents/<id>.json` 启动自动挂回）。

> 🆕 **Windows subprocess 兼容**：SelectorEventLoop **不支持子进程**（见 [§02](./02-startup-lifecycle.md)），
> `code_agent` 跑 `execute_shell_command`/`execute_file` 在 Windows 必抛 `NotImplementedError`。
> 修复 = 两个工具内分平台分支：Windows 走"同步 `subprocess.run` + `asyncio.to_thread`"，POSIX
> 走原生 `asyncio.create_subprocess_exec`，timeout 转 `asyncio.TimeoutError` 保持上层契约。

### HITL 人工审批

子任务连续重派达 3 次（`DEFAULT_RESPAWN_LIMIT`）→ 自动 `waiting_approval`。两条审批通路：

1. **webconsole**：Kanban 看板 Blocked 列点卡片 → `POST /api/ai/kanban/subtasks/{id}/approve`。
2. **对话回复**：主人对 bot 说"同意/拒绝"，主人格调
   `respond_subtask_approval(approved, note, subtask_ref="")`。批准→子任务退回 `pending`；拒绝
   →`failed`，主人格再决定 `fail_task_tree`。

### 追问溯源（决策树强制路径）

主人格被追问"你为什么选 X / 为什么这样做"时**先调 `artifact_get_recent`** 把对应任务树最近一份
artifact 原文查回来，再用角色口吻转告主人。**严禁**自行 `web_search` 重新拼凑解释（会与原代理
推理不一致）。

### WebConsole 管理

- `/api/ai/kanban/*`：5 列看板 / 详情 / 暂停 / 恢复 / 终止 / 审批 / 重派 / 评估触发（`webconsole/docs/35-kanban.md`）。
- `/api/ai/artifacts/*` + `/api/ai/kanban/tasks/{id}/workspace/*`：Artifact Hub 与 Workspace 文件管理。
- `/api/ai/capability-agents/*`：画像 CRUD（builtin/plugin/user 三态权限，`webconsole/docs/34-capability-agents.md`）。
