---
name: gscore-development
description: >
  当用户要求"维护/开发 gsuid_core 框架本身"、"GsCore 是怎么启动的"、
  "消息从进来到 AI 回复经过了哪些步骤"、"触发器是怎么匹配的"、
  "handle_event / handle_ai_chat 干了什么"、"AI Session 怎么路由 / 怎么隔离"、
  "工具注册表 / 渐进式工具加载 / find_tools 是怎么回事"、
  "Heartbeat 定时巡检 / 免唤醒续聊怎么实现"、"记忆系统 / 偏好记忆 / 双路检索原理"、
  "RAG 知识库 SQL 真值源 / 嵌入 Provider"、"定时任务 / Kanban 长任务编排"、
  "启动钩子 on_core_start(_before) / init_ai_core 顺序"、"配置系统 / 配置热重载"、
  "数据库基类 / @with_session / AI 表与总开关"、"帮助系统 register_help"、
  "_Bot / Bot / MockBot 区别"、"网页控制台认证加密"、
  "改框架要注意什么 / 有哪些已知坑"时触发此 SKILL。
  凡是改动 `gsuid_core/` 框架核心（非业务插件）的任务都应优先读取此 SKILL。

  面向 **GsCore（早柚核心 / gsuid-core）框架开发者与维护者**的系统级开发指南。
  与「插件开发」「适配器开发」「AI Core API（给插件用）」三个 SKILL 不同，本 SKILL
  讲的是**框架自身的内部实现与设计约束**：项目启动时序与两阶段钩子、插件发现/加载/
  依赖安装、配置系统（CoreConfig / PluginConfigStore / SV 配置 / 热重载）、事件处理
  入口 `handler.py` 与触发器匹配、命令 vs AI 的分流、`handle_ai_chat` 全链路、
  `_Bot`/`Bot`/`MockBot` 三类 Bot、AI Session 路由与隔离、Persona 系统、工具注册表与
  渐进式装配（三层工具池 + Reranker 精排 + `find_tools` 动态暴露 + `visible_when` 条件
  隐藏）、Heartbeat 定时巡检与免唤醒续聊、定时任务与 Kanban 长任务/能力代理、记忆系统
  （双路检索 / Scope 隔离 / 摄入 / 分层图 / 偏好记忆 / RF-Mem / 生命周期 / 多模态）、
  RAG 知识库（SQL 真值源 + 对账 + 过滤下推）与嵌入 Provider、统计系统、网页控制台 API
  与认证加密、数据库基类与 AI 表 / 总开关、帮助系统，以及一份**已知坑与开发注意事项**
  清单（D-1~D-22 历史缺陷 + 续聊/偏好/多进程/事件循环等踩坑点）。
---

# GsCore 框架开发与维护指南（核心入口）

> 本 SKILL 面向**框架本身的开发者 / 维护者**，描述 `gsuid_core/` 的系统结构、触发器与
> 事件流转、AI 子系统、数据库 / 配置 / 帮助系统，以及后续开发必须注意的约束与坑点。
> 目标：让不熟悉本项目的人也能安全地改框架，不踩历史上踩过的坑。
>
> 内容按章节拆分为「主入口 + `references/` 子文档」。需要某专题细节时，顺着下表的相对
> 路径**按需** `Read` 对应文件，**不要**一次性把所有内容塞进上下文。源码永远是唯一事实源，
> 本 SKILL 是导航与设计意图说明；改动核心后请同步更新对应章节。

## 谁该读这个 SKILL（与其他 SKILL 的分工）

| 你的任务 | 该读的 SKILL |
|----------|-------------|
| **改框架核心**（handler / ai_core / 启动 / 配置 / 数据库基类 / webconsole） | **本 SKILL** |
| 写一个业务插件（命令 / 数据库表 / 配置 / 帮助 / 接 AI） | `gscore-plugin-development` |
| 查 AI Core 给插件暴露了哪些 API（签名 / 类型） | `gscore-ai-core-api` |
| 写一个平台适配器（OneBot / 协议对接） | `gscore-adapter-development` |
| 部署 / 运维 Core（安装 / 启动 / 配置文件 / Docker） | `gscore-deploy` |

## 文档目录索引

