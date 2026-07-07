# ai_core — GsCore AI 子系统

> 这里是 GsCore 整个 AI 能力的"主干"：从一条用户消息进入、到 LLM 决策、到工具调用、到主动回话，全部链路的核心模块都在本目录下。
>
> **本 README 不是入门教程**，而是给"要在本目录写代码 / 改架构"的工程师与 AI Agent 看的导航册。开始动手前请把 §"⚠️ 必读注意点"读完。

---

## ⚠️ 必读注意点（动手前先看）

1. **遵守 `docs/LLM.md` 的红线**：禁止 `try/except` 兜底、禁止 `cast()`、禁止 `type: ignore`、禁止 `getattr / dict.get` 兜底。所有红线在本目录代码里都会被实际 review。
2. **`Bot` ≠ `_Bot`**（`gsuid_core/bot.py`）：
   - `_Bot` 是底层 WebSocket 实现，**不依赖 Event**，构造为 `_Bot(_id, ws)`。
   - `Bot` 是高层包装，**强依赖 Event**，构造为 `Bot(_bot, ev)`。
   - 本目录所有"对外发送"路径都走 `Bot.send` / `send_chat_result`；想拿底层 `_Bot` 只能从 `gss.active_bot` 里捞。**不要**自己造一个无 `Event` 的 `Bot` 实例。
3. **完全异步**：所有可能阻塞的方法必须 `async def`；同步 CPU 工作用 `@to_thread`（见 `gsuid_core/pool.py`）。
4. **数据库**：本目录的所有表都继承 `BaseIDModel / BaseBotIDModel / BaseModel`，所有类方法用 `@with_session`；表名 = 类名全小写无下划线，**禁止** `__tablename__`。详见 `docs/LLM.md` §3。
5. **主动消息（Heartbeat / ScheduledTask / Kanban / 工具主动 send）必须走 `ai_core.proactive.emit_proactive_message`**——不要直接 `bot.send` + 手动 `message_history.add_message` + 手动 `dispatcher.register_send`。详见 §"`proactive/`" 与 `plans/proactive_message_session_unification_20260529.md`。
6. **会话日志统一在 `AISessionLogger`**（详见 `docs/AI_SESSION_LOGGING.md`）：**所有 `GsCoreAIAgent` 恒有 `_session_logger`**（非 Optional）——不传 `session_id` 的来源（评估 / meme / 记忆 / 图片理解等后台调用）会在 `__init__` 自动派生 `auto_<create_by>_<rand>` 的一次性 subagent id。SubAgent 日志落 `data/ai_core/session_logs/subagents/`，与主 session 物理隔离。**显式 SubAgent 用完仍建议 `agent._session_logger.close()`** 及时落盘（subagent 不跑后台轮询，靠 close/__del__ 落盘）。**相同 session_id 在 1 小时窗口内续写同一文件，超时滚动新文件**（`SESSION_WINDOW_SECONDS`）。
7. **不要把任务 prompt 当 user_message 喂给主用户 session**（污染 `self.history` 与 `session_logger`，让用户回放历史时看到自己"发"过没发的话）。如果你要让人格执行某项后台任务，请派 SubAgent + emit_proactive_message。
8. **session_logger 落盘**：**仅主 session** 启动异步轮询（`PERSIST_INTERVAL=600s` 兜底 / `IDLE_PERSIST_THRESHOLD=60s`）；**subagent（含 auto_*）不轮询**，跑完即 `close()` 或由 `__del__` 兜底落盘——所以一次性 SubAgent 仍应显式 `close()` 以便及时可见。entry 类型受 `SESSION_ENTRY_TYPES` 白名单约束，新增类型必须先登记（否则记 warning）。会话日志按 `ScheduledCleanLogDay` 每日清理 X 天外文件（**0=不清理**，与框架日志同配置，见 `docs/AI_SESSION_LOGGING.md` §6.1）。
9. **`message_history` 由 `_Bot.target_send` 内部写入**（`bot.py:291`）。如果你额外手动 `history_manager.add_message`，同一条消息会被重复落库。
10. **`fewer permission prompts`**：本目录代码经常被前端 / webconsole API 读取（`ai_session_logs_api.py`、`history_api.py`），改动 entry / metadata schema 时是**追加字段**，**不要破坏**老字段（前端 dist 已编译了对旧字段的依赖）。

