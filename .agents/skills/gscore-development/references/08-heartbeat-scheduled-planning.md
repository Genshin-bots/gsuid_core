# 八、主动发言与任务编排

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[七、工具注册表与 Agent 装配](./07-tool-registry-and-agent.md) · **下一章**：[九、记忆系统](./09-memory-system.md)

本章讲四条不由用户当前消息触发的链路：Heartbeat 定时巡检、Scheduled Task
定时任务、Kanban 长任务编排、自主行动。定时巡检和自主行动共用人格作息
`active_hours`（默认 `08:00-23:30`，时区 `Asia/Shanghai`）。时段外这两条都不跑。

## 8.1 Heartbeat 定时巡检（`ai_core/heartbeat/`）

`ai_mode` 含"定时巡检"时启用，AI 每 `inspect_interval` 分钟主动巡检群聊决定要不要说话。
当前时间不在该人格的 `active_hours` 内时，这一轮巡检直接返回。

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
  │     disabled→不巡检 / global→全巡检 / global_group→仅群聊 /
  │     global_private→仅私聊 / specific→仅 target_groups
  └── _pre_check_session() 前置轻量规则过滤（零 LLM）
        ├── 无历史 → 跳过
        ├── 最后消息来自 AI → 跳过
        ├── 群已 1 小时不活跃 → 跳过
        └── AI 最近 5 条已发言（防刷屏）→ 跳过
      通过 → _inspect_session_with_semaphore（Semaphore(5) 控并发）
        └── run_heartbeat 两阶段：
              阶段一 决策（DECISION_PROMPT）→ {should_speak, mood, context_hook}
                    └─ 话头门：空 hook，或 hook 对不上近窗人类原句/C8 合并语境
                       （`heartbeat_hook_require_human_span`）→ 降级沉默
                       合并语境 peek，开口成功后才 consume
              阶段二 生成发言；与近窗主动正文过相似（`heartbeat_repeat_window`）→ 丢弃
            → _send_proactive_message（metadata={"proactive": True}）
