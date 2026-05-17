# GsCore AI Agent 框架能力深度优化 — 实现说明

> 本文档详细记录《AI Agent 框架能力深度优化方案 v2》的落地实现，涵盖每一项改动的**背景动机**、**具体修改**、**涉及文件**与**设计取舍**。
>
> 实现方案文档：`plans/AI_Agent_框架能力深度优化方案_v2.md`
> 实现日期：2026-05-16

---

## 目录

- [GsCore AI Agent 框架能力深度优化 — 实现说明](#gscore-ai-agent-框架能力深度优化--实现说明)
  - [目录](#目录)
  - [背景](#背景)
  - [P0 立即修复](#p0-立即修复)
    - [P0-1 调试信息泄露修复](#p0-1-调试信息泄露修复)
    - [P0-2 框架保底工具池](#p0-2-框架保底工具池)
    - [P0-3 Persona 决策逻辑重写](#p0-3-persona-决策逻辑重写)
    - [P0-4 主人好感度初始化](#p0-4-主人好感度初始化)
    - [P0-5 主人说话者感知高亮](#p0-5-主人说话者感知高亮)
  - [P1 近期优化](#p1-近期优化)
    - [P1-1 通用持久状态存储](#p1-1-通用持久状态存储)
    - [P1-2 记忆事实主语补全（取舍说明）](#p1-2-记忆事实主语补全取舍说明)
    - [P1-3 实体消歧与别名重定向](#p1-3-实体消歧与别名重定向)
    - [P1-4 记忆注入 Token 预算与事实主语补全](#p1-4-记忆注入-token-预算与事实主语补全)
    - [P1-5 图片理解摘要层](#p1-5-图片理解摘要层)
    - [P1-6 自我认知工具扩展](#p1-6-自我认知工具扩展)
    - [P1-7 连续无工具调用检测](#p1-7-连续无工具调用检测)
  - [P2 中期新增](#p2-中期新增)
    - [P2-1 群组画像](#p2-1-群组画像)
    - [P2-2 语境工具池](#p2-2-语境工具池)
    - [P2-3 语境注入](#p2-3-语境注入)
    - [P2-4 定时任务结构化上下文](#p2-4-定时任务结构化上下文)
    - [P2-5 工具前摇代码层触发](#p2-5-工具前摇代码层触发)
  - [P3 深度拟人化](#p3-深度拟人化)
    - [P3-1 情绪与主人联动](#p3-1-情绪与主人联动)
    - [P3-2 工具 description 标准化](#p3-2-工具-description-标准化)
  - [未实现项与取舍说明](#未实现项与取舍说明)
  - [数据库变更](#数据库变更)
  - [完整文件清单](#完整文件清单)
    - [新增文件](#新增文件)
    - [修改文件](#修改文件)
  - [修订记录（2026-05-17 Review 修正）](#修订记录2026-05-17-review-修正)
    - [第二轮 Review 修正（同日）](#第二轮-review-修正同日)
    - [第三轮调整（定时任务工具分类 + LLM.md 合规）](#第三轮调整定时任务工具分类--llmmd-合规)

---

## 背景

此前框架在两类典型场景中暴露出系统性缺陷：

1. **复杂的长链条任务**（如"给你 10 万元虚拟炒股"）无法跨会话存活——没有持久状态存储，Agent 收到指令后"什么都不会再发生"。
2. **语境消歧任务**（如群里问"下期深渊练什么角色"）会卡住——框架不知道"深渊"指原神深渊螺旋，工具搜索也命中不到游戏工具。

此外还存在调试信息泄露给用户、Agent 以"角色不懂"为由跳过工具调用、记忆事实信息密度为零、实体别名分裂等问题。

本次优化**只涉及框架能力层**，不涉及任何具体业务插件。目标是让框架为插件提供通用、可靠的底层能力。

---

## P0 立即修复

### P0-1 调试信息泄露修复

**问题**：`gs_agent.py` 中 `result_msg += "（🔧 本次执行工具调用列表: ...）"` 把调试信息追加到了用户可见的回复里。

**修改**（`gsuid_core/ai_core/gs_agent.py`）：删除该拼接，工具调用列表只写入 `logger.debug`，`result_msg` 保持纯净。`_session_logger.log_result()` 仍单独接收工具列表用于会话日志。

**核心变动**：

```diff
  # 始终返回字符串类型
  result_msg = str(result.output).strip()
- if _tool_call_list:
-     result_msg += f"\n\n（🔧 本次执行工具调用列表: {'、'.join(_tool_call_list)}）"
+ # 工具调用列表只进调试日志，不追加到用户可见消息
+ if _tool_call_list:
+     logger.debug(f"🔧 [本次工具调用] {', '.join(_tool_call_list)}")
```

### P0-2 框架保底工具池

**问题**：工具发现是纯向量搜索，口语化 query 与工具描述语义距离过大时，`web_search`、`search_knowledge` 等基础工具会被挤出 top-k，导致 Agent 关键场景"无工具可用"。

**修改**（采用**分类驱动**而非硬编码名单）：
- 保底工具池由工具注册时声明的 `category` 决定：`self` + `buildin` 两个分类即"框架保底工具池"。`rag/tools.py` 定义 `GUARANTEED_TOOL_CATEGORIES = ["self", "buildin"]`，`get_main_agent_tools()` 无条件全部加载这两个分类下的工具，不受向量搜索影响。
- 将原本属于 `buildin` 但并非"任何任务都需要"的 3 个工具（`search_image`、`get_self_persona_info`、`set_user_favorability`）改归 `common` 分类，使 `buildin` 保持精简的核心保底集。
- `gs_agent.py` 工具组装阶段：保底工具池全部保留；语境工具池 + 查询工具池合并去重后限制附加数量上限（12 个）。

> **设计取舍**：早期实现曾用硬编码的 `FRAMEWORK_GUARANTEED_TOOLS` 名单，但这要求每新增一个保底工具都得改框架中心清单。改为分类驱动后，插件/核心只需用 `@ai_tools(category="buildin")` 注册即可让工具进入保底池，零框架改动。

保底工具池覆盖：搜索（`web_search_tool`/`search_knowledge`/`web_fetch_tool`）、记忆与自我认知（`query_user_memory`/`get_self_info`）、持久状态（`state_*`）、好感度、子Agent、消息发送、定时任务创建入口（`add_once_task`/`add_interval_task`）等。

> **后续调整（定时任务工具分类细化）**：定时任务的 8 个工具按"创建 vs 管理"拆分——`add_once_task` / `add_interval_task` 两个**创建**入口保留在 `self` 保底池（其触发高度口语化，如"每天下午三点半推送新闻"，向量检索难命中）；`list` / `query` / `modify` / `cancel` / `pause` / `resume` 六个**管理**类工具改归 `common` 分类，由查询工具池按需向量检索（用户使用时通常显式带任务 ID 或明确表达管理意图，向量命中率高）。此调整使保底池从约 22 个工具收敛到约 16 个，降低每次请求的 Token 开销。

**核心变动**：

`rag/tools.py` —— 保底池由 category 决定，`get_main_agent_tools()` 全量加载：

```diff
+ # 保底工具分类：这两个分类下的工具无条件全部注入主Agent
+ GUARANTEED_TOOL_CATEGORIES: List[str] = ["self", "buildin"]

  async def get_main_agent_tools(query: str = "") -> ToolList:
-     # self 始终加载
-     for tool_base in all_tools_cag["self"].values():
-         result_tools.append(tool_base.tool)
-     # buildin 按向量阈值筛选，最多 4 个
-     buildin_tools_search = await search_tools(
-         query=search_query, limit=4, category="buildin", threshold=0.1)
-     result_tools.extend(buildin_tools_search)
+     # self + buildin 两个保底分类，无条件全部加载，不受向量搜索影响
+     for cat in GUARANTEED_TOOL_CATEGORIES:
+         for tool_base in all_tools_cag.get(cat, {}).values():
+             result_tools.append(tool_base.tool)
```

`gs_agent.py` —— 三层工具池组装，保底池全保留、附加池限量：

下方为 `gs_agent.py` 工具组装的**真实代码结构**（变量名、控制流与仓库一致）：

```python
# 第一层：框架保底工具池（self + buildin 全量，无条件保留）
core_tools = await get_main_agent_tools()
core_names = {t.name for t in core_tools}

# 附加工具池 = 语境工具池 + 查询工具池
extra_tools: ToolList = []

# 第二层：语境工具池——仅群聊场景，按群组画像标签自动加载
if ev is not None and getattr(ev, "group_id", None):
    try:
        scope_key = make_scope_key(ScopeType.GROUP, str(ev.group_id))
        ctx_tags = await get_scope_context_tags(scope_key)        # 读群组画像标签
        if ctx_tags:
            ctx_tools = get_tools_by_context_tags(ctx_tags, max_count=8)
            if ctx_tools:
                extra_tools += ctx_tools
    except Exception as e:
        logger.debug(f"语境工具池加载失败: {e}")

# 第三层：查询工具池——基于 query 的向量搜索（排除已在保底池的分类）
if qy:
    extra_tools += await search_tools(query=qy, limit=6,
                                      non_category=["self", "buildin"])

# 附加池去重：剔除与保底重名、以及附加池内部重复（内联循环，无独立 dedup 函数）
seen: Set[str] = set(core_names)
deduped_extra: ToolList = []
for t in extra_tools:
    if t.name in seen:
        continue
    seen.add(t.name)
    deduped_extra.append(t)

# 保底池全保留；附加池限量 12 个
tools = core_tools + deduped_extra[:MAX_EXTRA_TOOLS]   # MAX_EXTRA_TOOLS = 12
```

> **说明**：`ctx_tools` 是第二层语境工具池的局部变量——只在 `ev.group_id` 存在且群组画像有标签时，由 `get_tools_by_context_tags()` 赋值；非群聊或无标签时该层为空。去重是一段**内联循环**，并无 `dedup()` 这样的独立函数。早期方案文档曾用 `ctx_tools + dedup(...)` 的概念化简写描述思路，此处已替换为与仓库一致的真实结构。

`buildin_tools` —— 3 个非核心工具从 `buildin` 改归 `common`：

```diff
- @ai_tools(category="buildin")   # search_image / get_self_persona_info / set_user_favorability
+ @ai_tools(category="common")
```

### P0-3 Persona 决策逻辑重写

**问题**：旧 Prompt 用"宁可多委派，不可少委派"描述 SubAgent 策略，导致 Agent 把"委派 SubAgent"和"直接调工具"混为一谈；Persona 的"懒惰性格"还提供了"这很麻烦 → 返回沉默/推脱"的逃脱路径。

**修改**（`persona/prompts.py` 的 `SYSTEM_CONSTRAINTS`）：
- `## 决策逻辑` 改写为**严格优先级决策树**：合规检查 → 寒暄判断 → 工具是否充足（1-2 个工具直接调用 / 3+ 工具才委派 / 无专属工具用搜索兜底）→ 工具均不满足才角色化告知。
- 新增**绝对禁止行为**：禁止以"角色不懂/不擅长/不想管"跳过工具；禁止有工具时回复"不知道"；禁止用户明确提问时输出 `<SILENCE>`；禁止未尝试工具就直接委派。明确"角色懒惰只体现在语言风格，不影响是否执行任务"。
- `## 子Agent` 段补充**结构化 task 模板**（目标 / 触发上下文 / 已知信息 / 步骤 / 输出要求 / 资源ID）。

**核心变动**（`persona/prompts.py` 的 `SYSTEM_CONSTRAINTS`）：

```diff
- ### 任务分类与强制行为
- **复杂任务** → 必须立即调用 `create_subagent`，严禁自己尝试处理
- **判断原则**：宁可多委派，不可少委派。自己处理复杂任务是错误行为。
+ ### 决策树（优先级严格从高到低，逐级判断）
+ 1. 是否违反合规红线？是 → 角色化拒绝
+ 2. 是否日常寒暄/纯情绪回应？是 → 直接回应
+ 3. 工具是否充足？
+    - 1-2 个工具可解决 → 直接调用工具
+    - 需 3+ 工具协作 → 委派 create_subagent
+    - 无专属工具 → 用 web_search / search_knowledge 兜底
+ 4. 所有工具均无法满足 → 角色化告知，严禁编造
+
+ ### 绝对禁止行为
+ - 禁止以"角色不懂/不擅长/不想管"为由跳过工具调用
+ - 禁止在有可用工具时回复"不知道"或让用户自己查
+ - 禁止在用户明确提问时输出 <SILENCE>
+ - 角色的懒惰只体现在语言风格，绝不影响是否执行任务
```

### P0-4 主人好感度初始化

**问题**：主人 ID 只写在 system_prompt 中，Agent 对主人没有"情感积累"。

**修改**（`ai_router.py`）：新增 `_ensure_master_favorability()`，在 session 创建时检查每个主人的好感度，低于下限 90 则自动拉升到 95。通过 `_master_favorability_checked` 集合保证每个 `(bot_id, master_id)` 只检查一次，避免重复 DB 访问。

**核心变动**（`ai_router.py`）：

```diff
+ async def _ensure_master_favorability(bot_id: str) -> None:
+     """确保所有主人用户处于高好感度模式。"""
+     for master_id in core_config.get_config("masters") or []:
+         cache_key = (bot_id, str(master_id))
+         if cache_key in _master_favorability_checked:
+             continue                                  # 每个组合只检查一次
+         _master_favorability_checked.add(cache_key)
+         record = await UserFavorability.get_user_favorability(master_id, bot_id)
+         if (record.favorability if record else 0) < MASTER_FAVORABILITY_FLOOR:  # < 90
+             # 第 4 个参数是 user_name（昵称），系统初始化时未知，留空——不可误传 master_id
+             await UserFavorability.set_favorability(
+                 master_id, bot_id, MASTER_FAVORABILITY_TARGET)                   # → 95

  async def _get_or_create_ai_session(event, session_id=None):
+     await _ensure_master_favorability(getattr(event, "bot_id", "") or "")
      session = registry.get_ai_session(session_id)
```

### P0-5 主人说话者感知高亮

**问题**：主人发言时与普通用户的"说话者感知"文本无差异。

**修改**（`utils.py`）：新增 `_is_master_user()`；`_build_relationship_description()` 在用户为主人时返回高亮文本"【⚡ 你的主人】… 直接找你说话了。对主人：完全信任，认真对待，有求必应（合规范围内）。"

此外，群聊全员共用一个 session，因此说话者描述里**始终显式带上用户ID**（如 `小明(用户ID:123456)`），避免昵称重复或为"我"这类无意义值时 Agent 无法区分发言者。

**核心变动**（`utils.py`）：

```diff
+ def _is_master_user(user_id: str) -> bool:
+     masters = core_config.get_config("masters") or []
+     return str(user_id) in [str(m) for m in masters]

  def _build_relationship_description(favorability, user_name, user_id):
-     name = user_name or user_id
+     # 说话者标识始终包含用户ID，群聊中靠 ID 区分发言者
+     if user_name and user_name.strip() and user_name.strip() != str(user_id):
+         speaker = f"{user_name.strip()}(用户ID:{user_id})"
+     else:
+         speaker = f"用户ID:{user_id}"
+     if _is_master_user(user_id):
+         return (f"【⚡ 你的主人】{speaker} 直接找你说话了。"
+                 "对主人：完全信任，认真对待，有求必应（合规范围内）。")
      ...
-     return f"{name} 找你了，算是熟人了。"
+     return f"{speaker} 找你了，算是熟人了。"
```

---

## P1 近期优化

### P1-1 通用持久状态存储

**问题**：Agent 是无状态的，但复杂任务（虚拟炒股账户、任务进度）是有状态的。对话历史会话结束即消失，记忆图谱也无法精确存储"账户余额 = 87450.32"这类业务状态。

**新增模块** `gsuid_core/ai_core/state_store/`：

| 文件 | 说明 |
|------|------|
| `models.py` | `AIPersistentState` 数据库模型，以 `(scope, state_key)` 定位，value 存 JSON |
| `store.py` | `state_set/get/delete/list/append` 核心读写逻辑，自动处理 TTL 过期 |
| `tools.py` | 暴露给 Agent 的 `state_*` 工具 |
| `__init__.py` | 模块导出 |

**设计要点**：
- **命名空间隔离**：`scope` 支持 `"user:{id}"`、`"group:{id}"`、`"global"`，传 `"auto"` 时按当前会话自动推断（群聊/私聊）。
- **TTL 过期**：可选 `ttl_days`，读取时若已过期自动删除并视为不存在。
- **JSON 友好**：value 支持任意 JSON 可序列化结构。
- **`state_mutate` 乐观锁原语**：`(scope, state_key)` 加唯一约束；`state_mutate(scope, key, mutator)` 是统一的并发安全"读-改-写"原语——「读 version → `mutator(当前值)` 算出新值 → 条件更新（`WHERE version = 旧值`）」，并发冲突时只有先提交者成功，后者 `rowcount=0` 触发重试并基于最新值重新计算，避免"后写覆盖前写"静默丢数据。`state_append_item`（列表追加，支持 `max_length` 裁剪）是它的薄封装；群组画像的频次累加（P2-1）也复用它。
- **建表不依赖启动时序**：`AIPersistentState` 表通过 `store.py` 的 `_ensure_table()` 在**首次读写前**做一次针对性建表（进程级 flag 保证只执行一次）。框架全局 `create_all` 在 webconsole 后台阶段执行，与 `buildin_tools → state_store` 的导入是并发的，无法保证 `create_all` 时本模型已注册；`_ensure_table()` 使 state_store 与启动竞态解耦。

这 5 个工具是框架保底工具的一部分，任何 session 默认注入。

**核心变动**（新增模块的关键定义）：

```python
# state_store/models.py —— 以 (scope, state_key) 定位的键值表，并加唯一约束
class AIPersistentState(SQLModel, table=True):
    # 唯一约束是乐观锁的前提：并发首次插入只有一个成功，另一个收 IntegrityError 后重试
    __table_args__ = (
        UniqueConstraint("scope", "state_key", name="uq_state_scope_key"),
        {"extend_existing": True},
    )
    scope: str = Field(index=True)        # "user:123" / "group:456" / "global"
    state_key: str = Field(index=True)    # 业务键名，如 "stock:portfolio"
    value: str = Field(default="null")    # JSON 序列化后的值
    version: int = Field(default=1)       # 乐观锁版本号，每次写入自增
    expire_at: Optional[datetime] = Field(default=None, index=True)  # TTL

# state_store/store.py —— state_mutate 是统一的乐观锁"读-改-写"原语
async def state_mutate(scope, state_key, mutator, ttl_days=None):
    await _ensure_table()
    for attempt in range(_APPEND_MAX_RETRY):          # 默认 5 次
        record = await _select(scope, state_key)      # 读取当前记录（含 version）
        current = _decode(record)                     # 不存在/已过期 → None
        new_value = mutator(current)                  # 纯函数，冲突时会被重试
        if record is None:
            try:
                await _insert(scope, state_key, new_value, ttl_days)
                return new_value
            except IntegrityError:
                continue                               # 已被并发插入，重试走 UPDATE
        else:
            # 条件更新：仅当 version 仍是读取时的值才生效
            if await _update_if_version(record, new_value, ttl_days):
                return new_value                       # rowcount == 1
            # version 已被并发写入推进 → 重试
    raise RuntimeError("state_mutate 乐观锁重试耗尽")

# state_append_item 是 state_mutate 的薄封装：mutator 就是"把 item 追加进列表"
async def state_append_item(scope, state_key, item, max_length=None, ttl_days=None):
    def _appender(current):
        lst = list(current) if isinstance(current, list) else ([] if current is None else [current])
        lst.append(item)
        return lst[-max_length:] if (max_length and len(lst) > max_length) else lst
    new_list = await state_mutate(scope, state_key, _appender, ttl_days=ttl_days)
    return len(new_list)
```

```diff
# buildin_tools/__init__.py —— 注册 state_* 工具进保底池
+ from gsuid_core.ai_core.state_store import (
+     state_get, state_set, state_list, state_append, state_delete,
+ )
```

> `state_set_value` 同样对并发首次插入做了 `IntegrityError` 重试兜底（set 语义为 last-write-wins）。`state_mutate` 也对外导出，供群组画像等需要并发安全累加的场景复用。

### P1-2 记忆事实主语补全（取舍说明）

**问题**：注入的"已知事实"中，fact 常缺主语（如"建议关注中证白酒指数"），脱离 src/tgt 字段后无法独立阅读，信息密度低。

**取舍——不改提取提示词，改在检索层补全**：原方案曾计划改写 `extraction.py` 的提取提示词，新增"信息完整性检查"后置步骤要求 LLM 自查 fact 是否带主语。但提取提示词的**后置检查步骤不可靠**——LLM 倾向"读完指令一次性生成输出"，放在输出格式定义之后的检查步骤，可能在 JSON 已经开始生成后才被"读到"，此时无法回退。

因此最终**保持 `extraction.py` 原样不动**，把 fact 主语问题放到**检索/格式化层**解决：主语信息本就存在于 edge 的 source 端（`source_name`），注入 Prompt 时直接拼接即可，无需依赖提取阶段额外产出"完整句子"。具体实现见 [P1-4 的 `_complete_fact_subject`](#p1-4-记忆注入-token-预算与事实主语补全)。

> **为何检索层更可靠**：提取是"一次性"的（每段对话只提取一次，提示词约束失败就永久缺主语）；而检索层补全是"每次注入时"执行的确定性代码逻辑——既能修复历史数据，也不受 LLM 发挥波动影响。

### P1-3 实体消歧与别名重定向

**问题**：原神角色"妮可"的多个外号（"天使""大班尼特"）会被提取为 3 个独立实体，互相之间没有关联，检索"大班尼特"时无法召回关于"妮可"的大量记忆。

**修改**（`memory/ingestion/worker.py` 新增 `_apply_alias_redirection()`）：
- **Level-1 别名硬规则**：根据 `alias_of` 构建别名映射，解析传递性别名，把所有 edge 的 src/tgt 引用重写为正式名称，并将别名实体的摘要并入正式实体。
- **Level-2 向量相似度合并**：已由 `AIMemEntity.extract_and_upsert` 的混合检索去重承担（既有能力）。
- 别名映射同时写入群组画像的 `term_mappings`。

**核心变动**（`memory/ingestion/worker.py`）：

```diff
+ def _apply_alias_redirection(extracted: dict) -> dict[str, str]:
+     """别名重定向（实体消歧 Level-1）。"""
+     # 0. 先从 extracted 解包 entities / edges（缺失时给空列表，避免 NameError）
+     entities = extracted.get("entities") or []
+     edges = extracted.get("edges") or []
+     # 1. 收集 {别名: 正式名称} 映射
+     alias_map = {e["name"]: e["alias_of"] for e in entities if e.get("alias_of")}
+
+     # 2. 解析传递性别名（链式："大班尼特"→"妮可"→正式名）。
+     #    用带深度上限的递归把每个别名一路解析到链尾的正式名称，
+     #    深度上限同时兼作环路保护。
+     def _resolve(name: str, _depth: int = 0) -> str:
+         if _depth > 5 or name not in alias_map:
+             return name
+         return _resolve(alias_map[name], _depth + 1)
+     resolved = {a: _resolve(a) for a in alias_map}
+
+     # 3. 把所有 edge 的 src/tgt 引用重写为正式名称
+     for edge in edges:
+         if edge["source"] in resolved: edge["source"] = resolved[edge["source"]]
+         if edge["target"] in resolved: edge["target"] = resolved[edge["target"]]
+     # 4. 别名实体的摘要并入正式实体，别名本身不独立存储
+     return resolved

  # flush 流程中接入
  extracted = await _llm_extract(dialogue, scope_key)
+ alias_map = _apply_alias_redirection(extracted)
+ if alias_map:
+     await record_term_mappings(scope_key, alias_map)   # 写入群组画像
```

> 传递性别名是**真实现**而非注释占位：链式别名（A→B→C）会被 `_resolve` 一路解析到链尾的正式名称 C，深度上限 5 同时作为环路保护，超出即原样返回。

> **当前生效状态**：`_apply_alias_redirection` 的输入是提取结果里的 `a`（alias_of）字段。由于 P1-2 最终选择不改写 `extraction.py`（保持提取提示词原样），提示词当前不会主动产出 `a` 字段——因此 **Level-1 别名硬规则目前处于"代码就绪、暂未被喂数据"的休眠态**，`alias_map` 恒为空、流程被安全跳过。**Level-2 向量相似度合并**（`AIMemEntity.extract_and_upsert` 的混合检索去重）不依赖 `a` 字段，仍正常生效，是当前实体消歧的主力。若日后需要启用 Level-1，只需让提取提示词输出 `a` 字段即可，`worker.py` 侧无需改动。

### P1-4 记忆注入 Token 预算与事实主语补全

**修改**（`memory/retrieval/dual_route.py`）：
- **Token 预算控制**：`to_prompt_text()` 重写为预算配分式格式化——核心事实约 55%、语义类目约 15%、相关对话约 30%，每区块在预算内逐条累加，超预算即停止，低价值内容不再挤占空间。预算上限 `max_chars` **不再硬编码**：默认 2000，但通过记忆配置项 `memory_inject_max_chars`（可选 1000/2000/4000/8000/16000）暴露给用户——话题密集、历史很集中的群可按需调大，避免有用上下文被裁掉。
- **别名展开检索**：`dual_route_retrieve()` 入口处，若 query 中出现群内别名，自动附加正式名称，使检索能命中关于正式实体的全部记忆。
- **事实主语补全**：fact 的主语信息**本就存在于 edge 中**——`Edge.source_name`（source 实体名称）即主语。检索阶段两条路径都已填充该字段（System-1 的 `_hybrid_search_edges` 批量回填、System-2 由 `entity_id_to_name` 映射填充），因此 `_complete_fact_subject(fact, source_name)` 只需在格式化时做一次**字符串拼接**：缺主语的 fact 前面接上 `source_name`（纯数字名称视为用户 ID，补成"用户{id}"）。这是 P1-2 取舍后选定的主语补全方案——不依赖提取阶段，纯检索层确定性逻辑。

**核心变动**（`memory/retrieval/dual_route.py`）：

```diff
- def to_prompt_text(self, max_chars: int = 24000) -> str:
-     # 旧实现：固定顺序拼接，最后整体字符截断
-     facts_text = "\n".join(f"• {e['fact']}" for e in self.edges[:N])
+ def to_prompt_text(self, max_chars: int = 2000) -> str:   # 默认值，调用方按配置传入
+     # 预算配分：核心事实 55% / 语义类目 15% / 相关对话 30%
+     # 每区块在自己的预算内逐条累加，超预算即停止
+     fact_budget = int(max_chars * 0.55)
+     # 主语就在 edge 的 source_name 上，直接拼接，无需运行时查表
+     fact_lines = [f"• {_complete_fact_subject(e['fact'], e['source_name'])}"
+                   for e in self.edges[: memory_config.search_edge_count]]

+ def _complete_fact_subject(fact: str, source_name: str) -> str:
+     """用 edge 的 source_name 给缺主语的 fact 拼接主语。"""
+     fact = fact.strip()
+     if not fact:
+         return ""
+     if source_name and source_name not in fact:
+         subject = f"用户{source_name}" if source_name.isdigit() else source_name
+         if subject not in fact:
+             fact = f"{subject}{fact}"   # "建议关注X" → "用户444835641建议关注X"
+     return fact
```

> `to_memory_text()` 同样调用 `_complete_fact_subject`，保证两种注入文本的事实都带主语。`source_name` 由检索阶段填充，格式化时不再做运行时实体名查找。

```diff
# dual_route_retrieve() 入口 —— query 别名展开
+ mappings = await get_term_mappings(group_scope)
+ query = expand_query_with_aliases(query, mappings)   # 出现别名则附加正式名称
```

### P1-5 图片理解摘要层

**问题**：图片理解结果（完整描述常达上千字，含大量与当前问题无关的细节）直接塞入 context，严重浪费 Token。

**修改**（`gs_agent.py` 新增 `_summarize_image_description()`）：模型不支持图片时，对超过 400 字的图片描述用低成本模型做一次"针对用户问题"的聚焦摘要，只保留与问题相关的 1-3 句话。

**关于 `user_question` 的来源**：摘要需要的"用户问题"**不经任何参数链跨函数传递**。图片转述发生在 `_prepare_user_message()` 内部，该函数本就持有完整的消息内容列表——其中的纯文本部分（`text_parts`）就是用户问题。因此 `user_question` 直接在 `_prepare_user_message()` 内**就地计算**（`"\n".join(text_parts).strip()`），再作为局部变量传给同对象的 `_summarize_image_description()`。`_prepare_user_message()` 的签名无需新增参数，也无需引入 `self._last_user_question` 实例变量。

**仅发图片、无文字时的处理**：当消息只有图片、`text_parts` 为空，`user_question` 会是空字符串。这种情况**不会让摘要失效**——摘要 prompt 用 `user_question or '（无明确问题）'` 兜底，并明确指示模型"若用户没有明确问题，则用一句话概括图片主旨"。因此无文字消息仍会得到一句话的主旨摘要，Token 节省照常生效，不会输出随机内容或原样返回长描述。

**核心变动**（`gs_agent.py`，`_summarize_image_description` 为真实实现，非占位）：

```diff
  # _prepare_user_message() 内：图片转述前，就地从消息文本算出 user_question
+ user_question = "\n".join(text_parts).strip()        # text_parts 即本条消息的纯文本
  for idx, url in enumerate(image_urls):
      description = await understand_image(image_url=url)
+     # 对冗长描述做"针对用户问题"的二次摘要，节省 Token
+     description = await self._summarize_image_description(description, user_question)
      descriptions.append(f"图片{idx + 1}: {description}")

+ async def _summarize_image_description(self, description, user_question):
+     SUMMARY_THRESHOLD = 400
+     if not description or len(description) <= SUMMARY_THRESHOLD:
+         return description                       # 短描述直接返回，不额外调用模型
+     try:
+         prompt = (
+             "以下是一张图片的完整描述。"
+             f"用户正在问：「{user_question or '（无明确问题）'}」。\n"
+             "请从图片描述中提取与用户问题直接相关的信息，用 1-3 句话概括，"
+             "无关信息完全省略。若用户没有明确问题，则用一句话概括图片主旨。\n\n"
+             f"【图片完整描述】\n{description}"
+         )
+         _summary_agent = Agent(
+             model=get_model_for_task("low"),     # 低成本模型
+             system_prompt="你是一个图片信息提炼助手，只输出精简摘要。",
+             model_settings={"max_tokens": 500},
+             tools=[], toolsets=[], retries=0, output_type=str,
+         )
+         result = await _summary_agent.run(prompt, message_history=[])
+         summary = str(result.output).strip()
+         if summary:
+             return summary
+     except Exception as e:
+         logger.debug(f"图片描述二次摘要失败，使用原始描述: {e}")
+     return description                            # 摘要失败回退原描述（绝不返回 None）
```

### P1-6 自我认知工具扩展

**问题**：Agent 只知道自己的角色设定，不知道自己的工具能力边界，有时会拒绝本可做到的任务。

**修改**（`buildin_tools/self_info.py` 新增 `get_self_info` 工具）：返回完整自我认知档案——身份、运行框架、能力边界（按分类汇总已注册工具）、诚实边界、主人列表、当前会话语境标签。该工具加入框架保底工具池。

**核心变动**（`buildin_tools/self_info.py`）：

```diff
+ from gsuid_core.ai_core.register import get_registered_tools   # register.py 既有 accessor

+ @ai_tools(category="buildin")     # buildin 分类 → 自动进入保底工具池
+ async def get_self_info(ctx: RunContext[ToolContext]) -> str:
+     """获取自身的完整自我认知信息（身份/能力边界/主人）。"""
+     # 按分类汇总已注册工具，作为"能力边界"
+     for cat, tools in get_registered_tools().items():
+         capability_lines.append(f"  [{cat_labels.get(cat, cat)}] {'、'.join(tools)}")
+     # 主人列表 + 当前会话语境标签
+     masters = core_config.get_config("masters") or []
+     return "\n".join([...身份/框架/能力边界/诚实边界/主人/会话语境...])
```

> `get_registered_tools()` 不是新引入的名字——它是 `register.py` 既有的 accessor（`def get_registered_tools() -> Dict[str, Dict[str, ToolBase]]`，直接返回内部注册表 `_TOOL_REGISTRY`）。`get_self_info` 在函数内 `from ... import get_registered_tools` 引入，运行时不会 NameError。

### P1-7 连续无工具调用检测

**问题**：Agent 可能连续多轮只输出角色化推脱文本、不调用任何工具。

**修改**（`gs_agent.py`）：新增实例计数器 `_consecutive_no_tool_rounds`。交互式主 Agent 每轮结束后，若本轮无工具调用则计数 +1，否则归零；当计数 ≥2 时，在下一轮用户消息中注入强制提醒，要求立即检查工具列表或明确说明无工具可用——禁止以角色不懂为由跳过。

**注意 `final_user_message` 的类型**：`_prepare_user_message()` 在消息含图片且模型支持图片时返回的是 `list[UserContent]`，否则返回 `str`。因此**任何向 `final_user_message` 追加文本的地方都必须按类型分支**——对 `str` 用 `+=`，对 `list` 用 `.append()`。直接 `list += str` 会抛 `TypeError`。本项（连续无工具提醒）与 P1-5（RAG 上下文拼接）都遵循该规则。

**核心变动**（`gs_agent.py`）：

```diff
+ # __init__：实例级计数器
+ self._consecutive_no_tool_rounds: int = 0

+ # 运行前：连续 ≥2 轮无工具调用 → 注入强制提醒
+ if self.create_by in ["Chat", "Agent"] and self._consecutive_no_tool_rounds >= 2:
+     no_tool_reminder = (
+         "\n\n【⚠️ 系统检测】你已连续多轮未调用任何工具……"
+         "请立即检查工具列表，选择最合适的工具调用——禁止以角色不懂为由跳过工具。")
+     # final_user_message 可能是 str 也可能是 list[UserContent]，必须按类型分支
+     if isinstance(final_user_message, str):
+         final_user_message += no_tool_reminder
+     elif isinstance(final_user_message, list):
+         final_user_message = list(final_user_message)
+         final_user_message.append(no_tool_reminder)

+ # 运行后：更新计数
+ if self.create_by in ["Chat", "Agent"]:
+     self._consecutive_no_tool_rounds = 0 if _tool_call_list else self._consecutive_no_tool_rounds + 1
```

---

## P2 中期新增

### P2-1 群组画像

**新增模块** `gsuid_core/ai_core/memory/group_profile.py`：维护每个群组的整体语境特征。

- **语境标签**：摄入实体时累计标签频次（`record_entity_tags`），`get_context_tags` 按频次返回主要话题标签。
- **词汇映射表**：`record_term_mappings` 记录别名→正式名称，`get_term_mappings` 查询。
- **底层存储**：复用 P1-1 的通用持久状态存储，无需独立数据表。scope 用**保留命名空间** `"__gscore_group_profile__"`——带双下划线，与用户/插件的 scope 形式（`user:xxx` / `group:xxx` / `global`）区分开，避免某个插件恰好用了同名 scope 而覆盖框架内部数据。
- **并发安全**：`record_entity_tags`（频次累加）与 `record_term_mappings`（映射合并）本质都是"读-改-写"。它们**不走** `state_get→改→state_set` 三步（并发摄入下会丢更新），而是统一走 P1-1 的 `state_mutate` 乐观锁原语——传入一个纯函数 mutator，由 `state_mutate` 在版本冲突时基于最新值重试。
- 设有容量上限（词汇映射 60 条、标签 40 个）防止无限膨胀。

**核心变动**（新增 `memory/group_profile.py` 的关键接口）：

```python
# 复用 state_store 作为底层存储，scope = "__gscore_group_profile__"（保留命名空间）
async def record_term_mappings(scope_key, mappings):   # 记录别名→正式名称（state_mutate）
async def record_entity_tags(scope_key, tags):         # 累计实体标签频次（state_mutate）
async def get_context_tags(scope_key, top_n=8):        # 按频次返回主要语境标签
async def get_term_mappings(scope_key):                # 查询词汇映射表
def expand_query_with_aliases(query, term_mappings):   # query 中出现别名则附加正式名称
async def format_context_injection(scope_key):         # 生成【当前群聊语境】注入文本

# record_entity_tags 内部：用 state_mutate 做并发安全的频次累加
async def record_entity_tags(scope_key, tags):
    meaningful = [t for t in tags if t and t not in IGNORE_TAGS]
    if not meaningful:
        return
    def _mutate(current):                 # 纯函数，冲突时会被重试
        profile = _as_profile(current, scope_key)
        tag_counts = dict(profile["tag_counts"])
        for t in meaningful:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        profile["tag_counts"] = _cap(tag_counts, _MAX_TAGS)   # 容量上限
        return profile
    await state_mutate(_PROFILE_SCOPE, scope_key, _mutate)
```

**接入点**（`memory/ingestion/worker.py` 的 flush 流程，Step 4.5）：群组画像的语境标签靠 `record_entity_tags` 在**每次实体摄入时**累计——若漏掉这个接入点，群组标签会永远为空，P2-2 语境工具池也就永远无法生效。该调用与 P1-3 的别名重定向在同一处接入：

```diff
  # Step 4: LLM 提取
  extracted = await _llm_extract(dialogue, scope_key)

  # Step 4.5: 别名重定向 + 维护群组画像
  alias_map = _apply_alias_redirection(extracted)
+ from gsuid_core.ai_core.memory.group_profile import (
+     record_entity_tags, record_term_mappings,
+ )
+ if alias_map:
+     await record_term_mappings(scope_key, alias_map)   # 别名→正式名称
+ # 汇总本批次所有实体的 tag，累计到群组画像的语境标签频次
+ all_tags = []
+ for _e in extracted.get("entities", []):
+     all_tags.extend(_e.get("tag") or [])
+ if all_tags:
+     await record_entity_tags(scope_key, all_tags)
```

### P2-2 语境工具池

**问题**：群里问游戏问题时，向量搜索命中不到游戏插件工具。

**修改**：
- `models.py` 的 `ToolBase` 新增 `context_tags` 字段；`register.py` 的 `@ai_tools` 新增 `context_tags` 参数，插件可声明工具适用语境（如 `context_tags=["原神", "游戏"]`）。
- `rag/tools.py` 新增 `get_tools_by_context_tags()`（按标签重合度匹配工具）与 `get_scope_context_tags()`（读群组画像标签）。
- `gs_agent.py` 工具组装阶段，群聊场景下根据群组画像标签自动加载匹配的语境工具集（最多 8 个）。

**核心变动**：

```diff
# models.py / register.py —— @ai_tools 新增 context_tags 注册字段
  class ToolBase:
+     context_tags: List[str]      # 语境标签，用于语境工具池自动加载

  def ai_tools(func=None, *, category="default", check_func=None,
+              context_tags: Optional[List[str]] = None,
               **check_kwargs):
```

```python
# rag/tools.py —— 按标签重合度匹配工具
def get_tools_by_context_tags(tags: List[str], max_count: int = 8) -> ToolList:
    tag_set = {t.lower() for t in tags}
    scored = []
    for tool_base in get_all_tools().values():
        overlap = len({t.lower() for t in tool_base.context_tags} & tag_set)
        if overlap > 0:
            scored.append((overlap, tool_base.tool))
    scored.sort(key=lambda x: x[0], reverse=True)   # 按匹配标签数降序
    return [tool for _, tool in scored[:max_count]]
```

插件声明语境标签示例：

```diff
- @ai_tools(category="genshin")
+ @ai_tools(category="genshin", context_tags=["原神", "Genshin", "游戏"])
  async def get_genshin_characters(ctx, user_id: str) -> str:
      """获取指定用户的原神角色列表及练度信息"""
```

### P2-3 语境注入

**修改**（`handle_ai.py`）：构建对话上下文时注入【当前群聊语境】文本（主要话题 + 词汇映射说明），让 Agent 直接知道"深渊"在本群指什么、某个外号对应哪个角色，无需自行推理。文本由 `group_profile.format_context_injection()` 生成。

**输出格式与冷启动行为**：`format_context_injection()` 生成形如下面的多行文本——

```
【当前群聊语境】
主要话题: 原神、深渊、抽卡
语境说明（群内特有词汇）:
  - "深渊" = 深境螺旋
  - "大班尼特" = 妮可
```

新群**冷启动**时群组画像为空（无标签、无词汇映射），此时该函数**返回空字符串**，调用处 `if group_context_text:` 判空后不会注入任何内容——不会注入空的占位标题。这意味着语境消歧（如"深渊"自动展开）需要群组积累一定对话、画像非空后才会生效，属于预期行为：冷启动期 Agent 退化为依赖自身推理，不会因此报错或注入噪声。

**核心变动**（`handle_ai.py`）：

```diff
+ # 群组语境注入
+ group_context_text = ""
+ if event.group_id:
+     group_context_text = await format_context_injection(
+         make_scope_key(ScopeType.GROUP, str(event.group_id)))

  context_parts = []
  if rag_context:
      context_parts.append(rag_context)
+ if group_context_text:
+     context_parts.append(group_context_text)      # 【当前群聊语境】
```

### P2-4 定时任务结构化上下文

**问题**：定时任务描述是纯文本，SubAgent 执行时需重新"理解"，且无法获取上次执行结果。

**修改**：
- `scheduled_task/models.py` 的 `AIScheduledTask` 新增 `structured_context`（创建任务时填写的结构化 JSON 字符串）与 `last_result_summary`（上次执行结果摘要）。
- `scheduled_task/executor.py` 构建任务消息时注入结构化上下文、上次结果摘要、执行次数；执行后把结果摘要写回 `last_result_summary`，供循环任务的下次执行参考。
- **执行失败也要回写**：`session.run()` 抛异常（网络超时、token 超限等）时，原本只把 `status` 置 `failed`，`last_result_summary` 保留上一次的成功值——循环任务下次执行会读到过期的"上次结果"，完全不知道上次已失败。现在 `except` 分支也写 `last_result_summary`，置为 `[上次执行失败] {错误信息}`，让下次执行的 SubAgent 能感知上次失败。

**核心变动**：

```diff
# scheduled_task/models.py —— AIScheduledTask 新增字段
  task_prompt: str = Field(title="任务描述")
+ structured_context: Optional[str] = Field(title="结构化上下文", default=None)
+ last_result_summary: Optional[str] = Field(title="上次执行结果摘要", default=None)
```

```diff
# scheduled_task/executor.py —— 注入结构化上下文 + 回写结果摘要
+ context_block = ""
+ if task.structured_context:
+     context_block += f"\n\n【结构化上下文】\n{task.structured_context}"
+ if task.last_result_summary:
+     context_block += f"\n\n【上次执行结果摘要】\n{task.last_result_summary}"
  task_message = f"【定时任务执行】……任务内容：{task.task_prompt}{context_block}"

  result = await session.run(user_message=task_message, ...)
+ result_summary = str(result)[:200] if result else None
  await AIScheduledTask.update_data_by_data(
      select_data={"task_id": task_id},
-     update_data={"status": "executed", "result": result},
+     update_data={"status": "executed", "result": result,
+                  "last_result_summary": result_summary},
  )
```

```diff
# scheduled_task/executor.py 的 except 分支 —— 失败时同样回写 last_result_summary
  except Exception as e:
      await AIScheduledTask.update_data_by_data(
          select_data={"task_id": task_id},
          update_data={
              "status": "failed",
              "executed_at": datetime.now(TZ_SHANGHAI),
              "error_message": str(e),
+             "last_result_summary": f"[上次执行失败] {str(e)[:150]}",
          },
      )
```

### P2-5 工具前摇代码层触发

**问题**：前摇（工具调用前的角色化台词）完全依赖 LLM 自主生成，常被遗漏，用户面对沉默等待。

**修改**（`gs_agent.py`）：
- 新增框架默认前摇台词字典 `_FRAMEWORK_PRE_TOOL_EXPRESSIONS`（仅针对 web_search、create_subagent、render 等耗时工具）。
- **框架默认台词必须人格中性**：这是框架级默认值，会被**任何** Persona 套用，因此不得带特定角色（如早柚）的口吻或语气，否则换一个 Persona（傲娇系、正经系）的用户一上来就会被"附身"出戏。带角色个性的台词应由各 Persona 自行在 `config.json` 的 `pre_tool_expressions` 字段中提供。
- Persona 可在 `config.json` 的 `pre_tool_expressions` 字段覆盖（值为列表时随机取一句，空字符串表示无需前摇）。
- 在 `CallToolsNode` 检测到 `ToolCallPart` 时，按工具名查模板并立即发送前摇。每次运行最多发送 2 句，防止刷屏。

**核心变动**（`gs_agent.py`）：

```diff
+ # 框架默认前摇台词（仅耗时工具）——保持人格中性，角色专属台词写进各 Persona 配置
+ _FRAMEWORK_PRE_TOOL_EXPRESSIONS = {
+     "web_search_tool": "稍等，我查一下相关信息…",
+     "search_knowledge": "让我先查一下资料…",
+     "web_fetch_tool": "我打开这个链接看看…",
+     "create_subagent": "这个任务我来安排处理…",
+     "render_html_to_image": "稍等，正在生成图片…",
+     "render_markdown_to_image": "稍等，正在生成图片…",
+ }
+ # 取台词：Persona config.json 的 pre_tool_expressions 优先，否则用框架默认

  for part in node.model_response.parts:
      if isinstance(part, ToolCallPart):
          _tool_call_list.append(part.tool_name)
+         # 代码层前摇触发：耗时工具调用前主动发送角色化台词
+         if bot and return_mode in ["always", "by_bot"] and _pre_tool_sent < 2:
+             pre_expr = _get_pre_tool_expression(self.persona_name, part.tool_name)
+             if pre_expr:
+                 _pre_tool_sent += 1
+                 await send_chat_result(bot, pre_expr, ev=ev)
```

> **示例**：早柚人格的"唔…翻一下情报…""呜呼影分身之术！"等专属台词，应放在早柚 Persona 的 `config.json` 的 `pre_tool_expressions` 中，而非框架默认字典里。

**`_get_pre_tool_expression` 取词逻辑**（`gs_agent.py` 已实现，非空函数）：

```python
def _get_pre_tool_expression(persona_name, tool_name) -> Optional[str]:
    # 1. 读 Persona config.json 的 pre_tool_expressions（带 _persona_pre_tool_cache 缓存，
    #    读取/解析失败则视为空表，不抛异常）
    # 2. 按 [Persona 表, 框架默认表] 顺序查 tool_name：
    #    - 命中值为 list → random.choice 随机取一句（空 list → ""）
    #    - 命中值为 str  → 直接用
    #    - strip 后为空字符串 → 返回 None（该工具显式声明"无需前摇"）
    # 3. 两张表都没有该工具 → 返回 None
```

边界处理已覆盖：列表随机选、空字符串=显式跳过、配置读取失败回退框架默认、每次运行最多发 2 句由调用处的 `_pre_tool_sent` 计数控制。

---

## P3 深度拟人化

### P3-1 情绪与主人联动

**修改**（`handle_ai.py` 的 `_update_persona_mood`）：新增 `is_master` 参数，主人发言时额外触发一次"温暖"情绪事件（`greeting`，强度 0.35），让 Agent 对主人的发言产生正面情绪积累。

> `greeting` 是情绪系统**既有**的事件类型——`persona/mood.py` 的 `event_to_mood` 映射中 `"greeting": MoodType.WARM`，此前已用于"用户友好问候"。本项只是复用它，并非新增类型。即便传入未知事件类型，`update_mood` 也会 `event_to_mood.get(event_type, MoodType.NEUTRAL)` 兜底为中性，不会抛异常。

**核心变动**（`handle_ai.py`）：

```diff
  async def _update_persona_mood(persona_name, group_id, user_message,
+                                is_master: bool = False):
+     # 主人发言：带来温暖情绪（独立于关键词命中）
+     if is_master:
+         await update_mood(persona_name, group_id, "greeting", 0.35, "主人发言了")
      text = user_message.lower()
      ...

  # 调用处传入 is_master
  _update_persona_mood(persona_name=session.persona_name, group_id=mood_key,
-                      user_message=query)
+                      user_message=query, is_master=_is_master_user(str(event.user_id)))
```

### P3-2 工具 description 标准化

**问题**：工具描述写法影响向量检索质量，口语化 query 命中不到描述过于技术性的工具。

**修改**：按"触发条件 + 输出"规范重写关键内置工具描述：
- `web_search_tool`：强调"最新/现在/今天/最近/怎么了"等时效性、开放性触发词，并说明可作兜底查询。
- `search_knowledge`：强调"怎么打/有什么技能/属性是什么"等专业问题触发词。
- `web_fetch_tool`：明确"已有具体 URL 时读取正文"的使用边界。

**核心变动**（以 `web_search_tool` 的 docstring 为例）：

```diff
  """
  Web搜索工具
-
- 根据配置使用Tavily等供应商进行网络搜索，当需要查询任何即时信息时,
- 如天气、股票、游戏、公告等，调用此工具。返回搜索结果列表。
+
+ 当需要查询实时信息、最新消息、当前价格、近期事件、今日/本周/本月发生的事情，
+ 或遇到任何不确定、不了解的话题时使用。适合"最新""现在""今天""最近""怎么了"
+ "是什么""出了什么事"这类时效性或开放性问题，也可作为没有专属工具时的兜底查询。
+ 返回搜索引擎的结果摘要列表。
  """
```

> 工具描述变更会改变 `sync_tools` 计算的 hash，下次启动自动重新向量化。

---

## 未实现项与取舍说明

以下两项经评估后做了保守取舍，未在本次落地：

1. **主人记忆锚点（DB `is_anchor` 列）**：原方案建议在 `AIMemEntity` 增加 `is_anchor` 列并接入记忆老化机制。该改动需要数据库 schema 迁移并改造老化系统，成本较高；而主人识别已由"配置项 + 好感度初始化 + 说话者高亮 + `get_self_info`"四层机制充分保障，故未引入该列。

2. **Heartbeat 记忆驱动主动发言**：原方案建议改造 `heartbeat/inspector.py` 的主动发言决策逻辑。该模块是主动消息路径，未经充分测试的改动有刷屏风险，故本次未改动，留作后续工作。

---

## 数据库变更

| 变更 | 类型 | 生效方式 |
|------|------|---------|
| `AIPersistentState` 表 | 新增表 | `state_store/store.py` 的 `_ensure_table()` 在首次读写前针对性建表（进程级 flag 仅执行一次）；框架全局 `create_all` 也会创建，二者均幂等 |
| `aischeduledtask.structured_context` 列 | 新增列 | `utils/database/startup.py` 的 `ALTER TABLE` 语句 |
| `aischeduledtask.last_result_summary` 列 | 新增列 | 同上 |

新增列对旧库幂等兼容（`ALTER TABLE` 失败被静默忽略，列已存在时无影响）。

> **为何不依赖全局 `create_all`**：框架的 `SQLModel.metadata.create_all` 在 webconsole 后台初始化阶段执行，与 AI 重依赖（`buildin_tools → state_store`，`AIPersistentState` 模型经此链注册）的导入是**并发**的——若 `create_all` 先跑，模型尚未注册，本表就不会被建。`_ensure_table()` 在 state_store 首次操作前显式建表，使其不依赖启动时序。`AIPersistentState` 含 `(scope, state_key)` 唯一约束（`uq_state_scope_key`），由于本表是全新表，所有部署都会带约束创建，无需迁移。

---

## 完整文件清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `gsuid_core/ai_core/state_store/__init__.py` | 通用持久状态存储模块导出 |
| `gsuid_core/ai_core/state_store/models.py` | `AIPersistentState` 数据库模型 |
| `gsuid_core/ai_core/state_store/store.py` | 状态存储核心读写逻辑 |
| `gsuid_core/ai_core/state_store/tools.py` | `state_*` AI 工具 |
| `gsuid_core/ai_core/memory/group_profile.py` | 群组画像（语境标签 + 词汇映射表） |
| `docs/AI_AGENT_CAPABILITY_UPGRADE.md` | 本文档 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `gsuid_core/ai_core/gs_agent.py` | 调试信息泄露修复；保底/语境工具池注入；图片二次摘要；连续无工具调用检测；前摇代码层触发 |
| `gsuid_core/ai_core/rag/tools.py` | 保底工具池、语境工具池相关函数 |
| `gsuid_core/ai_core/persona/prompts.py` | 决策树重写；绝对禁止行为；SubAgent 结构化模板 |
| `gsuid_core/ai_core/ai_router.py` | 主人好感度初始化 |
| `gsuid_core/ai_core/utils.py` | 主人判定与说话者感知高亮 |
| `gsuid_core/ai_core/models.py` | `ToolBase` 新增 `context_tags` 字段 |
| `gsuid_core/ai_core/register.py` | `@ai_tools` 新增 `context_tags` 参数 |
| `gsuid_core/ai_core/handle_ai.py` | 群组语境注入；情绪主人联动；记忆注入字符预算改用配置项 |
| `gsuid_core/ai_core/configs/ai_config.py` | 新增 `memory_inject_max_chars` 记忆配置项 |
| `gsuid_core/ai_core/memory/config.py` | 新增 `memory_inject_max_chars` 属性 |
| `gsuid_core/ai_core/buildin_tools/__init__.py` | 注册 state_* 工具与 `get_self_info` |
| `gsuid_core/ai_core/buildin_tools/self_info.py` | 新增 `get_self_info` 工具 |
| `gsuid_core/ai_core/buildin_tools/web_search.py` | 工具 description 标准化 |
| `gsuid_core/ai_core/buildin_tools/rag_search.py` | 工具 description 标准化 |
| `gsuid_core/ai_core/buildin_tools/web_fetch.py` | 工具 description 标准化 |
| `gsuid_core/ai_core/memory/ingestion/worker.py` | 别名重定向接入；群组画像维护（标签 / 词汇映射） |
| `gsuid_core/ai_core/memory/retrieval/dual_route.py` | Token 预算格式化；别名展开检索；`_complete_fact_subject` 事实主语补全 |
| `gsuid_core/ai_core/scheduled_task/models.py` | 新增 `structured_context` / `last_result_summary` 字段 |
| `gsuid_core/ai_core/scheduled_task/executor.py` | 结构化上下文注入与结果摘要回写；执行失败时也回写 `last_result_summary` |
| `gsuid_core/utils/database/startup.py` | 定时任务表新增列的 `ALTER TABLE` 语句 |
| `gsuid_core/core.py` | 启动横幅新增"AI 核心统计"日志行（AI 工具数 / Trigger 工具数 / 人格数 / 配置文件数） |

---

## 修订记录（2026-05-17 Review 修正）

对首版落地（2026-05-16）做了一轮 Code Review，发现并修正了以下问题。其中 ③④⑤⑧⑩ 为**代码改动**，①②⑥⑦⑨ 为首版**文档与实际代码不符**、本次已让文档对齐真实代码。

| 编号 | 位置 | 问题 | 处理 |
|------|------|------|------|
| ① | P1-7 | 文档示意 `final_user_message += str`，但该值可能是 `list[UserContent]`，`list += str` 会 `TypeError` | 实际代码已按类型分支（`str` 用 `+=` / `list` 用 `.append()`）；本次修正文档 diff 与之对齐 |
| ② | P1-5 | 文档未说明 `_summarize_image_description` 的 `user_question` 从何而来 | 实际由 `_prepare_user_message()` 内部就地从 `text_parts` 计算，无跨函数参数链；本次在文档中写明 |
| ③ | P1-1 | `state_append_item` 声明了 `version` 乐观锁却走简单读-改-写，并发追加会静默丢数据 | **代码修正**：`(scope,state_key)` 加唯一约束，`state_append_item` 改为「读 version → 条件更新 → 失败重试」乐观锁 |
| ④ | P2-5 | 框架默认前摇台词用了早柚专属口吻，换 Persona 会出戏 | **代码修正**：`_FRAMEWORK_PRE_TOOL_EXPRESSIONS` 改为人格中性台词，角色专属台词归各 Persona 配置 |
| ⑤ | P0-4 | `set_favorability` 第 4 参数（`user_name` 昵称）误传了 `master_id` | **代码修正**：去掉该实参，留空由默认值处理 |
| ⑥ | P1-3 | 文档里传递性别名解析只有注释、像未实现 | 实际代码 `_resolve` 递归（深度上限兼作环路保护）已实现链式解析；本次补全文档 diff |
| ⑦ | P2-1 | 文档缺 `record_entity_tags` 在 `worker.py` 的接入点 diff | 实际代码 Step 4.5 已接入；本次补上文档 diff |
| ⑧ | P1-1 | `AIPersistentState` 表依赖全局 `create_all`，但其与模型导入是并发的，建表时序不确定 | **代码修正**：新增 `_ensure_table()`，首次读写前针对性建表，与启动时序解耦 |
| ⑨ | P0-2 | `gs_agent.py` 的 diff 基准对不上原始代码 | 本次加注说明该 diff 为概念化简写、非逐字补丁，回滚/review 应以 `git log` 为准 |
| ⑩ | P1-4 | `to_prompt_text` 的 `max_chars=2000` 硬编码，密集群可能裁掉有用上下文 | **代码修正**：新增 `memory_inject_max_chars` 配置项（默认 2000，可选 1000~16000），`handle_ai.py` 按配置传入 |

### 第二轮 Review 修正（同日）

对首轮修正后又做了一轮 Review。这一轮明确：**`extraction.py` 保持原样不动**——记忆事实的"完整性/主语"问题不在提取端解决，而在检索端由 `dual_route.py` 的 `_complete_fact_subject` 拼接主语（主语本就在 edge 的 `source_name` 上）。据此 P1-2 重写为"取舍说明"、P1-4 标题与实现对齐。

| 编号 | 位置 | 问题 | 处理 |
|------|------|------|------|
| ① | P1-5 | 仅发图片、无文字时 `user_question` 为空字符串 | 实际无问题：摘要 prompt 用 `user_question or '（无明确问题）'` 兜底并指示"概括图片主旨"，仍输出一句话摘要；本次文档写明该行为 |
| ② | P1-5 | 文档把 `_summarize_image_description` 函数体写成 `...` 占位，像未实现 | 实际代码是完整实现（low 级模型 + 失败回退原描述，绝不返回 None）；本次文档展开真实实现 |
| ③ | P1-3 | `_apply_alias_redirection` 文档 diff 未从 `extracted` 解包 `entities`/`edges` | 实际代码函数开头已 `extracted.get(...)` 解包；本次补全文档 diff |
| ④ | P0-2 | `ctx_tools` 赋值点缺失、`dedup` 像未定义 | 实际 `ctx_tools` 由群聊分支内 `get_tools_by_context_tags` 赋值、去重是内联循环；本次文档改用与仓库一致的真实结构 |
| ⑤ | P1-6 | `get_registered_tools()` 像未定义 | 实际是 `register.py` 既有 accessor（返回 `_TOOL_REGISTRY`）；本次文档加注并补 import 行 |
| ⑥ | P2-5 | `_get_pre_tool_expression` 像无实现 | 实际 `gs_agent.py` 已完整实现（含缓存 / 列表随机 / 空串跳过 / 读取失败兜底）；本次文档补出取词逻辑 |
| ⑦ | P2-4 | 任务执行抛异常时 `last_result_summary` 不回写，循环任务下次读到过期摘要 | **代码修正**：`except` 分支也写 `last_result_summary = "[上次执行失败] …"` |
| ⑧ | P2-1 | `record_entity_tags` / `record_term_mappings` 是简单读-改-写，并发摄入会丢计数 | **代码修正**：新增 `state_mutate` 乐观锁原语，两个 record 函数改用它；`state_append_item` 也重构为其薄封装 |
| ⑨ | P2-1 | 群组画像 `scope="group_profile"` 与插件 scope 有碰撞风险 | **代码修正**：改为保留命名空间 `"__gscore_group_profile__"` |
| ⑩ | P3-1 | `update_mood` 的 `"greeting"` 事件类型是否存在未说明 | 实际是 `mood.py` 既有类型（`greeting→WARM`），未知类型也会兜底为中性；本次文档写明 |
| ⑪ | P1-2 | 提取 prompt 的"步骤3 检查"放在输出格式后、LLM 可能跳过 | 据本轮决定：不改 `extraction.py`，主语补全移到检索层 `_complete_fact_subject`（见 P1-4）；P1-2 重写为取舍说明 |
| ⑫ | P2-3 | `format_context_injection` 输出格式与冷启动行为未说明 | 实际冷启动（画像为空）返回空字符串、调用处判空跳过；本次文档补出输出示例与冷启动说明 |

### 第三轮调整（定时任务工具分类 + LLM.md 合规）

在 P0-2 保底工具池落地后，发现保底池规模偏大（约 22 个工具），与方案"降低 Token 消耗"的初衷相悖。经评估做出以下调整：

| 编号 | 位置 | 问题 | 处理 |
|------|------|------|------|
| ① | P0-2 | 8 个定时任务工具全注册为 `self`，使保底池膨胀到约 22 个 | **代码修正**：`add_once_task` / `add_interval_task` 两个"创建"入口保留 `self`（口语化触发，向量难命中）；`list` / `query` / `modify` / `cancel` / `pause` / `resume` 六个"管理"类工具改归 `common`，保底池收敛到约 16 个。同时重写六者 docstring——意图与触发词前置、删除冗长 `Examples` 块，避免长描述稀释 embedding 主题 |
| ② | 全局 | 本轮新增/改动代码须符合 `docs/LLM.md`（禁止 `getattr` / `dict.get` 等兜底语法） | **代码修正**：消除新代码中的 `getattr`（`Event` / `ToolContext` 等有类型 dataclass 改直接访问）；`group_profile.py` 引入 `GroupProfileData(TypedDict)` + `_normalize()` 守卫；`worker.py` 新增 `ExtractedResult(TypedDict)`，`_restore_keys` 改用 `in` + `isinstance` 守卫规整 LLM 原始 JSON；日志 API 在 `logger.py` 引入 `LogEntry(TypedDict)`，`logs_api.py` 全部 `.get` 改为类型化访问 |