---

## 顶层目录速览

| 文件 | 职责 |
|---|---|
| `gs_agent.py` | **`GsCoreAIAgent`**：基于 pydantic_ai 的 Agent 主类。封装运行锁、history 截断、工具池组装（三层）、token 统计、出戏防火墙重说闭环、单轮意图-行为一致性检测、UsageLimit 兜底总结、`append_proactive_assistant_turn`（主动消息同步到 history）等。所有 LLM 调用都从这里出去。 |
| `handle_ai.py` | 用户消息入口（被消息触发器调用）。负责会话匹配、前置规则过滤、`format_history_for_agent` 构造 history_context、调用 `GsCoreAIAgent.run`。 |
| `ai_router.py` | `get_ai_session` / `get_ai_session_by_id`：根据 Event 找到或创建主用户绑定的 `GsCoreAIAgent`，并触发 Persona 热重载、主人好感度初始化。 |
| `session_registry.py` | `{session_id → GsCoreAIAgent}` 注册表 + 空闲 session 清理 / logger flush / shutdown。 |
| `session_logger.py` | **`AISessionLogger`** —— ai_core **唯一**的会话日志序列化器（所有来源同一条写盘路径）。entry 类型由 `SESSION_ENTRY_TYPES` 白名单固定（`system_prompt / user_input / tool_call / tool_return / text_output / thinking / proactive_emission` 等）；`link_agent` 串联 SubAgent；会话窗口 `SESSION_WINDOW_SECONDS=3600`（相同 session_id 续写 / 超时滚动）；`log_standalone_proactive` 处理主动消息磁盘回退。落 `data/ai_core/session_logs[/subagents]/`。**完整契约见 `docs/AI_SESSION_LOGGING.md`；修改 entry / linked_agents 结构会影响 webconsole 前端**。 |
| `models.py` | `ToolContext`（包含 `bot / ev / extra / parent_session_id`）、`ToolBase`、`KnowledgeBase` 等 TypedDict。 |
| `register.py` | `@ai_tools` 装饰器，把工具按 category（`self / buildin / 插件名`）登记到全局工具表。 |
| `resource.py` | 路径常量：`AI_SESSION_LOGS_PATH` / `AI_SUBAGENT_LOGS_PATH` / `PERSONA_PATH` / `RAG_DATA_PATH` 等。 |
| `utils.py` | `send_chat_result`（解析 @、meme、换行分发）、`SILENCE_MARKERS`、`extract_json_from_text` 等。**`send_chat_result` 支持 `extra_metadata` 透传**，主动消息走它落 metadata。 |
| `history_format.py` | `format_history_for_agent`：把 `message_history.MessageRecord` 序列化成 LLM 可读的 `history_context` 文本（被动回复走这条路）。 |
| `normalize.py` | 文本归一化（去 emoji / 标点等），主要给 RAG / classifier 用。 |
| `check_func.py` | 通用权限 / 状态检查工具。 |
| `trigger_bridge.py` | 把"主人格 LLM 决定要调一个具体命令"桥接到 GsCore 触发器系统。 |
| `startup.py` | ai_core 的启动钩子聚合点，被 `gsuid_core.startup` 调用。 |
| `self_cognition.py` | 自我认知（"你是谁、能做什么"）相关的辅助常量。 |

---

## 子模块详解

### `gs_agent.py` 中的关键设计

