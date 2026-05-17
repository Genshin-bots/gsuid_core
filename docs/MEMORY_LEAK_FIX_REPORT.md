# GsCore 内存泄漏与性能问题修复报告

> 本文档详细记录了针对 GsCore 框架内存占用异常偏高、命令响应异常过慢问题的代码审查结果与修复方案。

---
| `rag/embedding.py` | 🟡 中 | ✅ 单例模式，设计如此 |

---

## 二、详细修复内容

### 修复 1：Bot 单轮交互实例泄漏（最高优先级 🔴）

**问题描述**：
## 一、问题总览

| 观测现象 | 根因 | 所在模块 | 优先级 | 修复状态 |
|---------|------|---------|--------|---------|
| 内存随时间单调增长，重启后恢复 | Bot.instances / mutiply_instances 未清理 + bg_tasks 持续增长 | `bot.py` / `server.py` | 🔴 高 | ✅ 已修复 |
| WebSocket 断开后内存不释放 | `_send_task` 未被取消 + Bot 未从字典删除 | `server.py::disconnect()` | 🔴 高 | ✅ 已修复 |
| 所有命令均慢，无差异 | `embedding_model.embed()` 同步阻塞事件循环 | `rag/*.py` | 🔴 高 | ✅ 已修复 |
| 并发时响应慢，单请求正常 | 信号量过窄（sem=10）+ 后台任务无上限 | `bot.py` | 🟠 中高 | ⚠️ 需配置调整 |
| 命令首次响应慢，后续正常 | `on_core_start` 后台钩子仍在初始化 | `app_life.py` | 🟡 中 | ✅ 设计如此，已确认 |
| 定时任务触发后内存上涨 | 定时任务回调持有 Bot/Event 强引用 | `scheduled_task/executor.py` | 🟡 中 | ⚠️ 需关注 |
| 内存大且稳定不增长 | Embedding 模型常驻内存（~300MB）
`Bot.instances` 是类级别字典，用于存储单轮交互会话中的 `Bot` 实例。当用户完成交互后，这些实例从未从字典中移除；当 WebSocket 断开时，`GsServer.disconnect()` 也未清理这些引用。导致 `Bot` 对象无法被 GC 回收，内存随时间单调增长。

**注意**：`mutiply_instances` 用于多轮交互，其设计语义是"用户可以持续发多条消息"，清理时机应是用户主动退出多轮模式时，而非每次取完一条消息就清理。因此本次修复**仅清理单轮交互的 `instances`**，多轮交互的清理依赖 WebSocket 断开时的 `disconnect()` 统一处理。

**修复前代码**（`gsuid_core/bot.py`）：

```python
elif is_recive:
    self.receive_tag = True
    self.instances[self.session_id] = self
    self.event = asyncio.Event()
    return await self.wait_for_key(timeout)
```

**修复后代码**（`gsuid_core/bot.py`）：

```python
elif is_recive:
    self.receive_tag = True
    self.instances[self.session_id] = self
    self.event = asyncio.Event()
    try:
        result = await self.wait_for_key(timeout)
    finally:
        # 无论正常返回还是超时异常，都清理单轮交互引用
        self.receive_tag = False
        self.instances.pop(self.session_id, None)
    return result
```

**原因解释**：
- `Bot.instances` 用于单轮交互（`receive_resp`），用户回复后应立即清理
- 使用 `try/finally` 确保即使 `wait_for_key` 超时抛出 `asyncio.TimeoutError`，清理逻辑也能执行
- 使用 `dict.pop(key, None)` 安全删除，避免 KeyError
- 多轮交互（`mutiply_instances`）不在此处清理，因为多轮交互的设计允许用户持续发送多条消息，过早清理会导致多轮对话中断

---

### 修复 2：WebSocket 断开时未清理 Bot 资源（最高优先级 🔴）

**问题描述**：
`GsServer.disconnect()` 仅删除了 `active_ws` 和 `active_bot` 中的条目，但：
1. 未取消 `_Bot._send_task`，导致发送 worker 协程成为孤儿任务，持续占用内存
2. 未清理 `Bot.instances` / `mutiply_instances` 中属于该 bot_id 的条目
3. 未取消 `bg_tasks` 中未完成的后台任务（且 cancel 后未 await）

**修复前代码**（`gsuid_core/server.py`）：

