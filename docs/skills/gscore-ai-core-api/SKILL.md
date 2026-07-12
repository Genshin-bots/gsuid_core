---
name: gscore-ai-core-api
description: >
  当用户要求"AI Core 给插件提供了哪些 API"、"@ai_tools 装饰器怎么用"、
  "category='self'/'buildin'/'common'/'media'/'default' 有什么区别"、
  "怎么把已有触发器改造成 AI 工具 to_ai / MockBot / ai_return 怎么用"、
  "create_agent 怎么创建临时 Agent"、"ai_entity / ai_alias / ai_image 怎么注册"、
  "Persona / Memory / Scheduled Task / MCP / 嵌入 Provider 怎么调"、
  "ToolContext / ToolBase / KnowledgePoint / ImageEntity 是什么类型"、
  "buildin 都有哪些内置工具"、"get_registered_tools / get_all_tools 怎么查"、
  "send_meme / collect_meme / understand_image / web_search 怎么用"时触发此 SKILL。
  对所有「插件作者要对接 AI Core 提供的 API」的任务都应优先读取此 SKILL。

  GsCore（早柚核心 / gsuid-core）`gsuid_core/ai_core/` 模块的完整 API 速查手册。
  涵盖：模块导入速查（完整 import 块）、@ai_tools 装饰器签名与四种函数模式、
  工具分类系统（self/buildin/common/media/default/mcp 五类 + 保底池架构图）、
  触发器→AI 工具桥接（to_ai / MockBot / ai_return / send_message_by_ai 资源ID 机制）、
  create_agent 临时 Agent 与 GsCoreAIAgent.run() 全部参数、知识库注册（ai_entity /
  add_manual_knowledge 手动知识管理）、别名注册（含 C2 scope 变更）、
  图片实体注册（ai_image）、内置工具大全（self/buildin/common/media/default 几十个
  工具签名 + Kanban 任务编排 + Capability Agent 能力代理）、Persona 角色系统、
  Memory 记忆系统（双路检索 / observe / 摄入 worker）、Scheduled Task 定时任务
  数据模型、工具注册表查询 API（get_registered_tools / get_all_tools / ToolBase）、
  全部类型定义（ToolContext / KnowledgeBase / KnowledgePoint / ManualKnowledgeBase /
  ImageEntity / ToolBase / CheckFunc）、MCP 工具集成（Client/Config/ToolID 格式/
  call_mcp_tool）、Image Understand 图片理解、Web Search 统一搜索、Meme 表情包
  模块、嵌入 Provider 注册表、3 个完整示例 + 6 条常见问题。

  与 `gscore-plugin-development` 的区别：该 SKILL 讲「怎么写一个插件并接 AI」，
  本 SKILL 讲「AI Core 给插件暴露了哪些 API 可调用、各个 API 的完整签名/参数/
  返回值是什么」。前者是工作流指南，后者是参考手册。
---

# GsCore AI Core 插件开发者 API 速查手册（核心入口）

> 本 SKILL 是 [`docs/ai_core_api_for_plugins.md`](../../ai_core_api_for_plugins.md) 的拆分版本。
> 原文件是 2700+ 行的单文档，按章节拆分为「主入口 + `references/` 子文档」的形式。
> Agent 在需要某专题细节时，顺着下文的相对路径按需 `ReadFile` 加载对应文件，**不要**
> 一次性把所有内容塞进上下文。

## 文档目录索引