- **运行锁** `_run_lock`：保证同一 `GsCoreAIAgent` 不会被并发 `run`，避免 history 撕裂。
- **`extract_history()`**：先 `_truncate_history_with_tool_safety`（保留 ToolCall / ToolReturn 配对），再 `_drop_orphan_tool_results`（兜底丢孤儿，防止 pydantic_ai 报 "tool result's tool id not found"）。
- **三层工具池**：保底（`get_main_agent_tools`）+ 语境（群组画像标签）+ 查询（`search_tools` 向量搜索）。`create_by ∈ {"SubAgent","Chat","Agent","AutoPlanner"}` 才走自动组装；其它来源（如 `Heartbeat_Decision`）传 `tools=[]` 拒绝任何工具。
- **`append_proactive_assistant_turn(content, source, trigger_reason, generator_log_files=None)`**：把一条主动消息以 assistant-only `ModelResponse(TextPart)` 形式追加进 `self.history`，同步在 `_session_logger` 写一条 `proactive_emission` entry。Heartbeat / ScheduledTask / Kanban / 工具主动 send 全部通过它把"框架外注入的输出"同步进 pydantic_ai 历史，避免主人格"对刚说过的话失忆"。详见 `plans/proactive_message_session_unification_20260529.md` §3.5。
- **工具前置告知**：框架**不再**播报任何固定"前摇台词"（原 `_FRAMEWORK_PRE_TOOL_EXPRESSIONS` / persona `pre_tool_expressions` 已移除）。耗时工具前的告知由 Agent 依 `TOOL_ORCHESTRATION_CONSTRAINTS` 的"耗时工具处理"条款用自己的话完成（流式 TextPart 先于工具结果发出）。

### `proactive/` — 主动消息统一发送闭包（新）

> 详细设计：`plans/proactive_message_session_unification_20260529.md`

**唯一入口**：`emit_proactive_message(event, message, *, source, trigger_reason, generator_log_files=None, bot=None, suppress_when_heartbeat_recent=True)`。

内部按序：

1. **C8 防撞车**——复用 `heartbeat/dispatcher.py` 的 `UnifiedProactiveDispatcher.should_suppress_heartbeat`。`source="heartbeat"` 默认开启抑制；其它来源（`task / kanban / tool`）默认 `False`，避免关键播报被刚发完的 Heartbeat 误杀。
2. **bot.send**——通过 `send_chat_result(bot, message, extra_metadata={"proactive": True, "proactive_source": source, "trigger_reason": ...})`。`message_history` 由 `_Bot.target_send` 内部写入一次。**禁止**调用方再手动 `history_manager.add_message`，否则同一条消息会落库两次。
3. **主 session 同步**——通过 `session_registry.get_ai_session(event.session_id)` 找到用户绑定的 `GsCoreAIAgent`，调 `append_proactive_assistant_turn(...)` 把消息塞进 `self.history`，并在 `AISessionLogger` 写 `proactive_emission` entry；`generator_log_files` 会通过 `link_agent("proactive_generator", ..., log_file=...)` 挂到 `linked_agents` 上，webconsole 可点跳子 agent 日志。**主 session 不在内存时**（已被空闲清理 / 用户从未对话）走 `AISessionLogger.log_standalone_proactive` —— 用临时 logger 复用统一的会话窗口续写 + `_build_data` 写盘，格式与活跃 session 完全一致（详见 `docs/AI_SESSION_LOGGING.md`）。
4. **C8 网关登记**——`dispatcher.register_send(target_key, legacy_source, summary)`。

⚠️ 改造前，Heartbeat / ScheduledTask / Kanban 各自手写"bot.send + add_message + register_send"链路，造成：
- 同一条主动消息在 `message_history` 落两次；
- 决策 / 转译子 agent 没有 session_logger，事后无法审计；
- 主人格的 `self.history` 拿不到自己刚发出去的话；
- 任务 prompt 被当 `user_input` 喂给主 session，污染历史。

新前端契约（**全部追加字段**，旧前端不会崩）：

