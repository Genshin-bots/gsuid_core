# 二、启动时序与生命周期钩子

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[一、架构与模块全景](./01-architecture-and-modules.md) · **下一章**：[三、插件加载与配置系统](./03-plugin-loading-and-config.md)

本章讲"东西是按什么顺序起来的"。改启动相关逻辑（加初始化步骤、改钩子顺序、动 AI 子系统
初始化）前必读，否则极易踩"在连接建立前/后该做什么"的边界。

## 2.1 启动入口 `core.py::main()`

```
python -m gsuid_core → asyncio.run(main())
```

`main()` 同步阶段（在 `await server.serve()` 之前，全部阻塞顺序执行）：

1. 解析 CLI 参数 `--dev` / `--port` / `--host`
2. 切换 HuggingFace 镜像源（`os.environ["HF_ENDPOINT"]`）
3. `await init_database()` —— 建 SQLite + 初始化基础表
4. `await load_gss(args.dev)` —— 加载所有插件 + 触发模块级 `@sv` / `@ai_tools` 装饰器
5. `import gsuid_core.ai_core.startup` —— **注册唯一** `init_ai_core` 钩子（仅注册不执行）
6. 构造 FastAPI app（启动/关闭 hook 在 `app_life.lifespan` 里跑）
7. 注册 `@app.websocket("/ws/{bot_id}")` + 可选 `@app.post("/api/send_msg")`
8. 装 SIGINT/SIGTERM 信号处理（Windows 无 `add_signal_handler`，依赖 uvicorn）
9. `uvicorn.Server(config).serve()` —— 由 lifespan 在内部跑启动 hook

> **Windows 事件循环策略**：`core.py` **不设置**任何事件循环策略——它 `asyncio.run(main())`
> 并在内部 `await server.serve()`，而 uvicorn 的 loop factory 只在 `Server.run()` 里生效。
> 所以 Windows 上是 Python 默认的 **`ProactorEventLoop`**，`asyncio.create_subprocess_exec`
> **可用**。Proactor 关 socket 的 `InvalidStateError` 由 `core.py:291-297` 顶层 `except` 兜底。
> （2026-08-14 更正：此前写「切到 `WindowsSelectorEventLoopPolicy` 故不支持子进程」已过期，
> 详见 [§12.3](./12-developer-pitfalls.md)。）

## 2.2 两阶段启动钩子（核心设计）

启动钩子分两阶段，分界线是 **WS 服务是否已开始接受连接**：

| 阶段 | 钩子 | 执行时机 | 用途 | 是否阻塞连接 |
|------|------|----------|------|-------------|
| 一 | `on_core_start_before` | WS 启动**前** | DB 迁移、建表、Schema 升级、全局变量加载 | ✅ 阻塞 |
| 二 | `on_core_start` | WS 启动**后** | AI 子系统初始化等耗时操作 | ❌ 后台 task |
| 关闭 | `on_core_shutdown` | `lifespan` yield 之后 | 落盘、停 worker、清理 | 按优先级并发 |

**注册 API**（`gsuid_core/server.py`）：

```python
def on_core_start_before(func=None, /, priority: int = 0): ...  # WS 前阻塞，优先级升序分组并发
def on_core_start(func=None, /, priority: int = 0): ...         # WS 后台异步
def on_core_shutdown(func=None, /, priority: int = 0): ...      # 关闭
```

> **优先级语义**：数值**越小越先执行**；同优先级 `asyncio.gather` 并发。`on_core_start_before`
> 必须全部完成才开始接受连接。

### 阶段一：`on_core_start_before`

| 钩子 | 模块 | 优先级 | 功能 |
|------|------|--------|------|
| `move_database` | `utils/database/startup.py` | -100 | 旧 `GsData.db` 迁移到新路径 + 清 `global_val/*.json` |
| `create_core_tables` | `utils/database/startup.py` | -90 | 建核心表（**AI 总开关关闭时跳过 AI 表**） |
| `trans_adapter` | `utils/database/startup.py` | -80 | 执行 ALTER / CREATE INDEX 等 Schema 升级 |
| `load_global_val` | `buildin_plugins/.../command_global_val.py` | 0 | 加载 Bot 流量统计 / QPS 配置 |