```python
async def disconnect(self, bot_id: str):
    if bot_id in self.active_ws:
        try:
            await self.active_ws[bot_id].close(code=1001)
        except Exception:
            pass
        del self.active_ws[bot_id]
    if bot_id in self.active_bot:
        del self.active_bot[bot_id]
    logger.warning(f"{bot_id}已中断！")
```

**修复后代码**（`gsuid_core/server.py`）：

```python
async def disconnect(self, bot_id: str):
    from gsuid_core.bot import Bot

    if bot_id in self.active_ws:
        try:
            await self.active_ws[bot_id].close(code=1001)
        except Exception:
            pass
        del self.active_ws[bot_id]

    if bot_id in self.active_bot:
        bot = self.active_bot[bot_id]

        # 1. 取消发送 worker，防止孤儿 Task
        if bot._send_task and not bot._send_task.done():
            bot._send_task.cancel()
            try:
                await bot._send_task
            except asyncio.CancelledError:
                pass

        # 2. 取消所有后台任务并等待其真正结束
        tasks_to_cancel = [t for t in bot.bg_tasks if not t.done()]
        for t in tasks_to_cancel:
            t.cancel()
        for t in tasks_to_cancel:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        # 3. 清理 Bot.instances 中属于该 bot_id 的条目
        session_ids_to_remove = [
            sid for sid, b in Bot.instances.items() if b.bot_id == bot_id
        ]
        for sid in session_ids_to_remove:
            del Bot.instances[sid]

        # 4. 清理 Bot.mutiply_instances 中属于该 bot_id 的条目
        mutiply_ids_to_remove = [
            sid for sid, b in Bot.mutiply_instances.items() if b.bot_id == bot_id
        ]
        for sid in mutiply_ids_to_remove:
            del Bot.mutiply_instances[sid]

        # 5. 清理 Bot.mutiply_map 中对应的映射
        # mutiply_map 的结构为 {temp_gid: session_id}，与 mutiply_instances 的 key 对应
        map_keys_to_remove = [
            gid for gid, sid in Bot.mutiply_map.items() if sid in mutiply_ids_to_remove
        ]
        for gid in map_keys_to_remove:
            del Bot.mutiply_map[gid]
        if mutiply_ids_to_remove and not map_keys_to_remove:
            logger.warning(
                f"[disconnect] mutiply_instances 已清理 {len(mutiply_ids_to_remove)} 条，"
                f"但 mutiply_map 中未找到对应映射，map 可能泄漏"
            )

        del self.active_bot[bot_id]

    logger.warning(f"{bot_id}已中断！")
```

**原因解释**：
- `_send_task` 是一个 `while True` 循环的协程，若不被 cancel，会在 `wait_for(timeout=1.0)` 中无限循环，持有 `_Bot` 引用
- `task.cancel()` 仅设置取消标志，协程要在下一个 `await` 点才会真正抛出 `CancelledError`。因此必须 `await task` 等待其真正结束
- `bg_tasks` 中的后台任务（如 Observer、Meme 检测）可能持有 `Bot` 或 `Event` 引用，需要 await 其完成才能释放引用
- `Bot.instances` 和 `mutiply_instances` 是类级别字典，必须通过 `bot_id` 反向查找并清理
- `mutiply_map` 的清理依赖一个假设：`mutiply_map` 的值是 `session_id`，与 `mutiply_instances` 的 key 对应。若此假设不成立（如 `mutiply_map` 的值是其他格式），匹配会静默失败。已添加警告日志检测此情况
- 使用 `list(bot.bg_tasks)` 创建副本后再遍历，避免在迭代时修改集合

---

### 修复 3：bg_tasks set 持有 Task 强引用（高优先级 🔴）

**问题描述**：
`_Bot.bg_tasks` 用于防止 Task 被 GC，但代码中大量 `asyncio.create_task()` 创建的 Task 从未被加入 `bg_tasks`，且即使加入了也没有在完成后移除的机制。这导致：
1. 未加入 `bg_tasks` 的 Task 可能被 GC 取消（但这不是主要问题）
2. 若加入 `bg_tasks` 后未移除，set 会持续增长

**修复前代码**（`gsuid_core/bot.py`）：

```python
class _Bot:
    def __init__(self, _id: str, ws: Optional[WebSocket] = None):
        # ...
        self.bg_tasks = set()
        # ...
```

**修复后代码**（`gsuid_core/bot.py`）：