| 字段 | 位置 | 含义 |
|---|---|---|
| `proactive=True` | `message_history.MessageRecord.metadata` | 这是一条主动消息 |
| `proactive_source` | 同上 | `heartbeat / scheduled_task / kanban / tool` |
| `trigger_reason` | 同上 | mood / task_id / subtask name / tool name |
| `proactive_emission` | `session_logs/*.json` 的 entry type | 主动消息发射记录，data 段是 `ProactiveEmissionPayload` |
| `agent_type="proactive_generator"` | `linked_agents` | 决策 / 转译 / 执行体子 agent |

### `heartbeat/` — 定时巡检（主人格主动开口）

| 文件 | 职责 |
|---|---|
| `inspector.py` | APScheduler 钩子；启停 `start_heartbeat_inspector / stop_heartbeat_inspector`；前置规则过滤（30 分钟冷场判定 / 防刷屏 / C8 抑制）；信号量限流 5 路 LLM 调用。 |
| `decision.py` | `run_heartbeat(event, history, persona_name, extra_context="")`：决策 + 发言两阶段子 agent。**入参直接收 `persona_name: str`，不再要求传 GsCoreAIAgent**——避免每次心跳给"用户没跟 AI 说过话"的会话凭空创建 2-entry 的空壳 session_logger 文件。**返回 `(mood, message, generator_log_files)` 三元组**，由 inspector 交给 emitter。 |
| `dispatcher.py` | `UnifiedProactiveDispatcher`：C8 网关单例（`should_suppress_heartbeat / consume_merge_context / register_send`）。本身只做协调，不做发送——发送闭包在 `proactive/emitter.py` 中。**proactive/emitter.py 复用本文件的单例**。 |

⚠️ 巡检入口 `_inspect_session` 不再调 `_send_proactive_message`，而是 `await emit_proactive_message(event=event, message=message, source="heartbeat", trigger_reason=mood, generator_log_files=generator_log_files)`。

### `scheduled_task/` — 用户预约的定时唤醒

| 文件 | 职责 |
|---|---|
| `executor.py` | **`execute_scheduled_task(task_id)`**：派 SubAgent 执行体（独立 session_id + is_subagent=True + 任务对应 persona 的 system_prompt），任务 prompt 只在 SubAgent 内出现。结果通过 emitter 播报。 |
| `scheduler.py` | APScheduler 注册 / 取消 / 列出任务的工具集。 |
| `models.py` | `AIScheduledTask` 表（task_type=once/interval、structured_context、result、last_result_summary 等）。 |
| `startup.py` | `reload_pending_tasks` 启动期 catch-up，重新加载未到期任务 + 立即执行已到期任务（走同一条 `execute_scheduled_task` 路径）。 |

⚠️ **不要**把任务 prompt 当 user_message 喂给真用户 session——那会污染主 session 的 `self.history` / `message_history` / `session_logger`。改造后 `executor.py` 已经强制走 SubAgent 形态。

### `planning/` — Kanban 任务树并发调度

| 文件 | 职责 |
|---|---|
| `kanban.py` | 任务树的数据访问层（CRUD + 状态机），所有 SQL 操作集中在这里。 |
| `kanban_executor.py` | 并发调度引擎：`execute_ready_tasks` / `kick_root` / `_run_one_task_node` / `_persona_relay`（人格转译）/ `_notify` / `_notify_failure`。**转译 + 失败播报全部走 emitter（source="kanban"）。** |
| `kanban_tools.py` | 给主人格的 Kanban LLM 工具集：`register_kanban_task`、`respawn_subtask`、`fail_task_tree`、`evaluate_agent_mesh_capability` 等（审批转达统一走 `buildin_tools/approval_tools.py::respond_approval`）。 |
| `recurring.py` | 周期任务模板的"to APScheduler"层：`arm` / `disarm` / `_fire_template` / `_fire_subtask_template`，每次到点 clone 一个执行实例并 `kick_root`。 |
| `models.py` | `AIAgentTask` / `AIAgentArtifact` / `AIAgentTaskLog` 表。 |
| `runtime.py` | `PlanRunContext` + `bind_plan_context / reset_plan_context`：把任务上下文按 contextvar 串到能力代理。 |
| `workspace.py` | Artifact Workspace 沙盒（`ensure_workspace / put_artifact / list_artifacts`）。 |
| `resolver.py` | 工件 ID（`res_xxx`）的解析与 RM 资源桥接。 |
| `context.py` | 任务树上下文聚合工具，给主人格 prompt 用。 |
| `startup.py` | 启动期把数据库里 `armed` 的周期模板重新挂回 APScheduler。 |