| 章节 | 主题 | 链接 |
|------|------|------|
| 一 | 模块导入速查（工具注册 / 触发器桥接 / Agent / MCP / 嵌入 / Persona / Memory / 内置工具 / RAG 完整 import 块） | [references/01-import-cheatsheet.md](./references/01-import-cheatsheet.md) |
| 二 | `@ai_tools` 装饰器（函数签名、参数表、四种函数模式、check_func 权限校验、返回值类型） | [references/02-ai-tools-decorator.md](./references/02-ai-tools-decorator.md) |
| 三 | 工具分类系统（`_TOOL_REGISTRY` 内部结构、保底池概念、Agent 调用架构、插件分类建议） | [references/03-tool-categories.md](./references/03-tool-categories.md) |
| 四 | 触发器 → AI 工具桥接（`to_ai` / `ai_return` / `MockBot` / `send_message_by_ai` / 资源ID RM 机制 / 权限检查） | [references/04-trigger-bridge.md](./references/04-trigger-bridge.md) |
| 五 | `create_agent` 与 Agent 架构（`create_agent` 签名、`GsCoreAIAgent.run()` 全参数、`get_main_agent_tools`、`handle_ai_chat`） | [references/05-create-agent.md](./references/05-create-agent.md) |
| 六 | 知识库与别名注册（`ai_entity` / `add_manual_knowledge` 手动知识管理 / `ai_alias` 含 C2 scope / `ai_image` 图片实体） | [references/06-knowledge-and-alias.md](./references/06-knowledge-and-alias.md) |
| 七 | 内置工具大全（self/buildin/common/media/default 几十个工具签名 + Kanban 任务编排 + Capability Agent 能力代理 + self_model 演化层） | [references/07-builtin-tools.md](./references/07-builtin-tools.md) |
| 八 | Persona 角色系统 + Memory 记忆系统（Persona 类、build_persona_prompt、`memory_config` / `dual_route_retrieve` / `observe`） | [references/08-persona-and-memory.md](./references/08-persona-and-memory.md) |
| 九 | Scheduled Task 定时任务（`AIScheduledTask` 数据模型） | [references/09-scheduled-tasks.md](./references/09-scheduled-tasks.md) |
| 十 | 工具注册表查询 API + 全部类型定义（`get_registered_tools` / `get_all_tools` / `ToolBase` / `ToolContext` / `KnowledgeBase` / `KnowledgePoint` / `ManualKnowledgeBase` / `ImageEntity` / `CheckFunc`） | [references/10-registry-and-types.md](./references/10-registry-and-types.md) |
| 十一 | MCP 工具集成 + Image Understand + Web Search + Meme 表情包（`MCPClient` / `MCPConfig` / `call_mcp_tool` / `understand_image` / `web_search` / `send_meme` 集成点） | [references/11-mcp-image-search-and-meme.md](./references/11-mcp-image-search-and-meme.md) |
| 十二 | 嵌入 Provider 注册表（`register_embedding_provider` / `EmbeddingProviderEntry` 字段 / 懒 import 工厂模式 / 降级策略） | [references/12-embedding-provider.md](./references/12-embedding-provider.md) |
| 十三 | 完整示例 + 常见问题（基础工具注册 / 翻译 Agent / 插件入口 / 6 条 FAQ） | [references/13-full-examples-and-faq.md](./references/13-full-examples-and-faq.md) |

## 推荐查阅流程（按需跳转）

1. **第一次接 AI Core，先看导入速查**：[一、模块导入速查](./references/01-import-cheatsheet.md) 把所有 `from gsuid_core.ai_core.xxx import ...` 的入口列全，按需复制。
2. **要写 AI 工具**：先看 [二、`@ai_tools` 装饰器](./references/02-ai-tools-decorator.md) 搞清签名 + 函数模式；选 `category` 之前必读 [三、工具分类系统](./references/03-tool-categories.md)（self/buildin/common/media/default 保底池加载机制）。
3. **要把现有触发器改造成 AI 工具**：看 [四、触发器桥接](./references/04-trigger-bridge.md)，特别注意 `MockBot` 的拦截规则与 `send_message_by_ai` 资源ID 机制。
4. **要写临时 Agent / 子任务**：看 [五、`create_agent`](./references/05-create-agent.md)。
5. **要注册知识 / 别名 / 图片**：看 [六、知识库与别名](./references/06-knowledge-and-alias.md)。
6. **要查所有内置工具的签名**：看 [七、内置工具大全](./references/07-builtin-tools.md)。
7. **要碰 Persona / Memory / 定时任务**：分别看 [八、Persona + Memory](./references/08-persona-and-memory.md) 与 [九、Scheduled Task](./references/09-scheduled-tasks.md)。
8. **要查类型 / 反射工具注册表**：看 [十、注册表 + 类型](./references/10-registry-and-types.md)。
9. **要接 MCP / 图片理解 / 网络搜索 / 表情包**：看 [十一、MCP + 图片 + 搜索 + 表情包](./references/11-mcp-image-search-and-meme.md)。
10. **要扩展 RAG 嵌入后端**：看 [十二、嵌入 Provider](./references/12-embedding-provider.md)。
11. **要参考端到端示例或 FAQ**：看 [十三、完整示例 + FAQ](./references/13-full-examples-and-faq.md)。