```python
class _Bot:
    def __init__(self, _id: str, ws: Optional[WebSocket] = None):
        # ...
        self.bg_tasks: set[asyncio.Task] = set()
        # ...

    def _add_bg_task(self, task: asyncio.Task) -> None:
        """将后台任务加入 bg_tasks，并注册完成时自动移除的回调。

        防止 Task 完成后仍被 bg_tasks 强引用，导致 set 持续增长。
        """
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)
```

**原因解释**：
- `task.add_done_callback(self.bg_tasks.discard)` 在 Task 完成（无论成功、失败、取消）时自动从 set 中移除
- `self.bg_tasks.discard` 作为回调函数，即使 task 不在 set 中也不会报错（比 `remove` 安全）
- 所有通过 `asyncio.create_task()` 创建的后台任务都应通过 `_add_bg_task()` 注册

---

### 修复 4：后台任务未统一管理（高优先级 🔴）

**问题描述**：
`handler.py` 和 `bot.py` 中大量 `asyncio.create_task()` 直接创建任务，未加入 `bg_tasks` 管理。这些任务若抛出未捕获异常，会产生 "Task exception was never retrieved" 警告，且无法统一取消。

**修复前代码**（`gsuid_core/handler.py`）：

```python
# Meme Observer
if enable_ai:
    asyncio.create_task(
        observe_message_for_memes(event, ""),
    )

# Memory Observer
if is_enable_memory and memory_config.observer_enabled and "被动感知" in memory_mode:
    asyncio.create_task(
        observe(
            content=event.raw_text,
            speaker_id=str(event.user_id),
            # ...
        )
    )
```

**修复后代码**（`gsuid_core/handler.py`）：

```python
# Meme Observer
if enable_ai:
    meme_task = asyncio.create_task(
        observe_message_for_memes(event, ""),
    )
    ws._add_bg_task(meme_task)

# Memory Observer
if is_enable_memory and memory_config.observer_enabled and "被动感知" in memory_mode:
    mem_task = asyncio.create_task(
        observe(
            content=event.raw_text,
            speaker_id=str(event.user_id),
            # ...
        )
    )
    ws._add_bg_task(mem_task)
```

**修复前代码**（`gsuid_core/bot.py` target_send 中）：

```python
asyncio.create_task(
    observe(
        content=message_list_to_str(mr),
        speaker_id=f"__assistant_{bot_id}__",
        # ...
    )
)
```

**修复后代码**（`gsuid_core/bot.py`）：

```python
task = asyncio.create_task(
    observe(
        content=message_list_to_str(mr),
        speaker_id=f"__assistant_{bot_id}__",
        # ...
    )
)
self._add_bg_task(task)
```

**修复前代码**（`gsuid_core/ai_core/handle_ai.py`）：

```python
asyncio.create_task(
    _update_persona_mood(
        persona_name=session.persona_name,
        group_id=mood_key,
        user_message=query,
    )
)
```

**修复后代码**（`gsuid_core/ai_core/handle_ai.py`）：

```python
mood_task = asyncio.create_task(
    _update_persona_mood(
        persona_name=session.persona_name,
        group_id=mood_key,
        user_message=query,
    )
)
# 安全获取底层 _Bot 实例，兼容 Bot 和 MockBot
# 注意：先判断 Bot（更具体的子类），再判断 _Bot（更宽泛的父类），
# 防止 Bot 继承 _Bot 时 _Bot 分支先匹配导致 underlying 为 Bot 实例
underlying: _Bot | None = None
if isinstance(bot, Bot):
    underlying = bot.bot
elif isinstance(bot, _Bot):
    underlying = bot
elif hasattr(bot, "_real_bot") and isinstance(bot._real_bot, Bot):
    underlying = bot._real_bot.bot

if underlying is not None:
    underlying._add_bg_task(mood_task)
else:
    logger.warning(
        "🧠 [GsCore][AI] 无法获取 _Bot 实例，mood_task 未被注册到 bg_tasks，"
        "可能导致 Task 游离"
    )
```

**原因解释**：
- 所有后台任务都应通过 `_add_bg_task()` 注册，确保：
  1. Task 不被过早 GC
  2. Task 完成后自动从 `bg_tasks` 移除
  3. WebSocket 断开时可以统一取消所有后台任务