⚠️ Kanban 转译子 Agent **必须**启用 SubAgent 日志（`is_subagent=True`）；早期为避免噪声曾经禁用，但归一到 emitter 后必须保留以供审计——日志路径会通过 `generator_log_files` 挂到主 session 的 `linked_agents`。

### `agent_node/` — AgentNode 统一节点层（2026-07-07）

Persona 与能力代理同构为一个 `AgentNode`（统一注册表 + persona 目录只读投影 +
工具能力族 `dynamic`/`task_basics`/域族 + task-mode 交付边界叠加）。详见
`docs/AGENT_NODE_UNIFICATION_20260707.md`。

### `approval/` — 统一审批中心（2026-07-07）

一张 `AIApprovalRequest` 表 + `submit`/`resolve` 两个动词 + category 领域回调
（`command_exec` / `kanban_subtask` / `tool_call` / `agent_request`）。三个裁决
入口（对话工具 `respond_approval`、`/api/ai/approvals`、Kanban 看板兼容端点）
全部落到本模块；`@ai_tools(approval=...)` 是它的工具策略门。

### `capability_agents/` — 能力代理（AgentNode task-mode 实例化）

| 文件 | 职责 |
|---|---|
| `registry.py` | **插件兼容层**：旧 `CapabilityAgentProfile` dataclass + `register_capability_agent`（转注册到 agent_node，下个大版本移除）。 |
| `profiles.py` | 内置节点（`research_agent / code_agent / internal_reporter / memory_curator / scheduler_assistant / plugin_developer_agent`），AgentNode 定义。 |
| `runner.py` | **`run_capability_agent(profile_id, task, ev, bot, session_id_suffix)`**：task-mode 实例化——身份核+交付边界叠加、packs+白名单装配、全局任务档预算（`task_max_iterations/tokens`）、绑 PlanRunContext、写 capability_agent 日志。 |
| `evaluator.py` | "evaluate_agent_mesh_capability" 工具的实现——告诉主人格当前任务谁能干。 |
| `persistence.py` | webconsole 用户自建节点落盘 / 加载（v1 旧画像 JSON 自动迁移）。 |

### `memory/` — 记忆系统

> ⚠️ 巨大子模块，分层很多。这里只给导航。

| 子目录 / 文件 | 用途 |
|---|---|
| `observer.py` | 群消息观察器：把流量喂给短期 / 长期记忆通道。 |
| `ingestion/` | 入库链路：`hiergraph.py`（分层图谱 + 群组摘要缓存）、`entity.py`（实体抽取）、`episode.py`（情节切片）。 |
| `retrieval/` | 召回链路（按 scope_key + 实体 + 时间窗口）。 |
| `vector/` | 向量索引（`collections.py` 集成 Qdrant 本地实例，落 `data/ai_core/local_qdrant_db`）。 |
| `database/` | 记忆表 ORM。 |
| `prompts/` | 入库 / 召回 / 摘要的 prompt 模板（`summary.py / selection.py / output_models.py`）。 |
| `lifecycle/` | 短期 → 长期的搬运、过期清理。 |
| `scope.py` | `ScopeType.GROUP / USER / GLOBAL` + `make_scope_key`，所有记忆都按 scope_key 隔离。 |
| `group_profile.py` | 群组画像（语境标签 / 性格摘要）。 |
| `config.py` | 记忆模块的开关（`enable_heartbeat_memory` 等）。 |
| `startup.py` | Qdrant + 数据库初始化。 |

### `rag/` — 知识库 / 工具检索 / 图片召回

