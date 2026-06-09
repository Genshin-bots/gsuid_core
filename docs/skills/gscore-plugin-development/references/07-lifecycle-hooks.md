# 七、启动 / 关闭 / Bot 上线钩子

GsCore 提供 **4 个生命周期钩子**：两个启动期（阻塞前 + 启动后）、一个关闭期、一个 Bot 上线
回调。同一钩子可在不同模块重复注册，框架会按优先级合并并发执行。

## 7.1 钩子总览

| 钩子 | 时机 | 是否阻塞主流程 | 典型用途 |
|------|------|----------------|---------|
| `on_core_start_before` | **WS 服务启动前**（所有 Bot 都未连上来） | ✅ 阻塞——执行完才接连接 | 数据库迁移、表结构变更、必须先做完的全局初始化 |
| `on_core_start` | **WS 服务启动后** | ❌ 后台并发 | 缓存预热、资源下载、注册 AI 知识库、起后台任务 |
| `on_core_shutdown` | 进程**关闭前** | ✅ 阻塞 | 刷新缓冲、关数据库连接、保存中间状态 |
| `gss.on_bot_connect` | **每次** Bot 通过 WS 连上来时 | ❌ 后台并发 | 启动检查、推送遗留消息、与 Bot 自身相关的初始化 |

> **`on_core_start_before` vs `on_core_start` 关键区分**：前者在框架还没开放 WS 端口前就跑，
> 期间所有 Bot 连接尝试都被阻塞——所以"必须在用户能调用任何命令之前完成"的逻辑（schema 升级、
> 必须存在的配置文件预生成、`StringConfig` 字段补全）一律放 `on_core_start_before`；
> 后者在 WS 端口已开放之后**后台**跑，Bot 可能已经在收消息了，所以**不要**把"必须完成"的
> 初始化放这里。
>
> **`gss.on_bot_connect` vs `on_core_start`**：前者每次有新 Bot 连进来都会跑（包括重连），
> 后者只在框架启动一次；想"框架启动后做一次"用 `on_core_start`，想"每个 Bot 上线时单独做"
> 用 `gss.on_bot_connect`。

## 7.2 `on_core_start_before`（启动前阻塞钩子）

```python
from gsuid_core.server import on_core_start_before

# 不带括号：默认 priority=0
@on_core_start_before
async def upgrade_schema():
    await migrate_table()

# 带括号：自定义 priority（越小越先；同优先级并发）
@on_core_start_before(priority=10)
async def warm_critical_cache():
    await load_essential_data()
```

- **同步函数也支持**——框架内部用 `asyncio.to_thread` 包装；但能 async 就 async。
- 钩子内部抛异常会被框架捕获并 `logger.exception`，**不会**让进程崩溃，但**也不会**让插件
  的剩余初始化逻辑中断——所以 schema 迁移失败要明确告警 + 数据库标记，下一步用户的写操作
  自己会失败。

## 7.3 `on_core_start`（启动后后台钩子）

```python
from gsuid_core.server import on_core_start

@on_core_start(priority=5)
async def warmup_cache():
    await prefetch_role_icons()
    await build_alias_index()

@on_core_start
async def register_ai_knowledge():
    # 等 RAG 初始化完后再注册（priority 留 0 即可）
    from gsuid_core.ai_core.register import ai_entity
    ai_entity(KnowledgePoint(...))
```

## 7.4 `on_core_shutdown`（关闭前钩子）

进程收到 SIGTERM / Ctrl+C / `core重启` 命令时执行；用来**收尾**——刷写未持久化的缓冲、
关掉后台 task、保存中间状态。参考 `_XutheringWavesUID` 的写法：

```python
import asyncio
from gsuid_core.server import on_core_shutdown
from gsuid_core.logger import logger

_shutdown_event = asyncio.Event()
_flush_task = asyncio.get_event_loop().create_task(_activity_flush_loop())


@on_core_shutdown
async def _flush_on_shutdown():
    """退出前刷写缓冲区，防止数据丢失"""
    logger.info("[MyPlugin] 退出前刷写中...")
    _shutdown_event.set()
    try:
        await asyncio.wait_for(_flush_task, timeout=5)
    except asyncio.TimeoutError:
        logger.warning("[MyPlugin] 刷写超时，强制退出")
```

- 钩子有**总时间预算**，超时会被强制中止——内部务必加 `asyncio.wait_for(..., timeout=N)`。
- 同样支持 `priority` 参数；越小越先关闭。

## 7.5 `gss.on_bot_connect`（Bot 上线回调）

每次 Bot 通过 WS 连接到 core 时触发（首次连接 + 重连都触发）。无参数，无 priority。

```python
import asyncio
from gsuid_core.gss import gss
from gsuid_core.logger import logger

@gss.on_bot_connect
async def check_pending_messages():
    """Bot 上线后稍等一会儿，把启动期间积压的提醒推出去"""
    try:
        await asyncio.sleep(2)                # 等 Bot 自身完成握手
        await flush_pending_notifications()
    except Exception as e:
        logger.warning(f"[MyPlugin] 启动检查失败: {e}")
```

- 注册方式**和其他钩子不一样**——它是 `GsServer.on_bot_connect` 类方法，通过 `gss` 实例
  使用：`@gss.on_bot_connect`，**不是** `@on_bot_connect`。
- 函数签名**没有参数**——内部如需知道"哪个 Bot 上线了"应自己遍历 `gss.active_bot`。
- **同名同模块**重复注册会被去重，所以热重载时不会累积。
- 框架在 Bot 连进来之后会并发触发所有 `bot_connect_def`，**异常会被吞**——务必自己 `try/except`。

## 7.6 常见使用场景速查

| 场景 | 选哪个钩子 | 注意点 |
|------|-----------|--------|
| 数据库表结构变更 / 字段补全 | `on_core_start_before` | 阻塞所有 Bot 连接直到完成 |
| 加载全局配置 / 修复配置文件 | `on_core_start_before` | 避免运行时配置类抖动 |
| 注册 AI 知识库内容（`ai_entity`） | `on_core_start` | 等 RAG 初始化完 |
| 预热 HTTP / 图片缓存 | `on_core_start` | 后台跑、不阻塞用户命令 |
| 启动后台监控 / 数据同步任务 | `on_core_start` | 配合 `asyncio.create_task` |
| 推送遗留消息（重启前未处理的通知） | `gss.on_bot_connect` | Bot 在线才能推 |
| 启动检查（向主人汇报 Bot 上线） | `gss.on_bot_connect` | 注意防止重连刷屏 |
| 关闭前刷写缓冲 / 落盘 | `on_core_shutdown` | 加 `asyncio.wait_for` 超时 |
| 关闭前保存 task 状态 | `on_core_shutdown` | 不要在这里跑长任务 |
