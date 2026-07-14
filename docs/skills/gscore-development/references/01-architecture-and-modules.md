# 一、架构与模块全景

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **下一章**：[二、启动时序与生命周期](./02-startup-lifecycle.md)

本章给出 GsCore 的整体心智模型：它是什么、由哪些模块组成、一条消息从进来到 AI 回复
要走哪些环节。后续每一章都是对这里某个环节的展开。

## 1.1 GsCore 是什么

GsCore（早柚核心 / gsuid-core）是一个 **FastAPI + WebSocket + APScheduler** 的**单进程**
服务：

- **不是 Bot**：Core 自己不连任何聊天平台。各平台适配器（NoneBot2 / OneBot / AstrBot…）
  以 **WS 客户端**身份连上 Core 的 `/ws/{bot_id}`，把平台消息转成 `MessageReceive` 发进来，
  再把 Core 的回复发回平台。
- **插件宿主**：业务能力（原神查询、股票、游戏…）都是 `gsuid_core/plugins/` 下的插件，
  启动时被发现、装依赖、import，模块级 `@sv.on_xxx` 装饰器把触发器注册进全局表。
- **AI 中枢**：`ai_core/` 是一整套 Agent 系统（Persona、工具、记忆、RAG、定时巡检、
  长任务编排…），在**没有命令匹配**时接管消息。

> **关键含义**：因为是单进程，几乎所有运行时状态都是**进程内存**。多实例水平扩展不共享
> 状态（见 [§12](./12-developer-pitfalls.md)）。改框架时默认"只有一个进程、一个事件循环"。

## 1.2 顶层目录速览

```
gsuid_core/
├── core.py            # 进程入口 main()：建库 → 载插件 → 注册 AI 钩子 → 起 uvicorn
├── app_life.py        # FastAPI lifespan：两阶段启动钩子 + 关闭钩子的实际执行点
├── server.py          # GsServer 单例：插件加载、依赖安装、cached_import、连接管理、钩子注册表
├── gss.py             # gss = GsServer() 全局单例 + load_gss()
├── handler.py         # handle_event()：消息事件处理总入口（命令匹配 + AI 分流）
├── bot.py             # _Bot（底层）/ Bot（高层）/ 发送队列
├── sv.py              # Plugins / SV：插件与服务模块的双层注册，触发器装饰器
├── models.py          # Event / MessageReceive / MessageSend 等数据模型
├── config.py          # CoreConfig / PluginConfigStore：配置系统
├── aps.py             # APScheduler scheduler 单例
├── logger.py          # Logger 封装
├── utils/             # database/ image/ api/ plugins_config/ upload/ resource_manager …
├── webconsole/        # 网页控制台后端（FastAPI 路由 + 鉴权 + 各业务 API）
├── plugins/           # 用户插件目录
├── buildin_plugins/   # 内置插件（core_command 等）
└── ai_core/           # AI 子系统（见下）
```

## 1.3 `ai_core/` 模块结构