| 文件 | 职责 |
|---|---|
| `base.py` | RAG 接口契约。 |
| `embedding.py` | 嵌入模型客户端（默认 OpenAI 嵌入或本地）。 |
| `knowledge.py` | 知识点 CRUD + 检索。 |
| `image_rag.py` | 图片实体的注册与语义检索（落 `data/ai_core/local_embedding_images/`）。 |
| `reranker.py` | 二阶段重排（BAAI/bge-reranker-base，模型缓存在 `data/ai_core/rerank_models_cache/`）。 |
| `tools.py` | **`search_tools / get_main_agent_tools / get_scope_context_tags / get_tools_by_context_tags`**：`GsCoreAIAgent._execute_run` 装工具池靠这一组。 |
| `startup.py` | 索引初始化。 |

### `persona/` — 人格系统

| 文件 | 职责 |
|---|---|
| `persona.py` | `Persona` 主类（懒加载 / 文件路径管理）。 |
| `processor.py` | **`build_persona_prompt(name, mood_key=None, group_description=None)`**：把 persona md + mood + group_description 组装成 system_prompt。 |
| `prompts.py` | `ROLE_PLAYING_START / CHARACTER_BUILDING_TEMPLATE` 等模板。 |
| `resource.py` | `load_persona / extract_compact_persona`（Heartbeat 决策阶段用压缩版 persona 节省 token）。 |
| `mood.py` | 情绪状态机（per `mood_key` 隔离）。 |
| `config.py` | `persona_config_manager`：每个 persona 一份 config.json，含 `ai_mode / inspect_interval / scope / target_groups / tool_packs / tool_names` 等。 |
| `group_context.py` | 群聊场景的群组画像注入。 |
| `models.py` | `PersonaFiles / PersonaMetadata` dataclass。 |
| `startup.py` | 把所有 persona 启动期注入。 |

### `buildin_tools/` — 框架内置工具集（`@ai_tools(category="self|buildin")`）

| 文件 | 用途 |
|---|---|
| `message_sender.py` | `send_message_by_ai`：主动发送文本 / 图片（支持 `img_xxx`/`res_xxx`/http/base64 多源）。**已对接 `ToolContext.parent_session_id`，自动把发出去的文本同步进父 session 历史**。 |
| `subagent.py` | `create_subagent`：让主人格生成单步 Kanban 叶子根任务。 |
| `scheduler.py` | `add_once_task / add_interval_task / cancel_task / list_my_tasks`：定时任务工具集。 |
| `meme_tools.py` | 表情包检索 / 触发。 |
| `html_render_tools.py` | `render_html_to_image / render_markdown_to_image`：可视化产出。 |
| `web_search.py / web_fetch.py` | 联网。 |
| `rag_search.py` | 知识库检索。 |
| `database_query.py` | 让 LLM 安全查 GsCore 内置表。 |
| `file_manager.py / file_operations.py` | 工件落盘 / 读取。 |
| `command_executor.py` | 桥接到 GsCore 触发器（让 LLM 主动调一条命令）。 |
| `self_info.py / get_time.py / favorability_manager.py` | 自我认知 / 时间 / 好感度。 |
| `dynamic_tool_discovery.py` | 让 LLM 自己探查可用工具集。 |

### `mcp/` — MCP 协议接入

让 GsCore 既能作为 MCP 客户端调外部 MCP server，也能作为 MCP server 把自己的工具暴露给 Claude Desktop 等。

| 文件 | 用途 |
|---|---|
| `client.py` | MCP 客户端核心。 |
| `server.py` | MCP server 实现。 |
| `mcp_tool_caller.py` | 把 MCP 工具桥接为 pydantic_ai Tool。 |
| `utils.py` | MCP 复用函数层（`build_mcp_arguments` / `is_mcp_provider` 等），含 **details 参数映射**。 |
| `mcp_tools_config.py / mcp_presets.py / config_manager.py` | 配置层。`mcp_tools_config` 支持 `details` 字段实现参数名映射。 |
| `startup.py` | 启动期注入。 |

