---
name: gscore-plugin-development
description: >
  当用户要求"帮我写一个 GsCore 插件"、"给这个插件加功能"、"改造触发器支持 AI"、
  "怎么用 to_ai"、"注册 ai_tools"、"写一个游戏查询插件"、"插件帮助怎么注册"、
  "能力代理/代理画像"、"怎么为触发器添加AI功能"、"几个触发器的差别在哪"、"数据库和配置项怎么添加"
  "如何把数据库表挂到网页控制台"、"PIL/htmlkit/playwright 哪个用哪个"、
  "插件怎么挂自己的 HTTP 接口"、"插件怎么注册 FastAPI 路由"时触发此 SKILL。
  对所有 GsCore Bot 插件开发任务都应优先读取此 SKILL。

  为 GsCore 机器人框架编写插件的完整指南。涵盖项目级目录规范（参照 ZZZeroUID / SayuStock）、
  Plugins/SV 双层架构、各类触发器的语义差异（on_command vs on_prefix vs on_fullmatch vs
  on_keyword vs on_regex vs on_message vs on_file）、消息收发、数据库操作并注册到网页控制台
  （site.register_admin / GsAdminModel）、订阅系统（gs_subscribe）、定时任务、配置管理、
  帮助系统（register_help + get_new_help）、推荐的渲染范式（优先 PIL → htmlkit
  → playwright 兜底）、AI 工具集成（@ai_tools、to_ai、ai_return、create_agent）、
  知识库 / 别名注册、启动钩子、to_ai 批量改造工作流、为插件挂 FastAPI 后端接口。
---

# GsCore 插件开发完整指南（核心入口）

> 本 SKILL 已按章节拆分为主入口 + `references/` 子文档的形式组织。Agent 在需要某专题细节时，
> 顺着下文的相对路径按需 `ViewFile` / `ReadFile` 加载对应文件，**不要**一次性把所有内容塞进上下文。

## 文档目录索引

| 章节 | 主题 | 链接 |
|------|------|------|
| 一 | 插件基础结构（目录、命名、入口三件套、Plugins vs SV、pyproject、资源路径） | [references/01-plugin-basics.md](./references/01-plugin-basics.md) |
| 二 | SV 与触发器（SV 实例、八种触发器语义对比、装饰器通用参数、签名规范） | [references/02-sv-and-triggers.md](./references/02-sv-and-triggers.md) |
| 三 | 消息收发（Event 属性、bot.send 各种形态、send_option、多步会话） | [references/03-messaging.md](./references/03-messaging.md) |
| 四 | 配置管理（CONFIG_DEFAULT、StringConfig、所有配置类型） | [references/04-config-management.md](./references/04-config-management.md) |
| 五 | 数据库操作（SQLModel 基类、`@with_session`、`async_maker`、注册到 Web 控制台、`exec_list` 自动迁移） | [references/05-database.md](./references/05-database.md) |
| 六 | 定时任务与订阅（APScheduler、`gs_subscribe` 全套 API） | [references/06-scheduler-and-subscribe.md](./references/06-scheduler-and-subscribe.md) |
| 七 | 启动 / 关闭 / Bot 上线钩子（4 类钩子的区别与适用场景） | [references/07-lifecycle-hooks.md](./references/07-lifecycle-hooks.md) |
| 八 | 帮助系统注册（`register_help`、`get_new_help`、`register_status`） | [references/08-help-system.md](./references/08-help-system.md) |
| 九 | 图片渲染范式（PIL → htmlkit → playwright 三档） | [references/09-image-rendering.md](./references/09-image-rendering.md) |
| 十 | AI 集成：`to_ai` 与 `ai_return`（**优先方案**） | [references/10-ai-to-ai-and-ai-return.md](./references/10-ai-to-ai-and-ai-return.md) |
| 十一 | AI 集成：`@ai_tools` 装饰器（仅当函数不暴露为用户命令时使用） | [references/11-ai-tools-decorator.md](./references/11-ai-tools-decorator.md) |
| 十二 | AI 集成：知识库（`ai_entity`）与别名（`ai_alias`）注册 | [references/12-ai-knowledge-and-alias.md](./references/12-ai-knowledge-and-alias.md) |
| 十三 | AI 集成：`create_agent`（临时专用 AI Agent） | [references/13-ai-create-agent.md](./references/13-ai-create-agent.md) |
| 十四 | AI 集成：能力代理画像（`CapabilityAgentProfile`） + 框架自带 `plugin_developer_agent` | [references/14-ai-capability-profile.md](./references/14-ai-capability-profile.md) |
| 十五 | 完整插件示例（MyGameUID 端到端） | [references/15-full-plugin-example.md](./references/15-full-plugin-example.md) |
| 十六 | 常用工具模块速查（`get_res_path` / `send_msg_to_master` / `error_reply` / 限流 / 缓存 / 字体 / `to_thread` / `cache_data` / 批量播报 / 常用 import） | [references/16-common-utilities.md](./references/16-common-utilities.md) |
| 十七 | 代码规范红线（禁止 try/except 兜底、cast、type:ignore、getattr/dict.get 兜底、同步阻塞函数） | [references/17-code-redlines.md](./references/17-code-redlines.md) |
| 十八 | to_ai 批量改造工作流（背景、Step 0~4、完整股票 / 游戏示例、质量检查清单、Q&A） | [references/18-ai-trigger-migration.md](./references/18-ai-trigger-migration.md) |
| 十九 | 为插件挂 FastAPI 后端接口（共享 app、鉴权、CRUD、命名规范、反模式） | [references/19-fastapi-plugin-api.md](./references/19-fastapi-plugin-api.md) |