- `handle_ai.py` 中的 `bot` 可能是 `MockBot`（通过 `__getattr__` 代理到 `_real_bot`），直接使用 `bot.bot` 可能在 `MockBot` 场景下失效
- **第一版使用 `getattr` 链的问题**：`getattr(bot, "_real_bot", bot)` 在 `bot` 为 `_Bot` 时返回 `_Bot`，但 `_Bot` 没有 `.bot` 属性，导致 `getattr(real_bot, "bot", None)` 返回 `None`。这种隐式失败难以排查
- **改为 `isinstance` 显式判断**：通过 `isinstance(bot, Bot)`、`isinstance(bot, _Bot)`、`hasattr + isinstance` 三层判断，明确处理每种类型，失败时立即进入 `else` 分支并记录警告，避免静默失败
- **判断顺序**：先判断 `Bot`（更具体的子类），再判断 `_Bot`（更宽泛的父类）。若 `Bot` 继承自 `_Bot`，此顺序可防止 `_Bot` 分支先匹配导致 `underlying` 为 `Bot` 实例（`Bot` 无 `_add_bg_task` 方法）。若 `Bot` 不继承 `_Bot`，此顺序同样安全无害

---

### 修复 5：Embedding 模型同步调用阻塞事件循环（高优先级 🔴）

**问题描述**：
`embedding_model.embed([text])` 是同步调用（底层调用 `embed_sync`），在异步事件循环中直接执行会阻塞整个事件循环，导致所有命令响应变慢。虽然 `EmbeddingProvider.embed()` 提供了异步接口，但 `_EmbeddingModelWrapper.embed()` 直接调用了 `embed_sync`。

**修复内容**：
1. 在 `_EmbeddingModelWrapper` 中新增 `aembed()` 异步接口
2. 将所有调用方从 `embedding_model.embed([text])` 迁移为 `await embedding_model.aembed([text])`

**修复前代码**（`gsuid_core/ai_core/rag/base.py`）：

```python
class _EmbeddingModelWrapper:
    def __init__(self, provider: EmbeddingProvider):
        self._provider = provider

    def embed(self, texts: list[str]):
        """兼容 fastembed TextEmbedding.embed() 接口"""
        results = self._provider.embed_sync(texts)
        return iter(results)
```

**修复后代码**（`gsuid_core/ai_core/rag/base.py`）：

```python
class _EmbeddingModelWrapper:
    def __init__(self, provider: EmbeddingProvider):
        self._provider = provider

    def embed(self, texts: list[str]):
        """兼容 fastembed TextEmbedding.embed() 接口（同步，可能阻塞事件循环）

        警告：在异步环境中应使用 aembed() 以避免阻塞事件循环。
        """
        results = self._provider.embed_sync(texts)
        return iter(results)

    async def aembed(self, texts: list[str]):
        """异步批量嵌入（不阻塞事件循环）

        返回一个生成器，每个元素是 list[float]。
        在异步代码中应优先使用此方法代替 embed()。
        """
        results = await self._provider.embed(texts)
        return iter(results)
```

**调用方迁移**（共迁移 14 处）：

| 文件 | 行号 | 说明 |
|------|------|------|
| `system_prompt/vector_store.py` | 107, 151, 228 | sync / search / update |
| `rag/image_rag.py` | 182, 237, 487 | sync / search / add_manual |
| `rag/knowledge.py` | 160, 216, 284, 324, 382, 549 | sync / query / sync_manual / add / update / search |
| `rag/tools.py` | 119, 233 | sync / search |

迁移示例：
```python
# 修复前
vector = list(embedding_model.embed([text_to_embed]))[0]

# 修复后
vector = list(await embedding_model.aembed([text_to_embed]))[0]
```

**重要：aembed() 返回的是同步迭代器**

`aembed()` 的实现为：
```python
async def aembed(self, texts: list[str]):
    results = await self._provider.embed(texts)
    return iter(results)  # 返回同步迭代器，非异步生成器
```

因此调用方在 `await` 之后得到的是一个**普通的同步 `iter` 对象**，应使用 `for` 或 `list()` 迭代，**不能用 `async for`**：

```python
# ✅ 正确：await 后用 list() 或 for 迭代
vector = list(await embedding_model.aembed([text]))[0]

# ✅ 正确：await 后用 for 迭代
for vec in await embedding_model.aembed(texts):
    ...

# ❌ 错误：aembed() 返回的不是异步生成器
async for vec in await embedding_model.aembed(texts):  # TypeError!
    ...
```

本次迁移的 14 处调用方全部使用 `list(await ...)[0]` 形式，符合上述正确用法，无 `for` 循环或 `async for` 用法，因此不存在迭代器误用风险。