| 章节 | 主题 | 链接 |
|------|------|------|
| 一 | 架构与模块全景（`ai_core/` 目录结构、核心组件关系、消息→AI 的总链路） | [references/01-architecture-and-modules.md](./references/01-architecture-and-modules.md) |
| 二 | 启动时序与生命周期钩子（`core.py::main`、两阶段 hook、`init_ai_core` 顺序、关闭钩子、AI 总开关、Web 服务启动） | [references/02-startup-lifecycle.md](./references/02-startup-lifecycle.md) |
| 三 | 插件加载与配置系统（发现/分类/依赖合并安装/`cached_import`、`CoreConfig`/`PluginConfigStore`/`SV` 配置、配置热重载矩阵） | [references/03-plugin-loading-and-config.md](./references/03-plugin-loading-and-config.md) |
| 四 | 事件处理与触发器流转（`handle_event` 13 步、触发器匹配、命令 vs AI 分流、AI 触发条件、长度/并发防护） | [references/04-event-trigger-flow.md](./references/04-event-trigger-flow.md) |
| 五 | Bot 三类（`_Bot` 底层 / `Bot` 高层 / `MockBot` AI 代理、连接管理与 5 分钟重连复用、发送队列串行化） | [references/05-bot-classes.md](./references/05-bot-classes.md) |
| 六 | AI Session 路由与 Persona（`ai_router`、Session ID 设计、`AISessionRegistry`、内存保护、Persona 热重载 + Persona 配置系统） | [references/06-ai-session-and-persona.md](./references/06-ai-session-and-persona.md) |
| 七 | 工具注册表与 Agent 装配（`@ai_tools`/`_TOOL_REGISTRY`、三层工具池、Reranker 精排 + `find_tools` 渐进暴露 + `visible_when` 条件隐藏、主/子 Agent、MCP） | [references/07-tool-registry-and-agent.md](./references/07-tool-registry-and-agent.md) |
| 八 | 主动发言与任务编排（Heartbeat 定时巡检、免唤醒续聊软触发、Scheduled Task 定时任务、Kanban 长任务 + 能力代理 + HITL 审批） | [references/08-heartbeat-scheduled-planning.md](./references/08-heartbeat-scheduled-planning.md) |
| 九 | 记忆系统（双路检索、Scope 隔离、Observer/Ingestion、分层语义图、偏好记忆、RF-Mem 双过程、记忆生命周期、多模态摄入） | [references/09-memory-system.md](./references/09-memory-system.md) |
| 十 | RAG 知识库与嵌入（知识 SQL 真值源 + 两级对账 + 批量导入、Dense+BM25 混合检索 + 过滤下推、嵌入 Provider 抽象层） | [references/10-rag-knowledge-embedding.md](./references/10-rag-knowledge-embedding.md) |
| 十一 | 统计 / 网页控制台 / 数据库 / 帮助系统（AI Statistics、WebConsole API + 认证加密、数据库基类与 AI 表、帮助系统） | [references/11-statistics-webconsole-database.md](./references/11-statistics-webconsole-database.md) |
| 十二 | 已知坑与开发注意事项（D-1~D-22 历史缺陷复盘、`extract_json_from_text`、续聊/偏好/多进程/事件循环等踩坑清单、代码红线指针） | [references/12-developer-pitfalls.md](./references/12-developer-pitfalls.md) |

## 推荐阅读顺序（按需跳转）

1. **第一次接触框架**：先看 [一、架构与模块全景](./references/01-architecture-and-modules.md) 建立心智模型，再看 [二、启动时序](./references/02-startup-lifecycle.md) 搞清"东西是按什么顺序起来的"。
2. **改消息处理 / 触发逻辑**：看 [四、事件与触发器流转](./references/04-event-trigger-flow.md)；碰 Bot 发送 / 连接看 [五、Bot 三类](./references/05-bot-classes.md)。
3. **改 AI 链路**：先 [六、Session 与 Persona](./references/06-ai-session-and-persona.md) → [七、工具注册与 Agent 装配](./references/07-tool-registry-and-agent.md) → 按需 [八、主动发言/编排](./references/08-heartbeat-scheduled-planning.md) / [九、记忆](./references/09-memory-system.md) / [十、RAG](./references/10-rag-knowledge-embedding.md)。
4. **加配置 / 加启动逻辑 / 加数据库表 / 加帮助**：看 [三、插件加载与配置](./references/03-plugin-loading-and-config.md) 与 [十一、统计/控制台/数据库/帮助](./references/11-statistics-webconsole-database.md)。
5. **动手前必读**：[十二、已知坑与注意事项](./references/12-developer-pitfalls.md)——这一章是"别人替你踩过的坑"，改框架前过一遍能省大量返工。

## 关键概念速记（先看这一段再决定读哪一章）