## 推荐开发流程（按需跳转）

1. **新建插件**：先看 [一、插件基础结构](./references/01-plugin-basics.md) 确定目录与命名，参考 [十五、完整插件示例](./references/15-full-plugin-example.md) 起步。
2. **加命令**：看 [二、SV 与触发器](./references/02-sv-and-triggers.md) 选合适触发器，按 [三、消息收发](./references/03-messaging.md) 写发送 / 多步会话。
3. **加配置**：看 [四、配置管理](./references/04-config-management.md) 定义 `CONFIG_DEFAULT` 与 `StringConfig`。
4. **加数据库表**：看 [五、数据库操作](./references/05-database.md)；要可视化后台看 §5.5，要给已部署用户补字段看 §5.7。
5. **加定时推送**：看 [六、定时任务与订阅](./references/06-scheduler-and-subscribe.md) 的 `gs_subscribe` 强制规范。
6. **加启动逻辑**：在 [七、生命周期钩子](./references/07-lifecycle-hooks.md) 选合适的钩子。
7. **加帮助 / 状态**：看 [八、帮助系统注册](./references/08-help-system.md)。
8. **画图**：参考 [九、图片渲染范式](./references/09-image-rendering.md) 的"决策口诀"选 PIL / htmlkit / playwright。
9. **想被 AI 调用**：
   - 命令同时也是用户命令 → [十、`to_ai` 与 `ai_return`](./references/10-ai-to-ai-and-ai-return.md) **（优先）**
   - 纯数据 / 内部工具 → [十一、`@ai_tools` 装饰器](./references/11-ai-tools-decorator.md)
   - 知识库 / 别名 → [十二、知识库与别名注册](./references/12-ai-knowledge-and-alias.md)
   - 临时 Agent 子任务 → [十三、`create_agent`](./references/13-ai-create-agent.md)
   - 业务专业代理 → [十四、能力代理画像](./references/14-ai-capability-profile.md)
   - **批量改造已有触发器支持 AI** → [十八、to_ai 批量改造工作流](./references/18-ai-trigger-migration.md)
10. **挂自己的 HTTP 后端接口**：看 [十九、FastAPI 插件 API](./references/19-fastapi-plugin-api.md)——复用 `gsuid_core.webconsole.app_app.app`，3 行加一个接口。
11. **遇到 API 缓存 / 限流 / 字体 / 错误码 / 推主人 / 批量播报** 等问题：直接看 [十六、常用工具模块速查](./references/16-common-utilities.md)。
12. **写完代码**：用 [十七、代码规范红线](./references/17-code-redlines.md) 自查（try/except、cast、type:ignore、getattr 兜底、同步阻塞函数全部禁止）。

## 关键概念速记（先看这一段再决定读哪一章）