**原因解释**：
- `embed()` 是同步接口，直接调用 `embed_sync`，在事件循环中会阻塞其他协程
- `aembed()` 使用 `await self._provider.embed(texts)`，将计算移入线程池（`LocalEmbeddingProvider` 内部使用 `asyncio.to_thread` 或 `loop.run_in_executor`），不阻塞事件循环
- 所有调用方均位于 `async def` 函数中，因此可以直接使用 `await`
- **注意**：`memory/vector/ops.py` 中的 `model.embed()` 调用的是 `SparseTextEmbedding`（fastembed 原生类），非 `_EmbeddingModelWrapper`，因此不在本次迁移范围内

---

### 修复 6：IngestionWorker 重复启动（中优先级 🟡）

**问题描述**：
`init_memory_system()` 中创建 `IngestionWorker` 时没有检查是否已存在实例。若 `on_core_start` 钩子因某种原因被重复调用（如热重载），会创建多个 `IngestionWorker` 线程，每个线程都持有独立的事件循环和 LLM 连接，导致内存峰值过高。

**第一版修复的问题**：
第一版使用了 `threading.Lock` 保护 `_ingestion_worker`：

```python
_ingestion_worker_lock = threading.Lock()

@on_core_start(priority=5)
async def init_memory_system():
    with _ingestion_worker_lock:  # ← 在 async 函数中阻塞获取线程锁
        ...
```

**问题**：`threading.Lock` 的 `with` 语句是同步阻塞的。在 `async` 函数中调用它，若锁被另一个线程持有，会直接阻塞事件循环，与修复 5 要解决的问题属于同一类型。

**正确修复**（`gsuid_core/ai_core/memory/startup.py`）：

```python
_ingestion_worker: Optional[object] = None

@on_core_start(priority=5)
async def init_memory_system():
    """初始化记忆系统的所有组件。

    注意：on_core_start 钩子由 core_start_execute() 按优先级顺序 await，
    同一钩子函数不会并发执行，因此无需加锁保护 _ingestion_worker。
    """
    # ...
    global _ingestion_worker
    if _ingestion_worker is not None:
        logger.info("🧠 [Memory] IngestionWorker 已存在，跳过重复启动")
    else:
        try:
            from .ingestion.worker import IngestionWorker
            _ingestion_worker = IngestionWorker()
            _ingestion_worker.start_in_thread()
        except Exception as e:
            logger.error(f"🧠 [Memory] IngestionWorker 启动失败: {e}")
```

**原因解释**：
- `on_core_start` 钩子由 `core_start_execute()` 按优先级分组后**顺序 await**，同一钩子函数不会真正并发执行
- 因此根本不需要锁——简单的 `if _ingestion_worker is not None` 检查即可防止重复启动
- 若确实需要跨线程保护，应使用 `asyncio.Lock`（不阻塞事件循环），但在此场景下没有必要
- 去掉了 `threading.Lock`，避免在异步环境中同步阻塞事件循环

---

## 三、未修复但已确认的问题

### 3.1 信号量过窄（sem=10）

**现状**：`_Bot.sem = asyncio.Semaphore(10)` 限制每个 Bot 的并发任务数为 10。

**分析**：
- 在高并发场景下，10 个并发槽位可能被快速占满，后续任务排队等待
- 但信号量本身不是泄漏源，只是性能瓶颈
- 建议根据实际负载调整，可通过配置动态设置

**建议**：
```python
# 在配置中增加
"bot_concurrency_limit": 20  # 默认 10，可根据服务器性能调整
```

### 3.2 Embedding 模型常驻内存

**现状**：`LocalEmbeddingProvider` 加载的 fastembed 模型约占用 300MB-1GB 内存。

**分析**：
- 这是设计上的单例模式，模型加载后常驻内存以加速后续查询
- 若业务不需要 RAG，可通过配置关闭：
  ```json
  { "enable_rag": false, "enable_memory": false }
  ```
- Qdrant 已使用 `on_disk=True` 配置（见 `memory/vector/startup.py`），向量不全部加载到内存

### 3.3 定时任务持有 Bot 强引用

**现状**：`execute_scheduled_task` 中创建 `Bot(BOT, ev)` 实例，任务执行后该实例是否被释放取决于 Python 的 GC。

