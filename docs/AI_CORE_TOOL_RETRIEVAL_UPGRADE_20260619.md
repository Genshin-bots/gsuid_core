# GsCore AI 工具检索三期升级说明（2026-06-19）

> 本文档说明 2026-06-19 这一批针对 `ai_core` **工具检索 / 装配链路**的三期升级：
> **要解决的问题、各期设计与实现、改动文件、取舍细节、风险与回滚、验证情况**。
>
> 源码为唯一事实源。本次升级不依赖任何外部设计稿，全部改动均在本文档登记。

---

## 目录

1. [背景与问题](#1-背景与问题)
2. [现有分层工具池回顾](#2-现有分层工具池回顾)
3. [三期改动总览](#3-三期改动总览)
4. [Phase 1：召回侧改进](#4-phase-1召回侧改进)
5. [Phase 2：渐进式工具暴露](#5-phase-2渐进式工具暴露)
6. [Phase 3：两段式 domain 检索 + 条件隐藏](#6-phase-3两段式-domain-检索--条件隐藏)
7. [取舍细节](#7-取舍细节)
8. [风险与回滚](#8-风险与回滚)
9. [验证情况](#9-验证情况)
10. [附录 A：核心代码与调用链](#10-附录-a核心代码与调用链)
11. [跟进（2026-06-20）：保底池条件隐藏](#11-跟进20260620保底池条件隐藏)

---

## 1. 背景与问题

主 Agent 每轮在 `GsCoreAIAgent._execute_run`（`gs_agent.py`）里**按本轮用户消息**做工具
向量检索、把命中工具静态装配进 `Agent(tools=...)`。这套机制在**跨轮澄清**时会漏召回：

> 用户「帮我查查今天的天气」→ 系统据此召回天气工具，AI 反问「你要查哪个城市？」→
> 用户「广州」→ 这一轮**只拿「广州」去检索**，召不回任何天气工具，AI 失去能力。

两个深层痛点：

- **跨轮澄清漏召回**：澄清补全（「广州」/「下午 3 点」/「对」）本身没有独立的工具语义，
  关键信号往往落在 **bot 的反问**里（如「要渲染成图片吗？」），而旧检索 query 不含 bot 那句。
- **每轮工具过多**：保底池 + 向量召回 + 族展开后，闲聊一句也常挂二十个左右工具 schema，
  既抬 Token 又稀释工具选择精度。

并且业务上有一个长期疑问：**pydantic-ai 能不能"给 AI 一个搜工具的工具"**，让模型在
推理过程中按需把工具拉进来？——答案是**能**（见 Phase 2）。

---

## 2. 现有分层工具池回顾

本次升级是在既有分层之上做增强，先明确现状（`gs_agent.py` / `rag/tools.py` /
`tool_state_signals.py`）：

| 层 | 机制 | 作用 |
|---|---|---|
| L1 保底池 | `get_main_agent_tools()`：`self`(白名单) + `buildin` 无条件加载 | 框架基础能力常驻 |
| L2 状态驱动 | `get_state_driven_family_tools()`：按用户持久实体补能力族 | 跨轮追问定时任务 / Kanban / record |
| L3 会话驻留 | `_recent_tool_families`（sticky 3 轮） | 刚用过的族继续常驻数轮 |
| 语境池 | `get_tools_by_context_tags()` | 群画像标签匹配工具 |
| L4 族展开 | `expand_tools_to_families()` | 召回任一工具即带出整族 |
| L5 上文增强检索 | `_recent_user_texts` 拼进检索 query | 「改成后天吧」借上文召回 |

本次升级**不改动** L1–L5 的既有分层行为，Phase 1 只增强**召回侧**
（Reranker 精排 + 降 limit），并新增 **渐进式工具暴露（Phase 2）** 与
**domain 粒度检索 / 条件隐藏（Phase 3）**。跨轮澄清漏召回交由 Phase 2 通用解决。

---

## 3. 三期改动总览

| 期 | 主题 | 默认状态 | 核心文件 |
|---|---|---|---|
| 1 | `search_tools` 接 Reranker 精排；降 limit | 开 | `gs_agent.py`、`rag/tools.py` |
| 2 | 渐进式工具暴露（`find_tools` meta-tool + `RetrievableToolset`） | 开（非闲聊轮） | `dynamic_toolset.py`、`buildin_tools/dynamic_tool_discovery.py`、`models.py`、`gs_agent.py` |
| 3 | 两段式 domain 检索；`visible_when` 条件隐藏 | 开 | `rag/tools.py`、`register.py`、`buildin_tools/command_executor.py` |

---

## 4. Phase 1：召回侧改进

> L5 的既有 `_recent_user_texts` 上文增强保持原样。**跨轮澄清漏召回**不再用专门的
> "上轮 bot 问句拼接"规则（太刻意、易误伤），改由 Phase 2 的 `find_tools` 通用兜底：
> 模型借 message_history 里的上文（含 bot 的反问）自行判断缺哪个工具并现拉。

### 4.1 `search_tools` 接入 Reranker 二次精排

**实现**（`rag/tools.py`）：

- `search_tools` 新增 `rerank: bool = True`。启用 rerank 功能时，向量侧先粗召回
  `_RERANK_RECALL_LIMIT`(=20) 个候选，做完 **category/non_category/隐藏分类**过滤后，
  用交叉编码 Reranker 按 `工具名 + 描述` 组档精排，最后裁到调用方的 `limit`。
- 新增 `_rerank_tool_candidates()` 工具专用精排（区别于 `rerank_results` 的 `title/content` 组档）。
- **降级一致性**：Reranker 未启用 / 候选不足 / 异常时一律退回"按向量分数取前 limit"，
  与未接 Reranker 时行为完全一致。

### 4.2 降低 limit

`gs_agent.py` 主装配路径：向量检索 `limit` 8→**4**、`MAX_EXTRA_TOOLS` 16→**8**。
Reranker 精排后召回质量更高，少而准的种子经 L4 族展开即可覆盖需求，附加池规模减半。

---

## 5. Phase 2：渐进式工具暴露

> 直接回答业务疑问：**pydantic-ai 1.x 原生支持"运行时动态工具集"**——
> 自定义 `AbstractToolset.get_tools(ctx)` 会在**每个 step** 被重新调用，可从
> `ctx.deps` 读共享状态动态决定本步暴露哪些工具。于是"模型调一次检索工具 →
> 下一步那些工具就真的可调用"完全可以在框架内闭环。

旧的 `discover_tools` 之所以"看起来不支持"，是因为它**只返回文本描述**、工具并未变可调用，
且每轮 `Agent(tools=...)` 是静态列表、turn 内无法扩容。Phase 2 用真正的动态 toolset 修正这点。

### 5.1 三个组成部分

1. **`ToolContext.dynamic_tool_names: Set[str]`**（`models.py`）——单轮共享集合。
   `ToolContext` 每轮新建，故作用域天然是"单次 run"。
2. **`find_tools` meta-tool**（`buildin_tools/dynamic_tool_discovery.py`，`category="meta"`）——
   模型发现缺工具时调用，按需检索并把命中工具名写进 `ctx.deps.dynamic_tool_names`，
   返回简短清单。它**不声明 capability_domain**（避免被 L3 sticky 带进随后闲聊轮）。
3. **`RetrievableToolset(AbstractToolset)`**（新文件 `dynamic_toolset.py`）——
   `get_tools(ctx)` 在每个 step 读 `dynamic_tool_names`，把名字逐个 `find_tool_base` +
   `prepare_tool_def` 解析成可调用的 `FunctionToolsetTool`。只解析集合内的名字（不遍历全量
   注册表），并用 `exclude_names`（本轮静态已装配工具名）去重，避免跨 toolset 重名冲突。

### 5.2 装配门控（`gs_agent.py`）

- 总开关 `ENABLE_PROGRESSIVE_TOOLS = True`。
- **意图门**：仅在 `intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS`（即非「闲聊」）的
  自动装配轮，注入 `find_tools` 并挂载 `RetrievableToolset`；闲聊轮完全跳过（零开销）。
- 这样高频/无工具需求的闲聊保持原样，长尾意图才付"可能多一次 round-trip"的代价。
- `meta` 分类已加入 `NON_SEARCHABLE_TOOL_CATEGORIES`：`find_tools` 永不被向量检索召回，
  只由 gs_agent 按门控**显式注入**。

### 5.3 协同关系

`find_tools` 是**安全网**而非替代：静态召回（L1–L5）仍处理常见场景（不付额外往返），
模型只在"静态召回没给到所需工具"时才调 `find_tools` 现拉。两者互补。

---

## 6. Phase 3：两段式 domain 检索 + 条件隐藏

### 6.1 两段式·domain 粒度检索（`rag/tools.py`）

新增 `search_tools_by_domain(query, domain_limit=3, per_domain_limit=6, recall=12)`：
先语义召回（含 Reranker 精排）得到种子工具，再**聚合到 capability_domain**，取语义最靠前的
至多 `domain_limit` 个能力族整族纳入（未声明 domain 的种子按"单工具族"各占一个名额）。

相比逐工具检索，它以**能力族**为最小装配单位，保证"能创建就能改/删"、装配语义连贯，
且用 **domain 数量**（而非工具总数）控制规模，避免半个族被截断。
`find_tools` 已改用本函数作为其检索后端（two-stage 的运行时落点）。

### 6.2 `visible_when` 条件隐藏（`register.py`）

`@ai_tools` 新增 `visible_when` 谓词参数，签名 `(ctx) -> bool | Awaitable[bool]`。
返回 False 时本工具在**该 step** 对模型隐藏（schema 都不下发），从源头降噪。
内部包装成 pydantic-ai 的 `prepare` 函数挂到 `Tool(prepare=...)`，判定抛异常时默认可见。

> ⚠️ 约束：`prepare` 每个 step 对每个工具求值，故 `visible_when` 谓词**必须廉价、内存判定**，
> 切忌每步查库 / 发网络。

**与 `check_func` 的分工**：`check_func` 在"已调用"后拦截执行并回错误文案；
`visible_when` 在"是否展示"阶段决定模型能否看到该工具。二者纵深互补。

**首个落地**（`command_executor.py`）：`execute_shell_command` 加
`visible_when=_shell_visible_to_admin`——

- 后台 / 能力代理（无 `ev`）→ 返回 True 不隐藏，交 `check_func` 执行期兜底，
  **避免误伤**显式装配本工具的能力代理；
- 交互式用户 → 仅管理员（`ev.user_pm == 0`）可见。

既给普通用户的工具列表减一个高危工具噪声，又与既有 `check_pm` 执行期校验纵深互补。

---

## 7. 取舍细节

- **跨轮澄清的处理**：曾尝试用纯规则（上轮 bot 以「?」结尾 + 本轮短补全）把 bot 问句拼进
  query，因太刻意、易误伤普通短回复已撤掉，改由 Phase 2 `find_tools` 通用兜底（模型读
  上文自行现拉）。代价是依赖模型主动意识到缺工具，弱模型可能不触发。
- **Reranker 召回池大小**：`_RERANK_RECALL_LIMIT=20` 是"精排质量 vs 精排耗时"的折中。
  调大召回更全但精排更慢。
- **Phase 2 多一次 round-trip**：长尾意图里模型先调 `find_tools` 再调真工具，首字延迟 +1 跳。
  用意图门把这代价限制在非闲聊轮，且 `find_tools` 是安全网（多数轮静态召回已够、不触发）。
  仍依赖模型主动意识到"我缺工具"——弱模型可能不调；故**不能纯靠它**，保底/状态驱动仍在。
- **保底池没有激进砍小**：理论上 Phase 2 允许把保底池砍到极小、全靠 `find_tools` 现拉，
  但那对弱模型不稳。本期保守地把 `find_tools` 作为**增量安全网**叠加在现有分层之上，
  真正的"减工具"来自 Phase 1 降 limit + Phase 3 条件隐藏。后续可视模型能力再激进化。
- **Phase 3 per-step 成本**：`visible_when` 的便利以"每步每工具求值"为代价，故只用于廉价谓词；
  贵的前置条件仍应走 L2 状态驱动（加载时判一次）而非 `visible_when`。

---

## 8. 风险与回滚

| 改动 | 风险 | 回滚方式 |
|---|---|---|
| Reranker 精排 | rerank 服务异常拖慢检索 | 函数内已自动降级为向量序；或 `search_tools(rerank=False)` |
| 降 limit | 多能力轮召回不足 | 调回 `limit=8` / `MAX_EXTRA_TOOLS=16` |
| 渐进式暴露 | 模型滥用 `find_tools` 刷往返 | 置 `ENABLE_PROGRESSIVE_TOOLS=False` 即完全回退 Phase 1 |
| `visible_when` | 谓词写错误隐藏工具 | 判定异常已默认可见；移除该工具的 `visible_when` 即恢复 |

所有新机制都有**总开关 / 降级路径**，最坏情况可逐项关回到改动前行为。

---

## 9. 验证情况

均以脚本驱动 pydantic-ai 1.72 实测（非纸面推断）：

- **编译**：全部改动文件 `py_compile` 通过。
- **Phase 2 端到端**（`FunctionModel` 驱动真实 run）：`get_weather` 在 step 1 对模型**不可见**，
  模型调 `find_tools` 写入集合后，**step 2 动态出现并被成功调用**——证明"调一次检索工具 →
  下一步即可调用"在框架内闭环。
- **Phase 3b 条件隐藏**（`FunctionModel` 读 `info.function_tools`）：管理员可见、后台(无 ev)可见、
  **交互式非管理员不可见**，符合 `_shell_visible_to_admin` 设计。
- **注册自检**：`find_tools` 注册在 `meta` 分类；`execute_shell_command` 的 `Tool` 已携带
  `prepare`（visible_when）；`gs_agent` / `dynamic_toolset` / `rag.tools` 均正常 import，
  `NON_SEARCHABLE_TOOL_CATEGORIES = {meta, plugin_dev}`。

> 未覆盖：真实 Qdrant + Reranker + 真实模型的线上联调（需运行环境），建议灰度观察
> `find_tools` 调用频率与每轮工具数变化后再全量。

---

## 10. 附录 A：核心代码与调用链

### 10.1 先厘清：`find_tools` 不是 pydantic-ai 的内置工具

`find_tools` 是**本项目自己定义并注册**的一个普通工具，和 `get_weather`、
`execute_shell_command` 同级——只是它的职责是"检索并解锁其它工具"。它进入模型工具列表
要走完整四步，没有任何"框架魔法"：

```
① 定义   dynamic_tool_discovery.py:  async def find_tools(ctx, need)  +  @ai_tools(category="meta")
② 注册   register.py 的 @ai_tools:   把函数包成 pydantic-ai Tool 对象 → 存进 _TOOL_REGISTRY
③ 注入   gs_agent.py 装配阶段:        find_tool_base("find_tools") → tools.append(ft.tool)
④ 调用   模型看到 find_tools 的 schema（就是它的 docstring）→ 像普通工具一样调用它
```

模型调用 `find_tools` 后，函数体把命中工具名写进 `ctx.deps.dynamic_tool_names`；
真正"让工具变可调用"的是 `RetrievableToolset`（见 10.4）。

### 10.2 单轮共享集合（`models.py`）

`ToolContext` 每轮 run 新建一个，故这个集合天然是"单次 run"作用域：

```diff
@@ class ToolContext @@
     ev: Optional[Event] = None
     extra: Dict[str, Any] = field(default_factory=dict)
     parent_session_id: Optional[str] = None
+    # 渐进式工具暴露：find_tools 本轮命中的工具名集合，RetrievableToolset 每 step 读它
+    # 解析成可调用工具。作用域为单次 run（ToolContext 每轮新建），轮末自然丢弃。
+    dynamic_tool_names: Set[str] = field(default_factory=set)
```

### 10.3 `find_tools` meta-tool（`buildin_tools/dynamic_tool_discovery.py`，新增）

docstring 即模型看到的工具说明；函数体只做两件事：检索 → 写共享集合。

```python
# 不声明 capability_domain：find_tools 是单例 meta 工具，无能力族语义；声明了反而会被
# L3 会话驻留按族带进随后数轮（含闲聊），破坏"闲聊轮零开销"。它的装配完全由意图门控制。
@ai_tools(category="meta")
async def find_tools(ctx: RunContext[ToolContext], need: str) -> str:
    """按需加载完成任务所缺的工具（渐进式工具暴露）。
    当你发现当前可用工具里没有能完成用户需求的工具时，用一句话描述你需要的能力，调用本工具。
    命中的相关工具会在下一步变为可直接调用——不要在本步假装调用它们。
    """
    family_tools = await search_tools_by_domain(query=need, domain_limit=3, per_domain_limit=6)
    if not family_tools:
        return f"⚠️ 没有找到与「{need}」相关的工具，请换个更具体的描述，或直接据现有能力作答。"

    loaded_names = [t.name for t in family_tools]
    ctx.deps.dynamic_tool_names.update(loaded_names)   # ← 关键：写入单轮共享集合
    listing = "\n".join(f"- {name}" for name in loaded_names)
    return f"✅ 已加载以下工具，下一步即可直接调用：\n{listing}"
```

> 配套：`rag/tools.py` 的 `NON_SEARCHABLE_TOOL_CATEGORIES` 加入 `"meta"`，
> 确保 `find_tools` 自己永不被向量检索召回（只由 gs_agent 按意图门显式注入），避免自指。

### 10.4 `RetrievableToolset`（`dynamic_toolset.py`，新增）—— 闭环的关键

pydantic-ai 的 `AbstractToolset.get_tools(ctx)` 会在**每个 step 被重新调用**，
所以这里每步都重读共享集合、把名字解析成可调用工具。这就是"上一步写、下一步可用"
能成立的根本原因：

```python
class RetrievableToolset(AbstractToolset[ToolContext]):
    def __init__(self, exclude_names: Set[str], max_retries: int = 1):
        self._exclude = set(exclude_names)   # 本轮静态已装配工具名，跳过以免跨 toolset 重名
        self._max_retries = max_retries

    async def get_tools(self, ctx):          # ← pydantic-ai 每个 step 都会调用
        allowed = set(ctx.deps.dynamic_tool_names)   # ← 读 find_tools 写入的集合
        if not allowed:
            return {}
        out = {}
        for name in allowed:
            if name in self._exclude:
                continue
            tb = find_tool_base(name)        # 名字 → 注册表里的 ToolBase
            if tb is None:
                continue
            tool = tb.tool
            run_context = replace(ctx, tool_name=name, retry=ctx.retries.get(name, 0), ...)
            tool_def = await tool.prepare_tool_def(run_context)   # 会跑该工具的 visible_when
            if not tool_def:                 # Phase 3 条件隐藏：本步判定不暴露
                continue
            out[tool_def.name] = FunctionToolsetTool(   # 复用 pydantic-ai 内置实现
                toolset=self, tool_def=tool_def, max_retries=max_retries,
                args_validator=tool.function_schema.validator,
                call_func=tool.function_schema.call,
                is_async=tool.function_schema.is_async, timeout=tool_def.timeout,
            )
        return out
```

### 10.5 装配侧门控（`gs_agent.py`）

只有"自动装配 + 非闲聊轮"才注入 `find_tools` 并挂 `RetrievableToolset`；
`exclude_names` 在**去重后**取静态工具名，避免和 `Agent(tools=...)` 隐式 toolset 撞名：

```diff
+                # 渐进式工具暴露：非闲聊轮注入 find_tools 并标记本轮挂 RetrievableToolset，
+                # 模型中途发现缺工具即可调 find_tools 现拉，下一步即可用。闲聊轮跳过。
+                if (
+                    ENABLE_PROGRESSIVE_TOOLS
+                    and intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS
+                    and not any(t.name == "find_tools" for t in tools)
+                ):
+                    ft = find_tool_base("find_tools")
+                    if ft is not None:
+                        tools.append(ft.tool)
+                        _expose_dynamic = True
```

```diff
         _toolsets = [skills_toolset] if self.create_by in _SKILLS_CREATE_BY else []
+        # 启用渐进式暴露时挂 RetrievableToolset：每个 step 读 dynamic_tool_names 即时暴露命中工具。
+        # exclude_names 传静态已装配工具名，避免与 Agent(tools=...) 隐式 toolset 重名冲突。
+        if _expose_dynamic:
+            _toolsets = [*_toolsets, RetrievableToolset(exclude_names=set(tool_names))]
         _agent = Agent(model=self.model, deps_type=ToolContext, tools=tools, toolsets=_toolsets, ...)
```

### 10.6 一次 run 的完整时间线

```
step1  ModelRequest: 工具列表 = 静态召回(L1–L5) + find_tools          模型: 缺"查天气"工具
       ToolCall:    find_tools(need="查询某城市实时天气")
       工具体:       search_tools_by_domain → dynamic_tool_names |= {get_weather, ...}
                    返回文本"✅ 已加载: get_weather ..."
step2  get_tools:   RetrievableToolset 读集合 → 解析出可调用的 get_weather   ← 本步它才"出现"
       ModelRequest: 工具列表多了 get_weather
       ToolCall:    get_weather(city="广州")   ← 同一轮内完成，无需用户再补话
```

### 10.7 Phase 3 条件隐藏：`visible_when → prepare`（`register.py`）

`visible_when` 谓词被包成 pydantic-ai 的 `prepare` 函数挂到 `Tool(prepare=...)`。
`prepare` 每个 step 对每个工具求值，返回 `None` 即本步对模型隐藏（schema 都不下发）：

```diff
+        # 5.5 条件隐藏（Phase 3）：visible_when 谓词包装成 pydantic-ai 的 prepare 函数。
+        # prepare 在每个 step 被调用，返回 None 即本步不向模型暴露该工具。
+        prepare_fn = None
+        if visible_when is not None:
+            async def _prepare(ctx, tool_def, _pred=visible_when, _name=fn.__name__):
+                try:
+                    res = _pred(ctx)
+                    if inspect.isawaitable(res):
+                        res = await res
+                except Exception as e:
+                    logger.debug(f"🧠 [Register] 工具 [{_name}] visible_when 判定异常，默认可见: {e}")
+                    return tool_def
+                return tool_def if res else None
+            prepare_fn = _prepare
-        tool_obj = Tool(wrapped_tool, takes_ctx=True)
+        tool_obj = Tool(wrapped_tool, takes_ctx=True, prepare=prepare_fn)
```

注意 10.4 的 `RetrievableToolset` 里 `if not tool_def: continue` 复用的就是同一条
`prepare` 链——动态暴露的工具同样受 `visible_when` 约束（如非管理员即便 `find_tools`
拉到了 `execute_shell_command`，本步仍会被隐藏）。

---

## 11. 跟进（2026-06-20）：保底池条件隐藏

### 11.1 背景

Phase 1–3 只动了**附加池**（降 limit、`find_tools` 安全网、`visible_when` 首落地），
**保底池（L1：`self` + `buildin`）一个都没减**——正是 §7「保底池没有激进砍小」的遗留。
实测保底池 = `self`(4) + `buildin`(10) = **14 个无条件常驻**，叠加非闲聊轮的 `find_tools`
与状态/语境/向量附加后，闲聊一句仍常挂 20+ 个 schema。本次把 Phase 3 的 `visible_when`
机制**从附加池下沉到保底池**，给其中**窄场景常驻**工具按上下文条件隐藏。

### 11.2 改动

新增 `buildin_tools/visibility.py`，提供两个**廉价内存谓词**，挂到两个保底工具上
（工具仍属保底、需要时立即出现，只是无关轮不下发 schema）：

| 工具 | `visible_when` | 暴露条件 |
|------|----------------|----------|
| `read_image` | `context_has_image` | 当前轮 `ev` 有图片，或本轮上下文出现过 `img_*` / "图片ID" |
| `web_fetch_tool` | `context_has_url` | `ev` 文本/附件是 URL，或本轮 run 消息里出现过 `http(s)://` |

**关键正确性点**：`web_fetch` 的 URL **多来自 `web_search_tool` 的工具结果**（落在
`ctx.messages` 里、不在 `ev` 文本）。若只看 `ev` 文本会在"搜完想抓取"时误隐藏 `web_fetch`
（且 `visible_when` 同样作用于 `find_tools` 动态暴露，隐藏后连现拉都救不回）。故谓词通过
`_iter_context_texts(ctx)` **连 `ctx.messages` 一起扫**，覆盖"用户贴链接"与"search→fetch"两条路径。

谓词一律**偏可见**：拿不准就显示（误隐藏的代价远大于多显示一个），且 `register.py` 的
`prepare` 包装在判定抛异常时默认可见，纵深兜底。

### 11.3 效果与边界

- **效果**：图片无关轮少下发 `read_image`、URL 无关轮少下发 `web_fetch_tool`。叠加下方
  `state_list` 降级后，保底基数 14→**13**，典型闲聊/无图无 URL 轮实下发 ~11。
- **`state_list` 降级（2026-06-20）**：`state_*` 五件套里 `state_set`/`state_get` 是 bootstrap
  读写对、保留保底；`state_list` 仅用于"任务初始化没"这类判断、频率低于 set/get，已从 `buildin`
  降到 `common`（仍带 `capability_domain="持久状态"`，真做 state 任务、召回到任一 state 工具时整族
  带出）。`state_delete`/`state_append` 早已在 `common`。
- **仍未做（留给后续 B/C 档）**：把 `get_self_info` / `get_self_persona_info` /
  `query_user_memory` / `update_user_favorability` 等**移出保底、降级为按需检索**可再省 3~4 个，
  但要靠 `find_tools` 现拉、对弱模型不稳，本次未做（与 §7「视模型能力再激进化」一致）。
- **成本**：两个谓词每 step 各扫一次 `ev` + `ctx.messages`（短路命中即返），纯内存无 IO，
  符合 §6.2 对 `visible_when` 「廉价、内存判定」的约束。

### 11.4 验证

- 三个改动文件 `ast.parse` 通过；谓词逻辑单测（URL：用户贴链接 / search 结果 / 无 URL / 无 ev /
  附件 URL；图片：当前图 / 历史图片ID / 无图 / 无 ev）全部符合预期。
- 真实 import 通过（无循环依赖）；`read_image` / `web_fetch_tool` 的 `Tool` 均已携带
  `prepare`（visible_when 已挂上）。

### 11.5 回滚

移除对应工具 `@ai_tools(...)` 里的 `visible_when=` 参数即恢复"无条件常驻"；
谓词模块 `visibility.py` 可独立保留不影响其它工具。