```
gsuid_core/ai_core/
├── __init__.py          # 核心初始化
├── startup.py           # 唯一 AI 启动钩子 init_ai_core（按 _INIT_STEPS 串行初始化各子系统）
├── handle_ai.py         # AI 聊天处理入口 handle_ai_chat（双层长度防护 + 意图 + 记忆 + run）
├── ai_router.py         # Session 路由（get_ai_session / Persona 热重载检测）
├── session_registry.py  # AISessionRegistry：GsCoreAIAgent 对象注册表 + 空闲清理
├── gs_agent.py          # GsCoreAIAgent：Agent 实现、工具装配、_prepare_user_message 图片处理
├── register.py          # @ai_tools 装饰器 + _TOOL_REGISTRY 工具注册表 + visible_when + ai_alias/ai_entity
├── entity_index.py      # 实体身份索引 surface(正式名/别名)→插件，供 L0 实体路由确定性定插件（见 [§7.3b](./07-tool-registry-and-agent.md)）
├── models.py            # ToolContext / ToolBase / 数据模型（含 dynamic_tool_names）
├── dynamic_toolset.py   # RetrievableToolset：pydantic-ai 运行时动态工具集（find_tools 闭环）
├── trigger_bridge.py    # 触发器→AI 工具桥接（MockBot / ai_return / send_message_by_ai）
├── followup_window.py   # 免唤醒续聊软触发窗口（进程内存 + TTL 惰性清理）
├── utils.py             # extract_json_from_text / send_chat_result / SILENCE_MARKERS …
├── self_cognition.py    # 自我认知 self_model 演化层（自述块随 session 进稳定前缀；关系行每轮注入，见 §06）
├── interaction_scaffold.py  # 交互脚手架 C-1~C-3：省略跟进/漂移预算/寻址前置门 + extract_message_body（见 §12.22d）
├── context_assembly.py  # 上下文装配共享层：session system prompt + 每轮动态注入的唯一定义（生产/评测端点同源，见 §06/§11）
├── configs/             # ai_config 全局配置 + 配置数据模型
├── buildin_tools/       # 内建 AI 工具（见 §07）
├── skills/              # 运行时 Skill 系统（list_skills / run_skill_script + install_skill 统一安装/热重载）
├── classifier/          # 意图分类器（闲聊/工具/问答）
├── persona/             # Persona 角色系统（config/processor/prompts/mood/group_context）
├── heartbeat/           # 定时巡检（inspector + decision + dispatcher 主动消息网关）
├── scheduled_task/      # 定时任务（models/executor/scheduler/startup）
├── planning/            # Kanban 长任务编排层（任务树三表 + 执行器 + Artifact Hub）
├── capability_agents/   # 能力代理层（无人格专职执行体 + 画像注册表）
├── memory/              # 记忆系统（scope/observer/ingestion/retrieval/vector/lifecycle/prompts）
├── rag/                 # RAG 知识库（base/embedding/knowledge/chunking/reranker/image_rag/tools）
├── statistics/          # AI 统计（manager/models/dataclass_models/startup）
├── mcp/                 # MCP 工具集成（client/config_manager/server/startup）
├── multimodal/          # 多模态（asr/tts/video/document 提取）
├── image_understand/    # 统一图片理解接口（MCP / 原生多模态）
├── web_search/          # 统一 Web 搜索（Tavily/Exa/MCP 三选一）
├── meme/                # 表情包模块（采集/打标/检索/发送）
└── database/            # AI 通用数据库模型
```

> 完整的子模块职责在各专题章里展开。这里只需要建立"哪个东西在哪个目录"的索引感。

## 1.4 核心组件关系（消息 → AI 的总链路）

```
平台适配器 (WS 客户端)
      │  发 MessageReceive
      ▼
core.py::websocket_endpoint  ──►  _Bot._process()  ──►  handler.py::handle_event()
                                                              │
       ┌──────────────────────────────────────────────────────┤
       │ 1. 全局开关 / 黑名单 / 屏蔽列表 / 冷却                 │
       │ 2. msg_process() 解析为 Event                          │
       │ 3. 记历史 + 主人识别 + 用户/群入库 + session_id        │
       │ 4. 重复消息 / 相同消息冷却                              │
       │ 5. 触发器匹配 _check_command(SL.lst)                   │
       └───────────────┬───────────────────────────────────────┘
              有命令匹配 │                 │ 无命令匹配（或权限不足）
                        ▼                 ▼
            执行 trigger.func(Bot)   handle_ai.py::handle_ai_chat(Bot, Event)
            （插件命令）                  │ 并发信号量 + 双层长度防护
                                          │ 意图分类 + get_ai_session + 记忆检索
                                          ▼
                                  GsCoreAIAgent.run()  ──► pydantic-ai Agent
                                          │ 装配工具（保底池+检索+动态）
                                          │ LLM 多轮工具调用
                                          ▼
                                  send_chat_result(Bot)  ──► 回复发出 + observe 入队记忆
```

**两条主动链路**（不由用户消息触发）：

- **Heartbeat 定时巡检**：APScheduler 每 `inspect_interval` 分钟唤醒，遍历活跃会话，
  LLM 决策"要不要主动说话"。见 [§08](./08-heartbeat-scheduled-planning.md)。
- **Scheduled Task 定时任务 / Kanban 长任务**：用户预约的未来任务到点由 APScheduler /
  事件驱动执行器唤醒主人格执行。见 [§08](./08-heartbeat-scheduled-planning.md)。

## 1.5 几条贯穿全局的设计原则

1. **完全异步**：所有可能阻塞的路径都是 `async def`；CPU 密集运算（KMeans / BM25 /
   fastembed）放进专用 `ThreadPoolExecutor` 或 `to_thread`，绝不在事件循环里同步跑。
2. **绝不打断主链路**：解析 LLM 自由文本 / Qdrant payload / 外部不可信输入处按需保留
   `try/except` 兜底（这是 `docs/LLM.md` §1.1 的少数例外），保证一个子系统异常不拖垮
   整条消息处理。
3. **AI 总开关一票否决**：AI 关闭时不建 AI 表、不起 AI 后台任务、不进 AI 链路。
4. **配置即时持久化**：WebConsole 改配置立即落盘，绝大多数下次消息处理即生效。
5. **源码是唯一事实源**：文档（含本 SKILL）描述设计意图与导航；改了核心逻辑要回头同步。