**details 参数映射**：不同 MCP 工具的参数名不同，通过 `details` 字典建立映射（`"params - <内部名>"` → 从内部取值，字面量 → 固定值），使框架内部函数的参数自动转换为 MCP 工具期望的格式。

### `configs/` — LLM 提供商配置

| 文件 | 用途 |
|---|---|
| `ai_config.py` | `ai_config` 主开关（`enable / enable_memory / multi_agent_lenth` 等）。 |
| `models.py` | `get_model_for_task("high" / "low")`：按 `task_level` 路由到不同模型。 |
| `provider_config_manager.py` | 多 provider 注册表。 |
| `openai_config/` / `anthropic_config/` | 两套内置 provider。 |

### `database/` — ai_core 自有 ORM 模型

`models.py`：`UserFavorability`、`AIUsage`、`AIUserPreference` 等。**所有方法用 `@with_session`**。

### `skills/` — pydantic_ai toolset

| 文件 | 用途 |
|---|---|
| `resource.py` | `skills_toolset` 单例，挂在 `Agent(toolsets=[skills_toolset])`。 |
| `operations.py` | 技能 CRUD（让 LLM 自己持久化"我学会了什么"）。 |

### `statistics/` — 统计 / 监控

| 文件 | 用途 |
|---|---|
| `manager.py` | `statistics_manager` 单例：`record_trigger / record_heartbeat_decision / record_latency / record_token_usage / record_error`。webconsole 的 `/api/ai/statistics/*` 读这里。 |
| `models.py / dataclass_models.py` | 计数数据结构。 |
| `startup.py` | 启动期初始化。 |

### `classifier/` — 模式分类

`mode_classifier.py`：路由消息到不同 ai_mode（闲聊 / 任务 / RAG / 沉默 等）。模型文件落 `data/ai_core/intent_classifier_v5.2.joblib`。

### `state_store/` — 持久化业务状态

| 文件 | 用途 |
|---|---|
| `store.py` | 通用 KV 表（`AIStateRecord`），按 scope_key 隔离。 |
| `record_tools.py` | `record_put / record_append / record_update / record_get / record_query`：Kanban 子任务持久化业务数据（账户、签到名单等）的统一通道。 |
| `tools.py` | `state_set / state_get / state_delete`：相对自由的 KV 工具。 |
| `models.py` | ORM。 |

### `meme/` — 表情包

| 文件 | 用途 |
|---|---|
| `library.py` | 本地表情包库扫描 + 文件读取。 |
| `selector.py` | `pick(mood, scene, persona, session_id)`：按情绪/场景/人格挑表情包。 |
| `tagger.py` | 启动期自动给新表情包打标签。 |
| `observer.py` | 群内表情包使用观察。 |
| `filter.py` | 去重 / NSFW 过滤。 |
| `database_model.py` | `AiMemeRecord` 表（记录每次使用）。 |
| `config.py` | `meme_enable` 等开关。 |
| `startup.py` | 启动期扫描。 |

### `multimodal/` — 多模态

| 文件 | 用途 |
|---|---|
| `asr.py` | 语音转文字。 |
| `video.py` | 视频生成 / 处理。 |
| `document.py` | 文档解析（PDF / DOCX 等）。 |

### `image_understand/` — 图片理解（视觉模型）

`understand.py`：`understand_image(url)`，配合 `GsCoreAIAgent._summarize_image_description` 对图片描述做二次摘要节省 token。

### `web_search/` / `web_fetch/`

| 文件 | 用途 |
|---|---|
| `web_search/search.py` | 顶层调度，自动选 Exa / Tavily。 |
| `web_search/exa_search.py` / `tavily_search.py` | 具体 provider。 |
| `web_fetch/` | URL 抓取 + 正文提取。 |

---

## 数据落盘路径（`data/ai_core/`）