```

```python
MAX_CONCURRENT_LLM_CALLS = 5    # 信号量
INACTIVE_THRESHOLD_HOURS = 1    # 冷场阈值
```

> **主人识别（巡检也会称呼「主人」）**：巡检的 system_prompt 是**裸人格**
> （`ROLE_PLAYING_START + persona.md`，**不含** `SYSTEM_CONSTRAINTS` 里的主人名单），
> 且巡检是"旁观扫群"、没有被动链路那种"当前说话人"，故早期只能用昵称称呼群友。
> 现 `run_heartbeat` 用 `_build_masters_section(history)` 从 `core_config["masters"]` 筛出
> **出现在本群历史里**的主人，拼成「【你的主人】…」注入决策/发言两个 user 模板的
> `{masters_section}` 占位，使巡检在主人发言时也以「主人」相称。只列**在场**主人，
> 避免塞一串与本群无关的 ID；无主人在场则该段为空、行为不变。
>
> 对比被动链路：它的 system_prompt 由 `build_persona_prompt` 组装、本就含
> `SYSTEM_CONSTRAINTS` 的主人名单（见 [§06](./06-ai-session-and-persona.md)），且每轮还会用
> `utils._build_relationship_description` 对"当前说话人"做主人高亮——所以被动链路天然会叫主人，
> 巡检则需上面这段补偿。

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

`reload_pending_tasks()`（启动时）：查所有 `pending`，未过期的重注册；**已过期的不再立即执行**
（2026-07-12 起，防启动惊群 B1）——排进「错峰 + 抖动」补偿窗口：`_OVERDUE_BASE_DELAY`(20s)
起步、`_OVERDUE_SPACING`(8s) 错峰、序号超出 `_OVERDUE_MAX_WINDOW`(1800s) 时**取模回卷**
均匀铺满窗口（不要改回 `min()` 截断——那会把溢出任务全钉在窗口边界同一时刻重造惊群）。
补偿 job 显式传 `misfire_grace_time=None`：全局默认 90s（`aps.py`），启动卡顿超过它会
**静默丢弃** date job、任务永滞 pending。循环任务按 `next_run_time` 处理。
状态机：`pending ⇄ paused`，`→ cancelled/executed`。

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
5. 追问溯源：每个能力代理 artifact_put 登记产出（没登记则执行器用 raw_result 兜底写 output artifact）。
   能力代理撞上 `task_max_iterations` 时，倒数第二次模型请求注入收口提示（停止新检索，用已有材料交付）。
   仍撞上限则把按请求累加的 token 写入 `token_usage`（这条路径没有 `result.usage`）。
   已有落盘检索或本轮回执时，无工具收成一份交付交回看板，不以失败前缀开头；评测和零材料仍回可识别失败。
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

> 🔁 **交互式 `create_subagent` 不许双份播报**（2026-07-15 修；2026-10 群聊改为派完即走）。
> `create_subagent(agent_profile=...)` 走 `_dispatch_via_kanban` 建**叶子根**、`kick_root` 后
> **立刻返回 deferred ack**，主 session 放锁，群里其他人可以继续说话或再委派。结论只走
> `_wake_main_agent_for_delivery` 回灌主人格，由主人格转述/发图。执行体把 leaf-root 登进
> `_INTERACTIVE_RELAY_ROOTS` + `_DEFERRED_MAIN_DELIVERY_ROOTS`；终态时消费 deferred 才唤醒，
> 不再 `_persona_relay` 推群。`create_by` 为 TEST 时同步等到终态，因为该入口等不到回灌。
> 回灌帧带发起人。任何轮次发 `res_` 都跟当前 `ev.user_id` 对发起人，对不上、没有说话人或没有发起人记录就拒发；
> 模型传入的 `user_id` 不能改收件人。回灌轮仍把 C 端 @ 钉在 `ev.user_id`。本帧已有主图则禁止再委派；
> 同一条任务根在 180 秒内第 2 次回灌禁止再委派。同一发起人的另一条任务各自回灌，次数不共用。没有主会话的兜底通知不算回灌次数。群/频道排队**不受** `STALE_CHAT_REQUEST_TTL`（8s）丢弃：
> 后到说话人只是在等共享 `_run_lock`，晚回优于静默；私聊同用户连发仍按 TTL 丢过期请求。
> **后台 kanban 定时 tick**（无主人格在场）从不登记 → 照常自动播报。句柄禁止写进给用户看的话
> （配合 `send_chat_result._strip_resource_handles`，见 [§7.13](./07-tool-registry-and-agent.md)）。

内置 6 画像：`research_agent` / `code_agent` / `aigc_creator` / `data_analyst` /
`memory_curator` / `scheduler_assistant` + 内部 `capability_evaluator`。业务画像（如
`finance_agent`）由插件注册（`source` 三态 builtin/plugin/user，用户画像落
`data/ai_core/capability_agents/<id>.json` 启动自动挂回）。

> **仅主人节点 + 高危执行工具（2026-09-22）**：`code_agent` / `plugin_developer_agent`
> 以及白名单含 `execute_file` / `execute_shell_command` / `run_command` 的节点是
> `master_only`。非主人在 `create_subagent`、`register_kanban_task`、重派、执行器派活前
> 都会被拒（认主人名单，不认任务上的 `user_pm`）。即便节点门被绕过，`execute_file`、
> `execute_shell_command`、`run_command`、`run_skill_script` 在**主人格和子代理**里仍由
> `wrap_tool_execute`（`block_high_risk_execute`）在函数体之前拒绝；非主人的能力代理
> 装配时还会把这些名字从列表里剥掉，技能工具集对非主人隐藏 `run_skill_script`。
> 实现在 `ai_core/tool_risk.py`。

> **Windows subprocess 兼容（2026-08-14 更正）**：此处此前写「SelectorEventLoop 不支持子进程，
> `code_agent` 跑 `execute_shell_command`/`execute_file` 在 Windows 必抛 `NotImplementedError`」
> ——**该前提已过期**。`core.py` 现在不设事件循环策略，Windows 跑 `ProactorEventLoop`，
> asyncio 子进程可用（详见 [§12.3](./12-developer-pitfalls.md)）。
> 两个工具里仍保留「Windows 走同步 `subprocess.run` + `asyncio.to_thread`」的分支，
> 现为**冗余兜底**（timeout 仍转 `asyncio.TimeoutError` 保持上层契约），不必删除，
> 但**新工具不要再照抄这个平台分支**。

### HITL 人工审批（确认码，2026-10-26）

需要主人批准时，框架向主人名单里、且订阅了「主人用户」的人私聊一枚 6 位确认码，
并把当前 agent 轮次立刻结束。未单独指定时限的票 10 分钟失效；命令执行沿用命令配置
里的审批有效期。主人只回复这一枚码，或在审批中心点批准 / 拒绝。群里说「同意」不会
通过。模型没有转达工具。

同一事项已有待审批时不再开新票、不再发第二条私聊。`check_approval` 只报告状态，
不返回确认码。`create_subagent(plugin_developer_agent)` 在插件安装还挂着时直接拒绝。

带确认码的新票：批准后领域回调做副作用（复制插件 / 执行冻住的命令或工具体），
再把续作卡回灌进**提交时的那个会话**（能力代理是 `capagent_…`，不是群会话），
权限沿用提交时的 `user_pm`。拒绝或超时发关闭通知。副作用失败、续作没有做成，
或原来的会话已经不在时，关联任务标失败，不回灌。没有确认码哈希的旧票仍走
原来的 `kick_root`，不私聊、不回灌。

控制台：`POST /api/ai/approvals/{request_id}/resolve`。看板兼容端点内部转同一函数。

### 追问溯源（决策树强制路径）

主人格被追问"你为什么选 X / 为什么这样做"时**先调 `artifact_get_recent`** 把对应任务树最近一份
artifact 原文查回来，再用角色口吻转告主人。**严禁**自行 `web_search` 重新拼凑解释（会与原代理
推理不一致）。

### WebConsole 管理

- `/api/ai/kanban/*`：5 列看板 / 详情 / 暂停 / 恢复 / 终止 / 审批 / 重派 / 评估触发（`webconsole/docs/35-kanban.md`）。
- `/api/ai/artifacts/*` + `/api/ai/kanban/tasks/{id}/workspace/*`：Artifact Hub 与 Workspace 文件管理。
- `/api/ai/capability-agents/*`：AgentNode CRUD（builtin/plugin/user/persona 四态，字段已 v2 化，`webconsole/docs/34-capability-agents.md`）。

## 8.4 自主行动（`ai_core/autonomy/`）

`ai_mode` 含「自主行动」时启用。它不替代定时巡检，也不替用户发消息。全进程一个
APScheduler 任务 `ai_autonomy_tick`，每 5 分钟最多挑一个到期人格。

到期条件：模式开着、范围不是 `disabled`、当前在 `active_hours` 内、聊天模型槽有空、
预算允许、距上一行审计已过 `autonomy_interval_minutes`（15–720，默认 45）再乘 0.8–1.2。
表是空的时，首次延迟 10–20 分钟。这些门没过不写审计行。底片是空的则写一行休息，不打模型。

一轮四段：观察底片（好感达到 `autonomy_care_min_zone` 的用户，主人始终留下；群摘要）
→ 一次性 Agent（`create_by="Autonomy"`，工具表关闭，思考轮数用全局 `multi_agent_lenth`）
→ 只认成功工具的事实 → 交付。没做成事、或动力是休息，只记审计。

交付三条：亲近或主人且对方没在说话，立刻把摘要发出去；熟人、配额满或对方正忙，挂到下次开口
（4 小时，和定时任务的 300 秒合并槽分开）；自我探索只挂槽，不立刻发。模型点名的群或用户不在
底片里，改成自我探索，不发到那个群。

代跑命令固定 `user_pm = 6`，对象必须在本轮观察名单。发送工具和高危执行不挂。
审计表只有 `aiautonomyactivity`。问过模型的一轮把 `session_id` 写进行里，会话日志来源是
`Autonomy`；只记休息的行没有会话号。登录用户 `GET /api/autonomy/feed` 看各人格最近一行
和分页记录，`GET /api/autonomy/{persona}/activities` 查单个人格。产出物页「自主行动」页签
用的就是这条 feed。管理员 `POST /api/autonomy/{persona}/trigger_once` 可跳过作息、忙闲和冷却，
仍受预算、模式和工具限制。