> 给已部署用户**补数据库列**（Schema 升级）走这个阶段：插件用 `exec_list`，框架用
> `trans_adapter`。绝不能在运行期临时 ALTER。

### 阶段二：`on_core_start`

| 钩子 | 模块 | 优先级 | 功能 |
|------|------|--------|------|
| `_start_rm_cleanup` | `utils/resource_manager.py` | 10 | 启动 ResourceManager 资源清理 worker（图片/文件 RM） |
| `init_ai_core` | `ai_core/startup.py` | 0 | **AI 核心统一入口**，按 `_INIT_STEPS` 顺序串行初始化各 AI 子系统 |

> 插件可继续用 `@on_core_start(priority=N)` 注册自己的启动钩子，同优先级会与
> `init_ai_core` 并发。

## 2.3 `init_ai_core` 子系统初始化顺序（`_INIT_STEPS`）

AI 子系统**不再各自** `@on_core_start`，而是由 `ai_core/startup.py` 注册唯一一个
`init_ai_core()`。它先把 AI 重依赖（`handle_ai` / `buildin_tools` 会拉
`sklearn` / `sentence-transformers`）通过 `asyncio.to_thread(_import_ai_heavy_deps)`
在独立线程同步 import（避免冻住 loop），然后**按顺序串行**执行下列步骤；**单步异常被
`try/except` 兜住并继续下一步**：

| # | 名称 | 实现 | 主要工作 |
|---|------|------|----------|
| 1 | **Agent 套件** | `kits/__init__.py::load_enabled_kits()` | 按 `kit_slots.*` 装载 18 个第一方套件（**只挂 hook**，重依赖在 hook 体内懒导入，实测 0.14s） |
| 2 | RAG | `rag/startup.py::init_all()` | 预下载模型 → 线程内初始化嵌入模型 → 建 tools/knowledge/image collection → 同步工具/知识/图片 |
| 3 | Persona | `persona/startup.py` | 写默认 Persona「早柚」+ 迁移旧配置 |
| 4 | 审批中心 | `ai_core/approval` | 注册内置审批类别 + 预热 pending |
| 5 | 定时任务 | `scheduled_task/startup.py` | `reload_pending_tasks()` 把 DB 中 pending 任务挂回 APScheduler |
| 6 | 长任务编排 | `planning/startup.py` | 注册 Kanban 工具 + 内置/用户 Capability 画像 + 僵尸子任务恢复 + 每日 04:00 Artifact TTL 清理 |
| 7 | Memory | `memory/startup.py` | Qdrant collection 检查 → `IngestionWorker` 主循环后台 task → 多模态摄入 worker → 每周记忆生命周期 job |
| 8 | MCP 工具 | `mcp/startup.py` | 读 `mcp_configs/*.json` → 连 enabled 服务器 → 注册工具到 `_TOOL_REGISTRY["mcp"]`（仅 `register_as_ai_tools=true`） |
| 9 | Meme | `meme/startup.py` | 建 inbox/common/rejected 目录 → 表情包 collection → 打标 worker |
| 10 | 统计 | `statistics/startup.py` | `AISessionRegistry` 空闲清理循环 + Heartbeat 巡检 + 当日数据回灌 |
| 11 | MCP Server | `mcp/server.py` | 把 `_TOOL_REGISTRY` 整库通过 MCP 暴露：默认 **http 挂载主 app `/api/mcp`（同端口 8765）**；stdio 为独立 task |
| 12 | 命令执行 | `command_exec/startup.py` | 主人 shell 执行器 |