| 路径 | 内容 |
|---|---|
| `session_logs/` | 主 session 的 AISessionLogger JSON。 |
| `session_logs/subagents/` | SubAgent（Heartbeat 决策 / 发言、Kanban 转译、ScheduledTask 执行体）的日志。 |
| `persona/<name>/` | persona 资源（`persona.md / avatar.png / image.png / config.json / audio.*`）。 |
| `local_qdrant_db/` | 记忆向量索引。 |
| `local_embedding_images/` | 图片 RAG 缓存。 |
| `rerank_models_cache/` | reranker 模型权重。 |
| `intent_classifier_v5.2.joblib` | 意图分类器。 |
| `openai_config.json / gemini_config.json / ...` | LLM provider 配置。 |

---

## 典型流水线

### A. 被动回复（用户消息）

```
User → Trigger → handle_ai
                 ├─ message_history 已经写入 user 消息
                 ├─ get_ai_session(ev) → GsCoreAIAgent（单例 / 热重载）
                 ├─ format_history_for_agent → history_context 文本
                 └─ GsCoreAIAgent.run(user_message, ev, bot)
                       ├─ extract_history() 截断 + 孤儿清理
                       ├─ 工具池组装（保底+语境+查询）
                       ├─ pydantic_ai.Agent(...).iter() 多轮
                       │     ├─ ToolCallPart → log_tool_call
                       │     ├─ TextPart → 出戏预检 → send_chat_result（被 ev/bot 兜底）
                       │     └─ ToolReturnPart → RM 注册 + log_tool_return
                       ├─ self.history.extend(result.new_messages())
                       └─ session_logger.log_run_end + token_usage
```

### B. 主动回复（统一闭包）

```
触发源（Heartbeat / ScheduledTask / Kanban / 工具）
   │
   └─→ 生成 message（可能跑一个 SubAgent，写 generator_log_files）
         │
         └─→ emit_proactive_message(event, message, source, trigger_reason, generator_log_files)
              ├─ C8 防撞车判定
              ├─ send_chat_result(bot, message, extra_metadata={...})
              │     └─ _Bot.target_send 内部 add_message（proactive=True）
              ├─ session_registry.get_ai_session(event.session_id)
              │     ├─ session.append_proactive_assistant_turn(...) → pydantic_ai history + proactive_emission entry
              │     └─ session._session_logger.link_agent("proactive_generator", log_file=...)
              └─ dispatcher.register_send(target_key, legacy_source, summary)
```

---

## 重要不变式

| 不变式 | 检查方法 |
|---|---|
| 一条主动消息在 `message_history` 只落一次 | grep `metadata.proactive=true` 行数 == 实际发送次数 |
| 一条主动消息在主 session_logger 必有一条 `proactive_emission` | 抽样巡检 `data/ai_core/session_logs/<sid>_*.json` |
| 决策 / 转译 / 执行体子 agent 必有独立 subagent log，并出现在主 session 的 `linked_agents` 里 | `data/ai_core/session_logs/subagents/` 文件数 ≈ 触发次数 |
| 主 session 的 `self.history` 内必含上一条主动 turn | mock LLM，断言 `_agent.iter(message_history=...)` 入参含 `ModelResponse(TextPart=<上次主动消息>)` |
| ScheduledTask 不污染主 session 的 user_input | 跑一次定时任务，断言主 session 的 `AISessionLogger.entries` 不含 "【定时任务执行】" 类 user_input |
| 任何提到的工具 / flag / table 都在代码里真实存在 | grep / 跑 `pytest`（数据库表名严格全小写无下划线） |

---

## 相关上层文档

- `docs/LLM.md` — 代码红线（**写代码前先读**）。
- `docs/skills/gscore-development/SKILL.md` — 框架开发指南（触发链路 / 模块全景 / 已知坑）。
- `docs/AGENT_MESH_COLLABORATION_PROPOSAL_20260521.md` — Kanban Agent Mesh 设计稿。
- `plans/proactive_message_session_unification_20260529.md` — 主动消息统一会话/日志方案（**本次改造的源头**）。
- `gsuid_core/ai_core/rag/README.md` / `persona/README.md` / `scheduled_task/README.md` / `buildin_tools/README.md` — 各子模块 README。