**分析**：
- `Bot` 实例在函数局部作用域中创建，函数返回后应可被 GC
- 但若 `Bot` 实例被注册到某些全局缓存（如 `AISessionRegistry._ai_sessions`），则可能无法释放
- `AISessionRegistry.cleanup_idle_sessions()` 已每 30 分钟清理一次空闲 session

**建议**：监控 `AISessionRegistry._ai_sessions` 的大小，若持续增长，考虑缩短 `IDLE_THRESHOLD`。

### 3.4 反向查找 O(n) 性能问题（技术债务）

**现状**：`disconnect()` 中通过遍历 `Bot.instances.items()` 反向查找属于该 `bot_id` 的 `session_id`。

**分析**：
- 若 `Bot.instances` 存有大量会话（高并发场景），每次 `disconnect` 都做全量遍历，复杂度是 O(n)
- 这在单次调用时无害，但若 `disconnect` 频繁触发（如 Bot 频繁重连），会造成短暂卡顿

**建议（非紧急）**：维护一个反向索引 `Dict[str, Set[str]]`（`bot_id` → `session_id` 集合），在 `Bot` 实例注册到 `instances` 时同步维护。这属于优化建议，不是必须立即修复的错误。

---

## 四、验证清单

修复完成后，建议通过以下方式验证：

1. **内存监控**：使用 `psutil` 或 `memory_profiler` 监控进程内存，确认：
   - WebSocket 断开后内存是否下降
   - 长时间运行后内存是否稳定（不再单调增长）

2. **日志检查**：搜索以下关键词确认清理逻辑生效：
   - `{bot_id}已中断！`（disconnect 被调用）
   - `发送 worker 已启动` / `发送 worker 已取消`（send_task 生命周期）
   - `mutiply_instances 已清理 X 条，但 mutiply_map 中未找到对应映射`（mutiply_map 假设不匹配警告）
   - `无法获取 _Bot 实例，mood_task 未被注册到 bg_tasks`（handle_ai 安全访问失败警告）

3. **压力测试**：模拟高并发消息，检查：
   - 命令响应时间是否稳定
   - `bg_tasks` 集合大小是否稳定（可通过调试日志输出 `len(bot.bg_tasks)`）

4. **多轮交互测试**：测试 `receive_mutiply_resp` 功能，确认多轮对话不会被中断

---

## 五、修复项结论汇总

| 修复项 | 结论 | 备注 |
|--------|------|------|
| 修复 1（单轮清理） | ✅ 正确 | 使用 try/finally 确保超时也能清理 |
| 修复 1（多轮清理） | ✅ 已回滚 | 多轮交互不在每次 pop 后清理，避免中断对话 |
| 修复 2（disconnect） | ✅ 正确 | bg_tasks cancel 后增加 await；mutiply_map 不匹配时加警告日志 |
| 修复 3（bg_tasks discard） | ✅ 正确 | add_done_callback 自动移除已完成任务 |
| 修复 4（后台任务注册） | ✅ 正确 | handle_ai.py 中使用 `isinstance` 显式类型判断替代 `getattr` 链，兼容 _Bot / Bot / MockBot |
| 修复 5（aembed） | ✅ 已完成 | 14 处调用方全部从 `embed()` 迁移为 `await aembed()`，消除事件循环阻塞 |
| 修复 6（IngestionWorker） | ✅ 正确 | 去掉 threading.Lock，改用简单 if 检查（on_core_start 钩子顺序执行，无需锁） |

---

## 六、总结

本次修复主要针对以下核心问题：

1. **Bot 实例泄漏**：在单轮交互结束后（try/finally）和 WS 断开时（disconnect）主动清理 `instances`
2. **发送 worker 孤儿任务**：在 `disconnect()` 中 cancel + await `_send_task`
3. **bg_tasks 无限增长**：引入 `_add_bg_task()` 自动管理 Task 生命周期（add + discard callback）
4. **后台任务未统一管理**：将所有 `asyncio.create_task()` 纳入 `bg_tasks` 管理，且 `disconnect()` 中 cancel 后 await
5. **Embedding 同步阻塞**：新增 `aembed()` 异步接口，并将 `rag/*.py`、`system_prompt/vector_store.py` 中共 14 处调用方全部迁移为 `await embedding_model.aembed()`，消除事件循环阻塞
6. **IngestionWorker 重复启动**：去掉 `threading.Lock`，改用简单 `if` 检查（`on_core_start` 钩子顺序执行，无需锁）

这些修复应能显著改善内存随时间增长和命令响应慢的问题。