> **「Agent 套件」为什么排第一**：`AgentKit.register` 只是挂 hook，却决定了整条 agent
> loop 有没有情绪 / 关系 / 记忆 / 工具装配。它排在末尾时，**任何一个前置步骤卡住都会让
> hook 总线在整个启动窗口内空转**——实测 Meme 的一次性向量迁移占用 337 秒，期间整轮
> 群聊基准 24 例全部零工具调用、零记忆注入。这一步没有 I/O，放最前是安全的。

> **本循环没有硬超时**：单步只是每 60s 打一条 `仍在执行中` 警告，然后继续等。
> 一个卡死的步骤会无限期挡住它**后面**的所有步骤。因此顺序本身是可用性设计：
> 便宜且关键的步骤放前面。

> **加新 AI 子系统的正确姿势**：在 `_INIT_STEPS` 里加一步，**不要**自己写 `@on_core_start`。
> 步骤函数开头读 `ai_config.get_config("enable")`，关闭时 `return`。

> ⚠️ **一个子系统只能有一个初始化主**。套件的 `AgentKit.init_step` 与 `_INIT_STEPS`
> 条目**不能同时**指向同一个 bring-up：两边都挂不会报错，只会每次启动跑两遍（实测
> Meme 的 337 秒迁移被跑了两次，并强制重建了两次 Qdrant collection）。
> 有 `_INIT_STEPS` 条目的子系统，套件侧不要带 `init_step`；套件自有的 job
> （如关系温度闲置衰减）则反过来只挂在套件侧。
> `tests/test_agent_kits_slots.py::test_kit_init_steps_do_not_duplicate_core_init_steps`
> 锁死这一点。

> **认知枢纽挂载**（2026-08-16）是刻意的例外：它**不进** `_INIT_STEPS`（否则会拖 READY），
> 在 `init_ai_core` 的 `finally` 置 READY 之后 `spawn_cognition_mount()` → `create_task`。
> 顺序：插件 → 手动建枢纽 → agent 文按 `hub:{正式名}` 回挂（启动扫描不新建 `world:`）→ 环境连边。
> 没主语的索引页只计入 `skipped_unresolved`，不逐条打「无法解析正式名」。
> 套件也不得再挂一份 `init_step`。开关 `cognition_mount_enable`。见
> [`AI_AGENT_LIFECYCLE_SEQUENCE.md`](../../../../docs/AI_AGENT_LIFECYCLE_SEQUENCE.md) §15.2。
> 实机评测：Windows 必须 `NO_PROXY=localhost,127.0.0.1`，否则系统代理把本机
> Qdrant / `/api/chat_with_history` 打成 502。不要 `--dev`。

## 2.4 AI 总开关贯穿初始化

每个 `_init_*` 函数都先读 `ai_config.get_config("enable")`，关闭时直接 `return`；
`create_core_tables` 同样跳过 AI 表创建。**因此关闭 AI 时不会建任何 AI SQLite 表，也不起
任何 AI 后台任务**。

`handle_ai.py` 里 `enable_ai` 改为**函数内动态读取**（`ai_config.get_config("enable").data`），
不是模块级常量——用户在 WebConsole 切总开关后**无需重启**即生效。

> ⚠️ 改任何 AI 模块的启动钩子 / 定时任务 / 执行器时，**务必保留**这个总开关检查。历史缺陷
> D-21 就是"AI 关了但启动钩子/定时任务仍跑 AI 逻辑"。详见 [§12](./12-developer-pitfalls.md)。

## 2.5 关闭钩子（`on_core_shutdown`）

`app_life.lifespan` 在 `yield` 之后 `shutdown_event.set()` + `core_shutdown_execute()`，
按优先级分组并发跑：