- **嵌套加载**：`外层 __init__.py` + `外层 __nest__.py`（空文件） + `内层 __init__.py` 声明 `Plugins(...)` + `内层 __full__.py`（空文件）。详见 [一、插件基础结构 §1.2](./references/01-plugin-basics.md#12-入口三件套)。
- **Plugins vs SV**：插件级 vs 服务模块级；`SV` 自动从调用栈推断归属。详见 [§1.3](./references/01-plugin-basics.md#13-plugins-vs-sv-的层级关系)。
- **触发器选择**：`on_command`（推荐默认）vs `on_prefix`（强制带参）vs `on_fullmatch`（精确匹配）vs `on_keyword`（污染消息流，慎用）vs `on_regex`（复杂结构）vs `on_file` / `on_message`（特殊）。详见 [§2.2](./references/02-sv-and-triggers.md#22-触发器语义速查)。
- **`to_ai` vs `@ai_tools` 二选一**：同一函数不可同时用。命令也允许用户直接触发 → `to_ai`；纯 AI 内部工具 → `@ai_tools`。详见 [§10 顶部警告](./references/10-ai-to-ai-and-ai-return.md) 与 [§11 顶部警告](./references/11-ai-tools-decorator.md)。
- **主动推送必须用 `gs_subscribe`**：不要 `for bot in gss.active_bot.items(): await bot.target_send(...)` 硬塞群号。详见 [§6.2](./references/06-scheduler-and-subscribe.md#62-主动推送强制规范)。
- **数据库 Schema 变更用 `exec_list`**：放在 `on_core_start_before` 阶段执行。详见 [§5.7](./references/05-database.md#57-为已定义的表添加新列)。
- **唯一允许 `try/except` 的地方**：`_ai_return_xxx()` 辅助函数。详见 [§17.3](./references/17-code-redlines.md#173-ai_return-辅助函数的特殊说明)。
- **图片渲染优先级**：PIL（首选）→ htmlkit（推荐）→ playwright（兜底）。详见 [§9.1](./references/09-image-rendering.md#91-三档渲染方案优先级从高到低)。
- **能力代理 prompt 必须拼 `_DELIVERY_BOUNDARY`**：否则画像会绕过主人格直接发消息。详见 [§14.4](./references/14-ai-capability-profile.md#141-画像-prompt-写作要点硬约束)。
- **`to_ai` 改造三层**：触发器层 `to_ai="..."` + 数据/渲染层 `ai_return()` + 业务画像 `CapabilityAgentProfile`；详见 [§18.1](./references/18-ai-trigger-migration.md#181-背景你要做的事) 与 [§18.3 Step 0.4](./references/18-ai-trigger-migration.md#step-04-判断是否需要注册-capability-agent-画像)。
- **`ai_return` 注入点 = 数据已拿到 / 图片未生成**：必须在数据层函数里，不能只在触发器层。详见 [§18.3 Step 3](./references/18-ai-trigger-migration.md#step-3逐层分析调用链找出数据层注入-ai_return)。
- **插件 FastAPI = 共享 app + `Depends(require_auth)`**：从 `gsuid_core.webconsole.app_app import app` 即可挂自己的 `/api/<插件名>/...` 路由；详见 [§19.2](./references/19-fastapi-plugin-api.md#192-最简示例3-行代码加一个-get-接口) 与 [§19.3](./references/19-fastapi-plugin-api.md#193-加鉴权推荐-复用-require_auth)。

## 关联文档（本 SKILL 文件夹内）

- 触发器 → AI 迁移工作流：[§18、to_ai 批量改造](./references/18-ai-trigger-migration.md)
- 插件挂后端 API：[§19、FastAPI 插件 API](./references/19-fastapi-plugin-api.md)

## 关联文档（同仓库其他位置）

- AI Agent 总架构：[`docs/AI_AGENT_ARCHITECTURE.md`](../../AI_AGENT_ARCHITECTURE.md)
- AI 触发流程：[`docs/AI_TRIGGER_FLOW.md`](../../AI_TRIGGER_FLOW.md)
- LLM.md（Bot 内部连接管理红线）：仓库根目录 `docs/LLM.md`
- AI Core API（给插件用）：[`docs/ai_core_api_for_plugins.md`](../../ai_core_api_for_plugins.md)
- WebConsole 后端 API 设计：[`gsuid_core/webconsole/docs/README.md`](../../../gsuid_core/webconsole/docs/README.md)
