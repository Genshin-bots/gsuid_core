# 启动钩子系统变更日志

> 变更日期: 2026-05-01

## 一、变更概述

将原有的单一 `on_core_start` 启动钩子系统拆分为两个阶段，优化启动速度：

1. **`on_core_start_before`** — 在 WS 服务启动**之前**阻塞执行
2. **`on_core_start`** — 在 WS 服务启动**之后**作为后台任务异步执行

**核心动机**: 原先 `await core_start_execute()` 会阻塞整个启动流程，导致 WS 连接必须等待所有初始化（包括 RAG 模型下载、Memory 系统初始化等耗时操作）完成后才能建立。通过拆分为两个阶段，WS 服务可以更快地开始接受连接。

---

## 二、新增 API

### 2.1 `on_core_start_before` 装饰器

```python
# gsuid_core/server.py

def on_core_start_before(
    func: Optional[Callable] = None,
    /,
    priority: int = 0,
):
    """注册在 WS 服务启动之前执行的钩子函数。

    用于数据库迁移、全局变量加载等必须在连接建立前完成的操作。
    与 on_core_start 不同，此钩子会阻塞 WS 服务启动，确保执行完毕后才开始接受连接。
    """
```

**使用方式**（与 `on_core_start` 完全一致）：

```python
from gsuid_core.server import on_core_start_before

@on_core_start_before
async def my_before_hook():
    # 必须在 WS 连接前完成的操作
    ...

@on_core_start_before(priority=5)
async def my_before_hook_with_priority():
    # 带优先级的启动前钩子
    ...
```

### 2.2 `core_start_before_execute()` 函数

```python
# gsuid_core/server.py

async def core_start_before_execute():
    """执行 WS 服务启动前的钩子函数（阻塞式，必须全部完成后才启动 WS）"""
```

---

## 三、迁移的钩子函数

以下钩子函数从 `on_core_start` 迁移到 `on_core_start_before`：

| 钩子函数 | 模块 | 迁移原因 |
|----------|------|----------|
| `move_database()` | `gsuid_core/utils/database/startup.py` | 数据库文件迁移，必须在任何 DB 访问前完成 |
| `trans_adapter()` | `gsuid_core/utils/database/startup.py` | 数据库 Schema 迁移（ALTER TABLE / CREATE INDEX），必须在消息处理前完成 |
| `load_global_val()` | `gsuid_core/buildin_plugins/core_command/core_status/command_global_val.py` | 加载全局变量，命令处理依赖 |

---

## 四、保持在 `on_core_start` 的钩子函数

以下钩子函数保持不变，继续作为后台任务异步执行：

| 钩子函数 | 模块 | 优先级 | 功能 |
|----------|------|--------|------|
| `init_all()` | `ai_core/rag/startup.py` | 0 | RAG 模块初始化（模型下载、向量库同步） |
| `init_default_personas()` | `ai_core/persona/startup.py` | 0 | 默认角色初始化 |
| `init_memory_system()` | `ai_core/memory/startup.py` | 5 | 记忆系统初始化 |
| `_on_start()` | `ai_core/mcp/startup.py` | 5 | MCP 工具注册 |
| `init_meme_module()` | `ai_core/meme/startup.py` | 5 | 表情包模块初始化（目录创建、Qdrant Collection、打标 worker） |
| `init_ai_core_statistics()` | `ai_core/statistics/startup.py` | 10 | AI 统计系统初始化 |
| `_on_start()` (MCP Server) | `ai_core/mcp/server.py` | 10 | MCP Server 启动（将 to_ai 触发器暴露为 MCP 服务） |
| `init_scheduled_tasks()` | `ai_core/scheduled_task/startup.py` | 0 | 定时任务初始化 |
| 各插件 `all_start()` | `plugins/*/` | 0 | 插件资源下载/生成 |

---

## 五、修改的文件清单

| 文件 | 变更内容 |
|------|----------|
| `gsuid_core/server.py` | 新增 `core_start_before_def` 集合、`on_core_start_before()` 装饰器、`core_start_before_execute()` 函数 |
| `gsuid_core/app_life.py` | 导入 `core_start_before_execute`，在 `lifespan` 中先 `await core_start_before_execute()` 再 `asyncio.create_task(core_start_execute())` |
| `gsuid_core/utils/database/startup.py` | `move_database()` 和 `trans_adapter()` 从 `@on_core_start` 迁移到 `@on_core_start_before` |
| `gsuid_core/buildin_plugins/core_command/core_status/command_global_val.py` | `load_global_val()` 从 `@on_core_start` 迁移到 `@on_core_start_before` |
| `docs/startup.md` | 更新启动时序图、钩子系统说明、启动检查清单 |

---

## 六、启动流程对比

### 变更前

```
main()
  ├── init_database()
  ├── load_gss() → 插件加载 + 所有 on_core_start 钩子
  ├── uvicorn.Server.serve() → WS 服务启动
  └── lifespan:
        ├── await core_start_execute()  ← 阻塞！等待所有钩子完成
        ├── setup_frontend_b()
        ├── start_scheduler()
        └── yield
```

### 变更后

```
main()
  ├── init_database()
  ├── load_gss() → 插件加载（注册钩子，不执行）
  ├── uvicorn.Server.serve() → WS 服务启动
  └── lifespan:
        ├── await core_start_before_execute()  ← 阻塞，但只执行轻量级操作
        ├── asyncio.create_task(core_start_execute())  ← 后台异步，不阻塞
        ├── setup_frontend_b()
        ├── start_scheduler()
        └── yield
```

---

## 七、插件开发者指南

### 7.1 何时使用 `on_core_start_before`

当你的钩子函数满足以下条件时，应使用 `on_core_start_before`：

- ✅ 涉及数据库 Schema 变更（ALTER TABLE、CREATE INDEX）
- ✅ 需要迁移文件或数据
- ✅ 加载全局配置/变量，后续消息处理依赖这些数据
- ✅ 执行时间很短（毫秒级）

### 7.2 何时使用 `on_core_start`

当你的钩子函数满足以下条件时，应使用 `on_core_start`：

- ✅ 涉及网络请求（下载模型、同步数据）
- ✅ 初始化耗时较长的后台服务
- ✅ 不影响消息处理的核心功能
- ✅ 可以容忍在 WS 连接建立后延迟完成

### 7.3 注意事项

> ⚠️ **plugins 文件夹中的插件不受影响**，现有的 `@on_core_start` 装饰器继续正常工作。
> 插件开发者可以根据需要自行迁移到 `@on_core_start_before`，但这不是必须的。