| 钩子 | 模块 | 优先级 | 功能 |
|------|------|--------|------|
| `flush_ai_sessions_on_shutdown` | `ai_core/startup.py` | 0 | 强制把未达兜底间隔的 AI 会话日志落盘 |
| `_flush_user_buffer_on_shutdown` | `handler.py` | 0 | 用户写缓冲区最终刷写 |
| `save_global_val` | `core_command/.../command_global_val.py` | 0 | 持久化 Bot 流量统计 |
| `shutdown_scheduled_tasks` | `scheduled_task/startup.py` | 0 | 清理已完成 APScheduler job |
| `shutdown_meme_module` | `meme/startup.py` | 0 | 停 Meme 打标 worker |
| `shutdown_ai_core_statistics` | `statistics/startup.py` | 0 | 持久化统计数据 |
| `_on_shutdown`（MCP 工具） | `mcp/startup.py` | 5 | 清理 MCP 客户端 |
| `_on_shutdown`（MCP Server） | `mcp/server.py` | 10 | 关闭 MCP Server |
| `_stop_rm_cleanup` | `utils/resource_manager.py` | 10 | 停 RM 清理任务 |
| `shutdown_memory_system` | `memory/startup.py` | 20 | 停 IngestionWorker + 多模态 worker |

## 2.6 Web 服务启动（`lifespan` 序列）

`app_life.lifespan` 在 uvicorn 内部执行，顺序：

1. `await core_start_before_execute()` —— 阶段一钩子（`move_database` → `create_core_tables`
   → `trans_adapter` → `load_global_val`）
2. `asyncio.create_task(check_speed())` —— 后台测速选镜像源
3. `asyncio.create_task(core_start_execute())` —— **后台异步**跑全部 `@on_core_start`
   （`_start_rm_cleanup` + `init_ai_core`）
4. `asyncio.create_task(_bgsetup_frontend_b())` —— 后台准备网页控制台前端
5. `await start_scheduler()` —— 启动 APScheduler
6. `asyncio.create_task(clean_trace_collector())` —— 后台回收僵死追踪
   （旧的 `clean_log()` 已废除：日志缓冲改由条数 + 字符数上限在 append 时淘汰，
   周期性清空会抹掉网页控制台的回放积压，见 [十二、开发陷阱 §12.21](./12-developer-pitfalls.md)）
7. `yield` —— uvicorn 开始接受连接（此时 WS 已可连，**AI 子系统仍在后台逐步上线**）

> **含义**：WS 可连 ≠ AI 已就绪。AI 初始化在后台，前几秒发 AI 消息可能命中"还没起来"。
> 这是有意为之（不阻塞连接），不要为了"等 AI 起来"把 `init_ai_core` 改回阻塞式。

### WebSocket 端点要点（`core.py::websocket_endpoint`）

- **IP 访问控制**：被 ban / 不在 `TRUSTED_IPS` 时强制要求 `WS_TOKEN`；失败 `record_failure`。
- **例外：`bot_id == webconsole_livechat`**：必须持有有效控制台登录会话（query `token`），
  **不**走核心 `WS_TOKEN`，也**不**因本机 IP 而跳过鉴权。其它适配器 bot 行为不变。
- **5 分钟重连复用**：`gss.connect()` 内部启 `start_send_worker()`；断开后 `_Bot` 实例保留
  5 分钟以便重连复用（在途消息继续投递），超时才丢弃。详见 [§05](./05-bot-classes.md)。
- **两个并发协程**：`start()`（`receive_bytes` 带 1s 超时检查 `shutdown_event` → `handle_event`）
  与 `process()`（`bot._process()` 任务消费循环）。

### HTTP 端点（可选）

`ENABLE_HTTP=true` 时注册 `POST /api/send_msg`，用 `_Bot("HTTP")` 处理，通过 `send_dict`
同步返回结果（无 WebSocket）。

## 2.7 开发模式

```bash
python -m gsuid_core --dev              # 只加载 name.endswith("-dev") 的插件
python -m gsuid_core --port 8888 --host 0.0.0.0
```

`load_plugins()` 里 `if dev_mode and not plugin.name.endswith("-dev"): continue`。开发模式
插件目录命名为 `my_plugin-dev/`。