- **单进程事件循环**：Core 是 FastAPI + WebSocket + APScheduler 的**单进程**服务。大量状态（续聊窗口、工具轨迹、认证密钥、Bot 实例、Session 注册表、记忆队列）是**进程内存**，多进程水平扩展会状态不共享。详见 [§12](./references/12-developer-pitfalls.md)。
- **Windows 事件循环**：`core.py` 切到 `WindowsSelectorEventLoopPolicy` 规避 Proactor 关闭 socket 的 `InvalidStateError`，但 SelectorEventLoop **不支持子进程**——跑 subprocess 的工具必须分平台分支。详见 [§08](./references/08-heartbeat-scheduled-planning.md) 与 [§12](./references/12-developer-pitfalls.md)。
- **两阶段启动钩子**：`on_core_start_before`（WS 启动**前**阻塞执行，做 DB 迁移/建表/Schema 升级）vs `on_core_start`（WS 启动**后**后台异步，不阻塞连接）。AI 子系统统一收敛到 `ai_core/startup.py::init_ai_core` 一个钩子，按 `_INIT_STEPS` 顺序串行。详见 [§02](./references/02-startup-lifecycle.md)。
- **AI 总开关贯穿全链路**：`ai_config.get_config("enable").data` 在 `handle_ai` 内**函数级动态读取**（切换无需重启）；每个 `_init_*` 与定时任务执行前都检查总开关；关闭时 `create_core_tables` 跳过建 AI 表。改 AI 模块时**务必保留**这个检查。详见 [§02](./references/02-startup-lifecycle.md)、[§12](./references/12-developer-pitfalls.md)。
- **命令优先于 AI**：`handle_event` 先匹配 `SL.lst` 触发器，**有命令匹配走触发器、无匹配才落入 AI**。权限不足（`user_pm > sv.pm`）的命令会"不匹配"从而落到 AI（AI 调同名 `to_ai` 工具时会再做一次权限检查）。详见 [§04](./references/04-event-trigger-flow.md)。
- **Session ID 群聊不含 user_id**：群聊 `…:group:{group_id}`、私聊 `…:private:{user_id}`。群内所有用户共享同一 Session 与记忆，避免"群里每个人各聊各的"。`HistoryManager` 群聊时把 `user_id` 置空保证同群共享 deque。详见 [§06](./references/06-ai-session-and-persona.md)。
- **`_Bot` ≠ `Bot`（高频混淆点）**：`_Bot`（底层连接，key 是 `WS_BOT_ID`）/ `Bot`（包 `_Bot`+`Event`，插件用）/ `MockBot`（AI 调触发器时拦截 `send` 收集返回）。`gss.active_bot` 的 key 是 `WS_BOT_ID` 不是平台 `bot_id`。详见 [§05](./references/05-bot-classes.md)。
- **保底池由 category 决定**：`self`+`buildin` 无条件加载进主 Agent，无硬编码名单；`common`/`media`/`by_trigger`/`mcp` 走向量检索按需加载；`default` 是子 Agent 专属。再叠加 Reranker 精排、`find_tools` 渐进暴露（非闲聊轮）、`visible_when` 条件隐藏。详见 [§07](./references/07-tool-registry-and-agent.md)。
- **记忆与发言决策正交**：即使 Persona 纯静默，Observer 仍在后台积累记忆。摄入门控 100% 纯规则零 LLM。`IngestionWorker` 现已回归**主事件循环后台 task**（独立线程双循环曾击穿 Proactor 导致 WS 全断，已废弃）。详见 [§09](./references/09-memory-system.md)、[§12](./references/12-developer-pitfalls.md)。
- **配置写入即时持久化 + 多数热重载**：`StringConfig.set_config` 改内存后立即 `write_config` 落盘，大多数 AI 配置"下次消息处理即生效"；`inspect_interval` 是例外（需重启该 persona 的巡检 job，代码已自动 stop+start）。详见 [§03](./references/03-plugin-loading-and-config.md)。
- **SQLModel 不写 `__tablename__`**：表名 = 类名全小写。数据库方法写在模型类里、用 `@with_session`。Schema 变更走 `on_core_start_before` 的 `exec_list`/`trans_adapter`。详见 [§11](./references/11-statistics-webconsole-database.md)。

## 关联文档（同仓库其他位置）

- 代码红线与类型规范：仓库根目录 [`docs/LLM.md`](../../LLM.md)（改任何代码前必读）
- 插件开发工作流：[`docs/skills/gscore-plugin-development/SKILL.md`](../gscore-plugin-development/SKILL.md)
- AI Core API（给插件用）：[`docs/skills/gscore-ai-core-api/SKILL.md`](../gscore-ai-core-api/SKILL.md)
- 适配器开发：[`docs/skills/gscore-adapter-development/SKILL.md`](../gscore-adapter-development/SKILL.md)
- 部署运维：[`docs/skills/gscore-deploy/SKILL.md`](../gscore-deploy/SKILL.md)
- AI 提问/答疑速查：[`docs/AI_QUESTION_FLOW_PLAYBOOK.md`](../../AI_QUESTION_FLOW_PLAYBOOK.md)
- WebConsole 后端文档：[`gsuid_core/webconsole/docs/`](../../../gsuid_core/webconsole/docs/)