## 关键概念速记（先看这一段再决定读哪一章）

- **AI Core 入口模块统一前缀**：`gsuid_core.ai_core.*`，包括 `register` / `trigger_bridge` / `gs_agent` / `rag` / `mcp` / `image_understand` / `web_search` / `buildin_tools` / `persona` / `memory` / `statistics` / `scheduled_task` / `models` / `agent_node`（统一节点层）/ `capability_agents` / `approval`（统一审批中心）/ `self_cognition` / `handle_ai`。详见 [§1](./references/01-import-cheatsheet.md)。
- **`@ai_tools` 是入口装饰器**：被装饰的函数**必须是 async**；第一个参数支持三种上下文模式（`RunContext[ToolContext]` 推荐 / `ToolContext` / 无上下文）；参数类型注解为 `Bot` / `Event` 的会被自动注入且**不暴露给 LLM**。详见 [§2.3](./references/02-ai-tools-decorator.md)。
- **`category` 决定加载方式**：`self` / `buildin` 是**框架保底工具池**（无条件全部加载进主Agent，不受向量搜索影响）；`common` / `media` / `mcp` / 自定义分类是**向量检索按需加载**；`default` 是**子Agent 工具**（需通过 `create_subagent` 调用）。详见 [§3.2](./references/03-tool-categories.md)。
- **保底池由 category 决定，无硬编码名单**：`get_main_agent_tools()` 把 `self` + `buildin` 两个分类下的全部工具无条件加载；插件若希望某工具成为主 Agent 保底工具，注册时用 `category="buildin"` 即可。详见 [§3.2](./references/03-tool-categories.md)。
- **`to_ai` 与 `@ai_tools` 冲突不可共存**：同一函数**只能选其一**。命令同时允许用户直接触发 → `to_ai`（一份代码服务用户命令 + AI 调用）；纯 AI 内部工具 → `@ai_tools`。详见 [§四 顶部警告](./references/04-trigger-bridge.md)。
- **`MockBot` 拦截 `bot.send`**：AI 调用触发器时，真实 `Bot` 被 `MockBot` 代理——`bot.send(bytes)` / `bot.send("base64://...")` / `bot.send(Message(type="image"))` 都会通过 `RM.register()` 注册图片，**返回资源 ID**（如 `img_a1b2c3d4`），AI 据此决定是否调 `send_message_by_ai(image_id=...)` 发给用户；纯文字 `bot.send(str)` 被自动收集返回给 AI。详见 [§4.4](./references/04-trigger-bridge.md)。
- **`context_tags` 解决群聊语境失配**：插件可通过 `context_tags=["原神", "Genshin", "游戏"]` 声明工具适用语境，框架根据群组画像把匹配标签的工具自动加入该群（最多 8 个），不依赖向量搜索。详见 [§2.2](./references/02-ai-tools-decorator.md)。
- **`create_agent` 复用 Agent 实例**：在模块级调用一次，拿到 `GsCoreAIAgent` 后反复 `.run(user_message=...)`；支持 Pydantic 结构化输出（`output_type=BaseModel`）。详见 [§5.1](./references/05-create-agent.md)。
- **`ai_entity` 自动同步 vs `add_manual_knowledge` 手动管理**：前者启动时自动同步到向量库、`_hash` 检测增量更新；后者不自动同步、需手动调向量库 API。详见 [§6.2](./references/06-knowledge-and-alias.md)。
- **`ai_alias` 已接入记忆摄入链路（C2）**：注册的别名在实体抽取时作为"本群已知别名"注入提取提示词，指导 LLM 把别名对齐到正式名；检索期用于查询展开与动态实体链接消歧。`scope="Genshin"` 等可隔离跨游戏同名别名。详见 [§6](./references/06-knowledge-and-alias.md)。
- **Kanban 是事件驱动的多步任务编排**：主Agent 调 `evaluate_agent_mesh_capability` 评估画像覆盖 → 调 `register_kanban_task` 创建根 + N 子任务；每个子任务派给对应画像（`agent_profile`）。真实 `task_id` 不暴露给 LLM，靠自然语言句柄 + 框架解析。详见 [§7.7](./references/07-builtin-tools.md)。
- **能力代理（Capability Agent）= 无人格专职执行者**：主人格只识别派发 / 查进度 / 转译汇报，执行交给画像（`research_agent` / `code_agent` 等 6 个内置 + 插件业务画像）。`create_subagent(agent_profile="...")` 也支持即时单步委派。详见 [§7.8](./references/07-builtin-tools.md)。
- **`self_model` 演化层 4 字段**：`commitments` / `preferences_learned` / `recurring_topics` / `self_notes`。**2026-07 O-3 缓存优化后注入拆两半**：`build_self_cognition_context(bot_id, scope_key, include_relationship=False)` 产出的 self_model 自述块（慢变、bot/scope 级）由 `ai_router` 在**建 session 时固化进 system_prompt 稳定前缀**（跨轮命中 provider 缓存，按 TTL 刷新），per-user 的当前对话者关系行改由 `build_relationship_context(user_id, favorability)` **每轮注入用户消息侧**。签名变更：`build_self_cognition_context` 的 `user_id` 现可选（`include_relationship=False` 时不需要），新增 `include_relationship` 参数（默认 True，保持旧插件调用兼容）。详见 [§7.9](./references/07-builtin-tools.md) 与 `gscore-development` §6.7.1。
- **MCP 工具 ID 格式**：`{mcp_id} - {tool_name}`，如 `minimax - web_search`；可用 `parse_mcp_tool_id` / `format_mcp_tool_id` 解析与组装。详见 [§11.1](./references/11-mcp-image-search-and-meme.md)。
- **Web Search 统一接口**：`web_search()` 根据 `ai_config.websearch_provider` 自动选 Tavily / Exa / MCP；用 MCP 时需配置 `mcp_tools_config.websearch_mcp_tool_id`。详见 [§11.2](./references/11-mcp-image-search-and-meme.md)。
- **Meme 表情包集成点**：`handler.py` 中通过 `asyncio.create_task(observe_message_for_memes(event))` 异步采集；`handle_ai.py` 中导入 `meme.startup` 和 `meme_tools` 触发 `@on_core_start` 钩子与 `@ai_tools` 注册。详见 [§11.3](./references/11-mcp-image-search-and-meme.md)。
- **嵌入 Provider 懒 import 工厂模式**：插件 `__init__.py` 顶层调 `register_embedding_provider` 注册 `EmbeddingProviderEntry`；重依赖（如 `torch` / `sentence_transformers`）只能在 `factory` 内部 import；配置指向的 provider 不可用时框架**自动降级回 local** 并记录 error，不会让 AI 核心整体挂掉。详见 [§12](./references/12-embedding-provider.md)。

## 关联文档（同仓库其他位置）

- 插件开发工作流指南：[`docs/skills/gscore-plugin-development/SKILL.md`](../gscore-plugin-development/SKILL.md)
- 适配器开发指南：[`docs/skills/gscore-adapter-development/SKILL.md`](../gscore-adapter-development/SKILL.md)
- AI Agent 总架构：[`docs/AI_AGENT_ARCHITECTURE.md`](../../AI_AGENT_ARCHITECTURE.md)
- AI 触发流程 / 框架开发：[`docs/skills/gscore-development/SKILL.md`](../gscore-development/SKILL.md)
- AI Session Logging：[`docs/AI_SESSION_LOGGING.md`](../../AI_SESSION_LOGGING.md)
- 记忆系统详细：[`docs/MEMORY_SYSTEM.md`](../../MEMORY_SYSTEM.md)
- 召回与元事件：[`docs/RECALL_AND_META_EVENTS.md`](../../RECALL_AND_META_EVENTS.md)
- LLM.md（Bot 内部连接管理红线）：仓库根目录 `docs/LLM.md`
- 原单文件版（被本 SKILL 取代）：[`docs/ai_core_api_for_plugins.md`](../../ai_core_api_for_plugins.md)
